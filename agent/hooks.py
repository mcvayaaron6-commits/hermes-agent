"""
User-defined hooks — deterministic lifecycle callbacks the harness runs.

Hermes already supports rich Python ``*_callback`` parameters used internally
by the gateway / CLI / TUI, but it does not let *users* register
shell-command hooks that fire at well-defined lifecycle points the way
Claude Code, OpenHands 1.6+, and LangGraph do.  Hooks are what let the
*harness* — not the LLM — deterministically enforce lints, formatters,
secret scans, custom approvals, and telemetry every loop.

This module ships the engine.  Wiring (i.e. where each event fires) lives in
``run_agent.py`` and ``model_tools.py``.  The configuration loader lives in
``hermes_cli/config.py``.

Hook events
-----------

``SessionStart``     Fires when an ``AIAgent`` finishes initialisation.
``UserPromptSubmit`` Fires before a user message is appended to the
                     transcript and sent to the model.  May block or
                     transform.
``PreToolUse``       Fires before each tool call.  May block (exit 2) or
                     transform args (stdout JSON with ``decision:
                     "transform"`` + ``args``).
``PostToolUse``      Fires after each tool call.  May append context that
                     reaches the model as additional tool output.
``Stop``             Fires when the model emits a final non-tool response.
                     Exit 2 reinjects stderr as a user-role message so the
                     loop continues; exit 0 lets the response stand.
``SubagentStop``     Fires when a delegated subagent finishes.
``SessionEnd``       Fires when the session closes (best-effort).

Hook command I/O contract
-------------------------

Each hook command is invoked as ``[sh -c, cmd]`` (or directly when
``command`` is a list) with the JSON event payload on stdin.  Resolution:

* exit 0                          → allow as-is
* exit 2                          → block; ``stderr`` surfaces back into
                                    the agent loop (as a tool result for
                                    PreToolUse/PostToolUse, as a
                                    user-role message for Stop/SubagentStop)
* any other non-zero              → treated as a hook error, logged,
                                    does not block
* stdout JSON ``{decision, ...}`` → richer control:
    - ``decision: "allow"``         no-op
    - ``decision: "block"``         block with ``reason`` (or stderr)
    - ``decision: "transform"``     replace ``args`` (PreToolUse only) or
                                    append ``additional_context``

A hook command receives this payload on stdin (JSON, one object):

    {
        "event": "PreToolUse" | "PostToolUse" | "UserPromptSubmit" | ...,
        "session_id": str,           # may be empty pre-session-db
        "cwd": str,
        "hermes_version": str,
        "timestamp": float,          # unix seconds
        "tool": str | null,          # PreToolUse/PostToolUse only
        "args": dict | null,         # PreToolUse only
        "result": str | null,        # PostToolUse only (truncated)
        "user_message": str | null,  # UserPromptSubmit only
        "final_response": str | null,# Stop / SubagentStop only
        "agent_name": str | null,    # SubagentStop only
    }

Configuration is loaded by ``hermes_cli/config.py`` from (in order):

    .hermes/hooks.json     (project, walking up from cwd) — requires trust
    ~/.hermes/hooks.json   (user-global)

with the project file's entries taking precedence over the user-global
ones for matching events.  Schema:

    {
      "PreToolUse": [
        {"matcher": "write_file", "command": "./scripts/secret-scan.sh"}
      ],
      "PostToolUse": [
        {"matcher": "write_file|patch", "command": "ruff check --fix"}
      ],
      "Stop": [
        {"command": "./scripts/run-tests.sh", "timeout": 120}
      ]
    }

``matcher`` is a regex matched against the tool name (PreToolUse /
PostToolUse only).  Empty / absent matcher matches every tool.

Security
--------

The user-global file (``~/.hermes/hooks.json``) is implicitly trusted.
The repo-level file is only honoured after a one-time user trust prompt
recorded in ``~/.hermes/trusted_hook_files.json`` keyed on the file's
sha256.  ``HermesAgent`` callers can pre-trust paths via the
``HERMES_TRUSTED_HOOK_FILES`` env var (colon-separated paths).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event names — the harness wiring depends on these literals.
# ---------------------------------------------------------------------------

EVENT_SESSION_START = "SessionStart"
EVENT_USER_PROMPT_SUBMIT = "UserPromptSubmit"
EVENT_PRE_TOOL_USE = "PreToolUse"
EVENT_POST_TOOL_USE = "PostToolUse"
EVENT_STOP = "Stop"
EVENT_SUBAGENT_STOP = "SubagentStop"
EVENT_SESSION_END = "SessionEnd"

ALL_EVENTS: Tuple[str, ...] = (
    EVENT_SESSION_START,
    EVENT_USER_PROMPT_SUBMIT,
    EVENT_PRE_TOOL_USE,
    EVENT_POST_TOOL_USE,
    EVENT_STOP,
    EVENT_SUBAGENT_STOP,
    EVENT_SESSION_END,
)

_DEFAULT_TIMEOUT_SECONDS = 30
_MAX_STDERR_BYTES = 16 * 1024
_MAX_STDOUT_BYTES = 16 * 1024
_RESULT_PAYLOAD_TRUNCATE = 8 * 1024


# ---------------------------------------------------------------------------
# Specs & result objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookSpec:
    """One hook entry loaded from configuration."""

    event: str
    command: Union[str, Sequence[str]]
    matcher: Optional[str] = None
    timeout: int = _DEFAULT_TIMEOUT_SECONDS
    source: str = "user"  # "user" | "project" | "env"

    def matches_tool(self, tool_name: Optional[str]) -> bool:
        if self.matcher is None or self.matcher == "":
            return True
        if tool_name is None:
            return False
        try:
            return re.search(self.matcher, tool_name) is not None
        except re.error:
            logger.warning("Invalid hook matcher regex %r — treating as no match", self.matcher)
            return False


@dataclass
class HookOutcome:
    """The aggregated result of running every hook for one event."""

    blocked: bool = False
    block_reason: Optional[str] = None
    transformed_args: Optional[Dict[str, Any]] = None
    additional_context: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    ran: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked


# ---------------------------------------------------------------------------
# Trust store for repo-level hook files
# ---------------------------------------------------------------------------


def _trust_store_path() -> Path:
    from hermes_constants import get_hermes_home  # local import to avoid cycle
    return get_hermes_home() / "trusted_hook_files.json"


def _load_trust_store() -> Dict[str, str]:
    path = _trust_store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (OSError, ValueError):
        return {}
    return {}


def _save_trust_store(store: Dict[str, str]) -> None:
    path = _trust_store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write hook trust store %s: %s", path, exc)


def _file_sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def is_path_trusted(path: Path) -> bool:
    """Return True if a repo-level hooks file's content hash is in the trust store."""
    env_paths = os.environ.get("HERMES_TRUSTED_HOOK_FILES", "")
    if env_paths:
        for raw in env_paths.split(os.pathsep):
            candidate = raw.strip()
            if candidate and Path(candidate).expanduser().resolve() == path.resolve():
                return True
    digest = _file_sha256(path)
    if digest is None:
        return False
    store = _load_trust_store()
    return store.get(str(path.resolve())) == digest


