"""
Audit log — JSONL record of every significant lifecycle event.

When ``audit.enabled: true`` is set in ``~/.hermes/config.yaml``, each
hook fire, plan-mode transition, verifier verdict, and significant tool
call appends one JSON object (one line) to
``~/.hermes/logs/audit.jsonl``.  The format is stable across releases:
scripted consumers can ``tail -f`` it, pipe through ``jq``, or ship it
to log aggregators without parsing surprises.

Why JSONL rather than the existing agent.log
--------------------------------------------

``agent.log`` is a free-form text log — great for tailing during
development, terrible for analytics.  The audit log is structured:
every event has a fixed schema, every value is JSON-serialisable, and
every line stands alone (no multi-line stack traces).  Operators can
build dashboards from this without sed/regex acrobatics.

Schema
------

::

    {
      "ts": "2026-05-21T14:32:15.123456+00:00",
      "session_id": "sess_abc123",
      "event": "PreToolUse" | "PostToolUse" | "Stop" | "Verification" |
               "PlanModeEnter" | "PlanModeExit" | "SessionStart" |
               "SessionEnd" | "UserPromptSubmit" | "SubagentStop" |
               "ReworkInjected" | <custom>,
      "data": {
          # event-specific payload, redacted via the same redactor
          # that hook payloads use (when audit.redact_secrets is on)
      },
      "agent": "primary" | "subagent",
      "hermes_version": "0.13.0"
    }

The module is intentionally tiny — no rotation, no async, no fancy
buffering.  Audit lines are one-shot appends; a busy session writes
maybe a few hundred lines.  Operators that need rotation can pipe
``audit.jsonl`` through logrotate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_DEFAULT_AUDIT_FILENAME = "audit.jsonl"
_MAX_FIELD_BYTES = 16 * 1024
_WRITE_LOCK = threading.Lock()

#: The genesis sentinel — every audit chain starts with this prev_hash.
#: Picking a fixed sentinel (rather than empty string) means tampering
#: with the first line is detectable: any verifier checking the chain
#: will refuse a first line whose prev_hash != GENESIS.
GENESIS_PREV_HASH = "0" * 64

#: Cache of (key, last_hash) per file path so concurrent writes from
#: the same process avoid re-reading the file on every append.
_CHAIN_STATE: Dict[str, str] = {}
_CHAIN_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Event-type constants — keep them centralised so typos surface
# ---------------------------------------------------------------------------

EVENT_SESSION_START = "SessionStart"
EVENT_SESSION_END = "SessionEnd"
EVENT_USER_PROMPT_SUBMIT = "UserPromptSubmit"
EVENT_PRE_TOOL_USE = "PreToolUse"
EVENT_POST_TOOL_USE = "PostToolUse"
EVENT_STOP = "Stop"
EVENT_SUBAGENT_STOP = "SubagentStop"
EVENT_VERIFICATION = "Verification"
EVENT_PLAN_MODE_ENTER = "PlanModeEnter"
EVENT_PLAN_MODE_EXIT = "PlanModeExit"
EVENT_REWORK_INJECTED = "ReworkInjected"


# ---------------------------------------------------------------------------
# Config + path resolution
# ---------------------------------------------------------------------------


def _audit_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        return dict(load_config().get("audit") or {})
    except Exception:
        return {}


def _is_enabled() -> bool:
    return bool(_audit_config().get("enabled", False))


def _signing_key() -> Optional[bytes]:
    """Resolve the HMAC signing key.

    Precedence:
    1. ``HERMES_AUDIT_HMAC_KEY`` env var (hex or raw)
    2. ``audit.hmac_key_file`` config value (path to key file)
    3. None — chain still works (prev_hash links), but lines are unsigned

    Returns bytes or None.  ``None`` means "use hash-chain only, no HMAC."
    """
    raw = os.environ.get("HERMES_AUDIT_HMAC_KEY", "").strip()
    if raw:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            return raw.encode("utf-8")
    cfg = _audit_config()
    key_path = cfg.get("hmac_key_file")
    if key_path:
        try:
            return Path(str(key_path)).expanduser().read_bytes().strip()
        except OSError:
            logger.debug("audit: could not read hmac_key_file %s", key_path)
    return None


def _line_hash(line: str) -> str:
    """SHA-256 of the line bytes — used as next-line prev_hash."""
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _hmac_sign(prev_hash: str, content_json: str, key: bytes) -> str:
    """HMAC-SHA256 over ``prev_hash || content`` — the line's signature.

    Including the prev_hash inside the MAC means tampering with the
    chain (e.g. truncating an earlier line) breaks every subsequent
    signature.  This is what makes the log tamper-evident.
    """
    mac = hmac.new(key, digestmod=hashlib.sha256)
    mac.update(prev_hash.encode("utf-8"))
    mac.update(b"|")
    mac.update(content_json.encode("utf-8"))
    return mac.hexdigest()


def _read_last_chain_state(path: Path) -> str:
    """Return the last line's hash from the file on disk.

    Cached per-path so we don't re-tail on every write.  Cache miss
    paths: file doesn't exist (returns GENESIS), file is empty
    (returns GENESIS), file's last line can't be parsed (returns
    GENESIS — chain will visibly fork, verifier flags it).
    """
    key = str(path)
    with _CHAIN_LOCK:
        cached = _CHAIN_STATE.get(key)
        if cached is not None:
            return cached
    last_hash = GENESIS_PREV_HASH
    if path.is_file():
        try:
            # Tail the file — read the last line only.  Cheap because
            # we only do this once per process per path.
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                if size > 0:
                    # Walk back up to 32 KB looking for the last newline.
                    chunk = min(size, 32 * 1024)
                    fh.seek(size - chunk)
                    tail = fh.read(chunk).decode("utf-8", errors="replace")
                    lines = [l for l in tail.splitlines() if l.strip()]
                    if lines:
                        last_hash = _line_hash(lines[-1])
        except OSError:
            pass
    with _CHAIN_LOCK:
        _CHAIN_STATE[key] = last_hash
    return last_hash


def _update_chain_state(path: Path, new_hash: str) -> None:
    with _CHAIN_LOCK:
        _CHAIN_STATE[str(path)] = new_hash


def _reset_chain_state(path: Optional[Path] = None) -> None:
    """Test-only: clear cached chain state for a path (or all paths)."""
    with _CHAIN_LOCK:
        if path is None:
            _CHAIN_STATE.clear()
        else:
            _CHAIN_STATE.pop(str(path), None)


def _resolve_log_path(cfg: Optional[Dict[str, Any]] = None) -> Path:
    cfg = cfg if cfg is not None else _audit_config()
    explicit = cfg.get("path")
    if explicit:
        return Path(str(explicit)).expanduser().resolve()
    try:
        from hermes_constants import get_hermes_home
        base = get_hermes_home() / "logs"
    except Exception:
        base = Path.home() / ".hermes" / "logs"
    return base / _DEFAULT_AUDIT_FILENAME


# ---------------------------------------------------------------------------
# Payload sanitisation
# ---------------------------------------------------------------------------


_TRUNC_SUFFIX = "... [truncated]"


def _truncate(value: Any, limit: int = _MAX_FIELD_BYTES) -> Any:
    """Truncate large strings so the audit log doesn't balloon.

    Final length is exactly ``limit`` — including when ``limit`` is
    smaller than the suffix.  When the requested cap can't fit the
    full suffix, we truncate the suffix itself so the result still
    fits, rather than returning a string longer than the caller
    asked for.
    """
    if isinstance(value, str) and len(value) > limit:
        if limit <= 0:
            return ""
        if limit >= len(_TRUNC_SUFFIX):
            return value[: limit - len(_TRUNC_SUFFIX)] + _TRUNC_SUFFIX
        # limit is shorter than the suffix — keep some marker but
        # never exceed the cap.
        return _TRUNC_SUFFIX[-limit:]
    if isinstance(value, dict):
        return {k: _truncate(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_truncate(v, limit) for v in value]
    return value


def _apply_redactor(payload: Dict[str, Any], redactor: Optional[Callable[[str], str]]) -> Dict[str, Any]:
    """Apply a redactor to every string value in the payload."""
    if redactor is None:
        return payload
    out: Dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(v, str):
            try:
                out[k] = redactor(v)
            except Exception:
                out[k] = v
        elif isinstance(v, dict):
            out[k] = _apply_redactor(v, redactor)
        elif isinstance(v, (list, tuple)):
            out[k] = [
                redactor(x) if isinstance(x, str) and redactor else x
                for x in v
            ]
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _format_line(
    event: str,
    *,
    session_id: str = "",
    data: Optional[Dict[str, Any]] = None,
    agent: str = "primary",
    hermes_version: Optional[str] = None,
    redactor: Optional[Callable[[str], str]] = None,
    prev_hash: str = GENESIS_PREV_HASH,
    signing_key: Optional[bytes] = None,
) -> str:
    """Render one audit-log line with tamper-evident chain metadata.

    Schema (extends the original):
        ts, session_id, event, agent, hermes_version, data,
        prev_hash       (SHA-256 of prior line, or GENESIS)
        sig             (HMAC-SHA256 over prev_hash + payload, optional)

    The sig field is omitted when no signing key is configured — the
    chain itself (prev_hash linking) still detects truncation and
    reordering even without HMAC.  HMAC raises the bar to "attacker
    must also have the key."
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    payload = {
        "ts": ts,
        "session_id": session_id or "",
        "event": event,
        "agent": agent or "primary",
        "hermes_version": hermes_version or os.environ.get("HERMES_VERSION", ""),
        "data": _apply_redactor(_truncate(data or {}), redactor),
        "prev_hash": prev_hash,
    }
    if signing_key:
        # Sign the JSON of everything except the sig field itself.
        # Canonical serialisation (sort_keys=True) means the verifier
        # can reproduce the byte sequence we signed without ambiguity.
        canonical = json.dumps(payload, ensure_ascii=False, default=str,
                               sort_keys=True)
        payload["sig"] = _hmac_sign(prev_hash, canonical, signing_key)
    return json.dumps(payload, ensure_ascii=False, default=str)


