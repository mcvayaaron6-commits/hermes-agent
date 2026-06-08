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

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


_DEFAULT_AUDIT_FILENAME = "audit.jsonl"
_MAX_FIELD_BYTES = 16 * 1024
_WRITE_LOCK = threading.Lock()


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

    Final length is exactly ``limit`` — the suffix is included inside
    the cap, not appended past it.
    """
    if isinstance(value, str) and len(value) > limit:
        return value[: max(0, limit - len(_TRUNC_SUFFIX))] + _TRUNC_SUFFIX
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
) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    payload = {
        "ts": ts,
        "session_id": session_id or "",
        "event": event,
        "agent": agent or "primary",
        "hermes_version": hermes_version or os.environ.get("HERMES_VERSION", ""),
        "data": _apply_redactor(_truncate(data or {}), redactor),
    }
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
    line = _format_line(
        event,
        session_id=session_id, data=data, agent=agent,
        hermes_version=hermes_version, redactor=redactor,
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("audit: could not create log dir %s: %s", target.parent, exc)
        return False
    try:
        with _WRITE_LOCK:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.write("\n")
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