def trust_path(path: Path) -> bool:
    """Record a repo-level hooks file's current content hash as trusted."""
    digest = _file_sha256(path)
    if digest is None:
        return False
    store = _load_trust_store()
    store[str(path.resolve())] = digest
    _save_trust_store(store)
    return True


# ---------------------------------------------------------------------------
# Configuration parsing
# ---------------------------------------------------------------------------


def _parse_entry(event: str, entry: Any, source: str) -> Optional[HookSpec]:
    if not isinstance(entry, dict):
        logger.warning("Skipping non-object hook entry under %s: %r", event, entry)
        return None
    command = entry.get("command")
    if not command:
        logger.warning("Skipping hook with no command under %s: %r", event, entry)
        return None
    if not isinstance(command, (str, list, tuple)):
        logger.warning("Skipping hook with non-string/list command under %s: %r", event, command)
        return None
    matcher = entry.get("matcher")
    if matcher is not None and not isinstance(matcher, str):
        logger.warning("Hook matcher must be a string, got %r — ignoring matcher", matcher)
        matcher = None
    try:
        timeout = int(entry.get("timeout", _DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SECONDS
    timeout = max(1, min(600, timeout))
    if isinstance(command, (list, tuple)):
        command_value: Union[str, Sequence[str]] = tuple(str(c) for c in command)
    else:
        command_value = str(command)
    return HookSpec(
        event=event,
        command=command_value,
        matcher=matcher,
        timeout=timeout,
        source=source,
    )


def parse_hook_config(raw: Any, *, source: str) -> List[HookSpec]:
    """Parse a JSON-shaped hooks config dict into a list of HookSpec.

    Tolerant: skips malformed entries with a logged warning so a typo in
    one event doesn't disable the rest.  Unknown event names are also
    skipped with a warning.
    """
    out: List[HookSpec] = []
    if not isinstance(raw, dict):
        return out
    for event, entries in raw.items():
        if event not in ALL_EVENTS:
            logger.warning("Unknown hook event %r — known events: %s",
                           event, ", ".join(ALL_EVENTS))
            continue
        if not isinstance(entries, list):
            logger.warning("Hook event %s value must be a list, got %r — skipping",
                           event, type(entries).__name__)
            continue
        for entry in entries:
            spec = _parse_entry(event, entry, source)
            if spec is not None:
                out.append(spec)
    return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class HookRegistry:
    """Holds parsed hook specs and dispatches events.

    The registry is intentionally small and pure: it doesn't import from
    ``run_agent`` or ``model_tools`` so it can be unit-tested in isolation.
    """

    def __init__(self, specs: Iterable[HookSpec] = ()) -> None:
        self._specs: List[HookSpec] = list(specs)

    @classmethod
    def empty(cls) -> "HookRegistry":
        return cls(())

    @classmethod
    def from_config(
        cls,
        user_config: Any = None,
        project_config: Any = None,
    ) -> "HookRegistry":
        specs: List[HookSpec] = []
        if user_config is not None:
            specs.extend(parse_hook_config(user_config, source="user"))
        if project_config is not None:
            specs.extend(parse_hook_config(project_config, source="project"))
        return cls(specs)

    def __len__(self) -> int:
        return len(self._specs)

    def __iter__(self):
        return iter(self._specs)

    def for_event(self, event: str, tool_name: Optional[str] = None) -> List[HookSpec]:
        return [s for s in self._specs if s.event == event and s.matches_tool(tool_name)]

    def has_any(self, event: str) -> bool:
        return any(s.event == event for s in self._specs)

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {evt: 0 for evt in ALL_EVENTS}
        for spec in self._specs:
            counts[spec.event] = counts.get(spec.event, 0) + 1
        return counts

    def describe(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for spec in self._specs:
            cmd_display = spec.command if isinstance(spec.command, str) else " ".join(spec.command)
            out.append({
                "event": spec.event,
                "matcher": spec.matcher or "*",
                "command": cmd_display,
                "timeout": spec.timeout,
                "source": spec.source,
            })
        return out


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _truncate(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 13] + "... [truncated]"
    return value


def _build_payload(
    event: str,
    *,
    session_id: str = "",
    cwd: Optional[str] = None,
    tool: Optional[str] = None,
    args: Optional[Dict[str, Any]] = None,
    result: Optional[str] = None,
    user_message: Optional[str] = None,
    final_response: Optional[str] = None,
    agent_name: Optional[str] = None,
    hermes_version: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "event": event,
        "session_id": session_id,
        "cwd": cwd or os.getcwd(),
        "hermes_version": hermes_version or os.environ.get("HERMES_VERSION", ""),
        "timestamp": time.time(),
    }
    if tool is not None:
        payload["tool"] = tool
    if args is not None:
        payload["args"] = args
    if result is not None:
        payload["result"] = _truncate(result, _RESULT_PAYLOAD_TRUNCATE)
    if user_message is not None:
        payload["user_message"] = _truncate(user_message, _RESULT_PAYLOAD_TRUNCATE)
    if final_response is not None:
        payload["final_response"] = _truncate(final_response, _RESULT_PAYLOAD_TRUNCATE)
    if agent_name is not None:
        payload["agent_name"] = agent_name
    if extra:
        for k, v in extra.items():
            payload.setdefault(k, v)
    return payload


def _run_one(
    spec: HookSpec,
    payload: Dict[str, Any],
    *,
    redactor: Optional[Callable[[str], str]] = None,
) -> Tuple[int, str, str]:
    """Run a single hook subprocess.  Returns (rc, stdout, stderr).

    On timeout or spawn failure, returns rc=124 (timeout) or rc=127
    (spawn failure) with a synthetic stderr — same convention shells use.
    """
    try:
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        return 127, "", f"hook: payload serialisation failed: {exc}"

    if redactor is not None:
        try:
            payload_json = redactor(payload_json)
        except Exception as exc:  # never let redaction errors block hooks
            logger.debug("Hook payload redactor raised: %s", exc)

    if isinstance(spec.command, str):
        proc_args: Union[str, Sequence[str]] = spec.command
        use_shell = True
    else:
        proc_args = list(spec.command)
        use_shell = False

    try:
        completed = subprocess.run(
            proc_args,
            input=payload_json,
            text=True,
            capture_output=True,
            timeout=spec.timeout,
            shell=use_shell,
            cwd=payload.get("cwd") or None,
        )
    except subprocess.TimeoutExpired as exc:
        partial_stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return 124, "", (f"hook: timed out after {spec.timeout}s — "
                         f"{(partial_stderr or '').strip()[:200]}".strip())
    except FileNotFoundError as exc:
        return 127, "", f"hook: command not found: {exc}"
    except OSError as exc:
        return 127, "", f"hook: spawn failed: {exc}"

    stdout = (completed.stdout or "")[:_MAX_STDOUT_BYTES]
    stderr = (completed.stderr or "")[:_MAX_STDERR_BYTES]
    return completed.returncode, stdout, stderr


def _interpret_stdout(stdout: str) -> Optional[Dict[str, Any]]:
    """Try to interpret stdout as a JSON decision object.  Returns None if not JSON."""
    stripped = stdout.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def run_hooks(
    registry: HookRegistry,
    event: str,
    *,
    session_id: str = "",
    cwd: Optional[str] = None,
    tool: Optional[str] = None,
    args: Optional[Dict[str, Any]] = None,
    result: Optional[str] = None,
    user_message: Optional[str] = None,
    final_response: Optional[str] = None,
    agent_name: Optional[str] = None,
    hermes_version: Optional[str] = None,
    redactor: Optional[Callable[[str], str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> HookOutcome:
    """Run every hook registered for ``event`` and aggregate the outcome.

    Hooks for the same event run in registration order (user-global first,
    then project), and the first block short-circuits.  Transform decisions
    accumulate: a later ``transform`` overwrites an earlier ``transform``
    on ``args``, and ``additional_context`` values are appended.
    """
    outcome = HookOutcome()
    if event not in ALL_EVENTS:
        outcome.errors.append(f"unknown event {event!r}")
        return outcome

    matching = registry.for_event(event, tool_name=tool)
    if not matching:
        return outcome

    payload = _build_payload(
        event,
        session_id=session_id,
        cwd=cwd,
        tool=tool,
        args=args,
        result=result,
        user_message=user_message,
        final_response=final_response,
        agent_name=agent_name,
        hermes_version=hermes_version,
        extra=extra,
    )

    current_args = args if isinstance(args, dict) else None

    for spec in matching:
        if outcome.blocked:
            break
        if outcome.transformed_args is not None:
            payload["args"] = outcome.transformed_args
            current_args = outcome.transformed_args
        rc, stdout, stderr = _run_one(spec, payload, redactor=redactor)
        outcome.ran.append(_short_command(spec.command))

        decision = _interpret_stdout(stdout)

        if rc == 0 and decision is None:
            continue  # allow

        if rc == 2:
            outcome.blocked = True
            outcome.block_reason = (
                (decision.get("reason") if isinstance(decision, dict) else None)
                or (stderr.strip() if stderr else "")
                or "blocked by hook"
            )
            break

        if decision is not None:
            kind = str(decision.get("decision", "")).lower()
            if kind == "block":
                outcome.blocked = True
                outcome.block_reason = (
                    decision.get("reason")
                    or stderr.strip()
                    or "blocked by hook"
                )
                break
            if kind == "transform":
                # Args replacement is only meaningful for PreToolUse.
                new_args = decision.get("args")
                if isinstance(new_args, dict) and event == EVENT_PRE_TOOL_USE:
                    outcome.transformed_args = new_args
                ctx = decision.get("additional_context")
                if isinstance(ctx, str) and ctx.strip():
                    outcome.additional_context.append(ctx.strip())
                continue
            if kind == "allow":
                ctx = decision.get("additional_context")
                if isinstance(ctx, str) and ctx.strip():
                    outcome.additional_context.append(ctx.strip())
                continue

        if rc != 0:
            outcome.errors.append(
                f"hook {_short_command(spec.command)} exited {rc}: "
                f"{(stderr or stdout).strip()[:300]}"
            )

    return outcome


def _short_command(command: Union[str, Sequence[str]]) -> str:
    if isinstance(command, str):
        first = command.split(maxsplit=1)[0] if command else ""
    else:
        first = command[0] if command else ""
    return Path(first).name or first or "<hook>"


# ---------------------------------------------------------------------------
# File-system loader
# ---------------------------------------------------------------------------


_PROJECT_HOOKS_FILENAME = "hooks.json"
_PROJECT_HOOKS_DIRNAME = ".hermes"


def _find_project_hooks_file(start_dir: Path) -> Optional[Path]:
    """Walk parents from ``start_dir`` looking for ``.hermes/hooks.json``.

    Stops at the first match.  Mirrors the discovery story used by
    ``.gitignore`` and most editor config files — convention-over-config
    avoids forcing a setup step on every new repo.
    """
    try:
        current = start_dir.resolve()
    except (OSError, RuntimeError):
        return None
    home = Path.home()
    seen: set[Path] = set()
    while current not in seen:
        seen.add(current)
        candidate = current / _PROJECT_HOOKS_DIRNAME / _PROJECT_HOOKS_FILENAME
        if candidate.is_file():
            return candidate
        # Don't walk above $HOME — a hooks.json there is the user-global
        # file, handled separately, and walking further up gives broad
        # surfaces (e.g. /home/) we shouldn't read.
        if current == home or current.parent == current:
            break
        current = current.parent
    return None


def _read_json_file(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"could not read {path}: {exc}"
    try:
        return json.loads(text), None
    except ValueError as exc:
        return None, f"invalid JSON in {path}: {exc}"


@dataclass
class HookLoadReport:
    """Diagnostic summary of a load_hook_registry call."""

    user_path: Optional[Path] = None
    user_loaded: bool = False
    user_error: Optional[str] = None
    project_path: Optional[Path] = None
    project_loaded: bool = False
    project_trusted: bool = False
    project_needs_trust: bool = False
    project_error: Optional[str] = None

    def warnings(self) -> List[str]:
        out: List[str] = []
        if self.user_error:
            out.append(self.user_error)
        if self.project_error:
            out.append(self.project_error)
        if self.project_path and self.project_needs_trust and not self.project_trusted:
            out.append(
                f"untrusted project hooks file at {self.project_path}: "
                f"run `hermes hooks trust` to enable"
            )
        return out


def load_hook_registry(
    *,
    cwd: Optional[Path] = None,
    user_hooks_path: Optional[Path] = None,
    auto_trust_project: bool = False,
) -> Tuple[HookRegistry, HookLoadReport]:
    """Discover and parse the user-global and project-level hooks files.

    Parameters
    ----------
    cwd
        Starting directory for project-file discovery.  Defaults to
        ``Path.cwd()``.
    user_hooks_path
        Path to the user-global hooks file.  Defaults to
        ``<hermes_home>/hooks.json``.
    auto_trust_project
        When True (used by tests and ``hermes hooks trust``), accept the
        project hooks file without consulting the trust store.

    Returns ``(registry, report)``.  Untrusted project files are skipped
    and surfaced via the report so the CLI can prompt the user once.
    """
    report = HookLoadReport()
    user_raw: Any = None
    project_raw: Any = None

    if user_hooks_path is None:
        from hermes_constants import get_hermes_home  # local import to avoid cycle
        user_hooks_path = get_hermes_home() / _PROJECT_HOOKS_FILENAME

    if user_hooks_path.is_file():
        report.user_path = user_hooks_path
        data, err = _read_json_file(user_hooks_path)
        if err:
            report.user_error = err
        else:
            user_raw = data
            report.user_loaded = True

    project_file = _find_project_hooks_file(cwd or Path.cwd())
    if project_file is not None:
        report.project_path = project_file
        report.project_needs_trust = True
        if auto_trust_project or is_path_trusted(project_file):
            report.project_trusted = True
            data, err = _read_json_file(project_file)
            if err:
                report.project_error = err
            else:
                project_raw = data
                report.project_loaded = True

    registry = HookRegistry.from_config(user_config=user_raw, project_config=project_raw)
    return registry, report


__all__ = [
    "ALL_EVENTS",
    "EVENT_PRE_TOOL_USE",
    "EVENT_POST_TOOL_USE",
    "EVENT_SESSION_END",
    "EVENT_SESSION_START",
    "EVENT_STOP",
    "EVENT_SUBAGENT_STOP",
    "EVENT_USER_PROMPT_SUBMIT",
    "HookLoadReport",
    "HookOutcome",
    "HookRegistry",
    "HookSpec",
    "is_path_trusted",
    "load_hook_registry",
    "parse_hook_config",
    "run_hooks",
    "trust_path",
]