def write_event(
    event: str,
    *,
    session_id: str = "",
    data: Optional[Dict[str, Any]] = None,
    agent: str = "primary",
    hermes_version: Optional[str] = None,
    redactor: Optional[Callable[[str], str]] = None,
    path: Optional[Path] = None,
) -> bool:
    """Append one event to the audit log.  Returns True on success.

    Silent failures: returns False and logs a debug-level message rather
    than raising.  Audit logging is best-effort observability — a full
    disk or permissions issue must never block the agent loop.

    Reads ``audit.enabled`` from config.yaml on every call.  Operators
    can flip the flag mid-session and subsequent writes will (start /
    stop) without restart.
    """
    if not _is_enabled():
        return False
    target = path if path is not None else _resolve_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("audit: could not create log dir %s: %s", target.parent, exc)
        return False
    # Default-on redaction — when audit.enabled is true, secrets in
    # the data payload are scrubbed via the canonical
    # agent.redact.redact_sensitive_text unless the caller passed an
    # explicit redactor (typically None means "use default").  The
    # docstring promises 'redacted via the same redactor that hook
    # payloads use' — the prior implementation left this unwired,
    # writing raw secrets to disk.  Operators can disable explicitly
    # via security.redact_secrets: false in config.yaml.
    if redactor is None:
        try:
            from agent.redact import redact_sensitive_text
            try:
                from hermes_cli.config import load_config
                _sec_cfg = (load_config().get("security") or {})
                _redact_on = bool(_sec_cfg.get("redact_secrets", True))
            except Exception:
                _redact_on = True
            if _redact_on:
                # Wrap to match the (text -> str) signature; the
                # _apply_redactor helper handles string-or-nested-dict
                # recursion downstream.
                redactor = lambda s: redact_sensitive_text(s, force=False)
        except Exception:
            pass
    # Hash-chain + optional HMAC.  Per-file write lock ensures the
    # chain stays consistent under concurrent writers.
    key = _signing_key()
    try:
        with _WRITE_LOCK:
            # Cross-process correctness: in-process _CHAIN_STATE cache
            # can lie when another process has appended lines since we
            # last read.  Take an OS-level exclusive flock on the file
            # FOR THE ENTIRE read-tail + append critical section.  On
            # platforms without fcntl (Windows native), we fall back
            # to in-process cache only — operators should pin a single
            # writer process in that case.
            try:
                import fcntl  # POSIX
            except ImportError:
                fcntl = None  # type: ignore[assignment]
            with target.open("a", encoding="utf-8") as fh:
                if fcntl is not None:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                    except OSError:
                        pass  # filesystem may not support flock (NFS etc.)
                # ALWAYS re-tail under the flock — the cache is per-
                # process and can't see another process's appends.
                # The tail-read is O(32KB) so this is fast.
                _reset_chain_state(target)
                prev = _read_last_chain_state(target)
                line = _format_line(
                    event,
                    session_id=session_id, data=data, agent=agent,
                    hermes_version=hermes_version, redactor=redactor,
                    prev_hash=prev, signing_key=key,
                )
                fh.write(line)
                fh.write("\n")
                fh.flush()
                _update_chain_state(target, _line_hash(line))
                # flock is released on fh.close() (with-exit).
    except OSError as exc:
        logger.debug("audit: write to %s failed: %s", target, exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Read helpers — used by the (future) /audit slash command and tests
# ---------------------------------------------------------------------------


def tail_events(n: int = 50, *, path: Optional[Path] = None) -> list[Dict[str, Any]]:
    """Read the last ``n`` audit events.  Returns an empty list when
    the file doesn't exist.  Never raises.
    """
    target = path if path is not None else _resolve_log_path()
    if not target.exists():
        return []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[Dict[str, Any]] = []
    for line in text.splitlines()[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def count_events_by_type(*, path: Optional[Path] = None) -> Dict[str, int]:
    """Return a summary of event counts.  Useful for /audit summary."""
    counts: Dict[str, int] = {}
    for event in tail_events(n=10_000, path=path):
        evt = str(event.get("event") or "<unknown>")
        counts[evt] = counts.get(evt, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Chain verification — enterprise tamper-evidence
# ---------------------------------------------------------------------------


from dataclasses import dataclass, field


@dataclass
class ChainVerificationResult:
    """Outcome of a tamper-evidence check on the audit log."""

    ok: bool = True
    lines_total: int = 0
    lines_ok: int = 0
    first_bad_line: Optional[int] = None
    failure_reason: Optional[str] = None
    sig_checked: bool = False
    sig_ok: int = 0
    sig_missing: int = 0
    issues: List[str] = field(default_factory=list)


def verify_chain(
    *,
    path: Optional[Path] = None,
    signing_key: Optional[bytes] = None,
) -> ChainVerificationResult:
    """Walk the audit log and check the prev_hash chain (and HMAC if key supplied).

    Returns a ChainVerificationResult.  Three failure modes:

    * **First line's prev_hash != GENESIS** — someone deleted lines from
      the front of the log.
    * **Any line's prev_hash != hash(prior_line)** — a line was edited,
      reordered, or deleted from the middle.
    * **HMAC mismatch (when key supplied)** — content was tampered with
      and the chain rewritten, but the attacker didn't have the key.

    When ``signing_key`` is None and lines contain sigs, the sigs are
    reported as 'missing key' rather than failed.
    """
    target = path if path is not None else _resolve_log_path()
    result = ChainVerificationResult()
    if signing_key is None:
        signing_key = _signing_key()
    result.sig_checked = signing_key is not None
    if not target.is_file():
        result.failure_reason = f"audit log not found: {target}"
        result.ok = False
        return result
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        result.failure_reason = f"cannot read {target}: {exc}"
        result.ok = False
        return result

    expected_prev = GENESIS_PREV_HASH
    for lineno, raw in enumerate(text.splitlines(), start=1):
        raw = raw.rstrip("\n")
        if not raw.strip():
            continue
        result.lines_total += 1
        try:
            event = json.loads(raw)
        except ValueError as exc:
            result.ok = False
            result.first_bad_line = lineno
            result.failure_reason = f"line {lineno}: malformed JSON ({exc})"
            return result
        actual_prev = str(event.get("prev_hash", ""))
        if actual_prev != expected_prev:
            result.ok = False
            result.first_bad_line = lineno
            result.failure_reason = (
                f"line {lineno}: prev_hash mismatch "
                f"(expected {expected_prev[:12]}..., got {actual_prev[:12] or '<missing>'}...)"
            )
            return result
        if signing_key is not None:
            sig = event.pop("sig", None)
            if sig is None:
                result.sig_missing += 1
                result.issues.append(f"line {lineno}: no signature present")
            else:
                canonical = json.dumps(event, ensure_ascii=False,
                                       default=str, sort_keys=True)
                expected_sig = _hmac_sign(actual_prev, canonical, signing_key)
                if not hmac.compare_digest(sig, expected_sig):
                    result.ok = False
                    result.first_bad_line = lineno
                    result.failure_reason = (
                        f"line {lineno}: HMAC signature mismatch "
                        f"(content tampered with or wrong key)"
                    )
                    return result
                result.sig_ok += 1
        expected_prev = _line_hash(raw)
        result.lines_ok += 1
    return result


__all__ = [
    "EVENT_PLAN_MODE_ENTER",
    "EVENT_PLAN_MODE_EXIT",
    "EVENT_POST_TOOL_USE",
    "EVENT_PRE_TOOL_USE",
    "EVENT_REWORK_INJECTED",
    "EVENT_SESSION_END",
    "EVENT_SESSION_START",
    "EVENT_STOP",
    "EVENT_SUBAGENT_STOP",
    "EVENT_USER_PROMPT_SUBMIT",
    "EVENT_VERIFICATION",
    "count_events_by_type",
    "tail_events",
    "write_event",
]
