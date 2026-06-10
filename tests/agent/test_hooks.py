"""Tests for the user-defined hooks engine."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent import hooks as hooks_mod
from agent.hooks import (
    ALL_EVENTS,
    EVENT_POST_TOOL_USE,
    EVENT_PRE_TOOL_USE,
    EVENT_SESSION_START,
    EVENT_STOP,
    EVENT_USER_PROMPT_SUBMIT,
    HookRegistry,
    HookSpec,
    is_path_trusted,
    load_hook_registry,
    parse_hook_config,
    run_hooks,
    trust_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_script(tmp_path: Path, name: str, body: str) -> Path:
    """Write an executable shell script and return its absolute path."""
    script = tmp_path / name
    script.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.fixture(autouse=True)
def _isolated_hermes_home(monkeypatch, tmp_path):
    """Redirect hermes_home so the trust store doesn't bleed across tests."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_TRUSTED_HOOK_FILES", raising=False)
    yield home


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_parse_hook_config_known_events():
    raw = {
        "PreToolUse": [
            {"matcher": "write_file", "command": "true"},
        ],
        "Stop": [
            {"command": "echo done"},
        ],
    }
    specs = parse_hook_config(raw, source="user")
    assert len(specs) == 2
    by_event = {s.event: s for s in specs}
    assert by_event[EVENT_PRE_TOOL_USE].matcher == "write_file"
    assert by_event[EVENT_STOP].matcher is None


def test_parse_hook_config_unknown_event_skipped(caplog):
    raw = {"NotARealEvent": [{"command": "true"}]}
    with caplog.at_level("WARNING"):
        specs = parse_hook_config(raw, source="user")
    assert specs == []
    assert any("Unknown hook event" in r.message for r in caplog.records)


def test_parse_hook_config_skips_malformed_entries():
    raw = {
        "Stop": [
            {"command": "echo ok"},
            {"matcher": "x"},  # no command
            "not-a-dict",
            {"command": 42},   # not str/list
        ],
    }
    specs = parse_hook_config(raw, source="user")
    assert len(specs) == 1


def test_parse_hook_config_timeout_clamped():
    raw = {"Stop": [{"command": "x", "timeout": -10}]}
    spec = parse_hook_config(raw, source="user")[0]
    assert spec.timeout == 1
    raw = {"Stop": [{"command": "x", "timeout": 10000}]}
    spec = parse_hook_config(raw, source="user")[0]
    assert spec.timeout == 600


def test_hook_spec_matcher_regex():
    spec = HookSpec(event=EVENT_PRE_TOOL_USE, command="true", matcher="write_file|patch")
    assert spec.matches_tool("write_file")
    assert spec.matches_tool("patch")
    assert not spec.matches_tool("memory")


def test_hook_spec_invalid_regex_does_not_raise():
    spec = HookSpec(event=EVENT_PRE_TOOL_USE, command="true", matcher="[unterminated")
    assert spec.matches_tool("anything") is False


def test_hook_spec_empty_matcher_matches_all():
    spec = HookSpec(event=EVENT_PRE_TOOL_USE, command="true", matcher=None)
    assert spec.matches_tool("anything")
    spec2 = HookSpec(event=EVENT_PRE_TOOL_USE, command="true", matcher="")
    assert spec2.matches_tool("anything")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_summary_and_describe():
    raw = {"PreToolUse": [{"command": "x"}], "Stop": [{"command": "y"}, {"command": "z"}]}
    reg = HookRegistry.from_config(user_config=raw)
    summary = reg.summary()
    assert summary[EVENT_PRE_TOOL_USE] == 1
    assert summary[EVENT_STOP] == 2
    desc = reg.describe()
    assert {d["event"] for d in desc} == {EVENT_PRE_TOOL_USE, EVENT_STOP}


def test_registry_for_event_filters_by_matcher():
    raw = {
        "PreToolUse": [
            {"matcher": "write_file", "command": "x"},
            {"matcher": "memory", "command": "y"},
            {"command": "z"},  # matches all
        ]
    }
    reg = HookRegistry.from_config(user_config=raw)
    matches = reg.for_event(EVENT_PRE_TOOL_USE, tool_name="write_file")
    assert len(matches) == 2
    commands = [m.command for m in matches]
    assert "x" in commands and "z" in commands


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def test_run_hooks_allow_when_exit_zero(tmp_path):
    script = _make_script(tmp_path, "allow.sh", "exit 0\n")
    reg = HookRegistry([HookSpec(event=EVENT_STOP, command=str(script))])
    outcome = run_hooks(reg, EVENT_STOP, final_response="hello")
    assert outcome.ok
    assert outcome.blocked is False
    assert outcome.ran


def test_run_hooks_block_on_exit_2(tmp_path):
    script = _make_script(tmp_path, "block.sh",
                          'echo "bad input" >&2\nexit 2\n')
    reg = HookRegistry([HookSpec(event=EVENT_PRE_TOOL_USE, command=str(script))])
    outcome = run_hooks(reg, EVENT_PRE_TOOL_USE, tool="write_file", args={"path": "x"})
    assert outcome.blocked
    assert outcome.block_reason
    assert "bad input" in outcome.block_reason


def test_run_hooks_transform_args(tmp_path):
    script = _make_script(
        tmp_path,
        "transform.sh",
        'echo \'{"decision": "transform", "args": {"path": "rewritten.txt"}}\'\n'
        "exit 0\n",
    )
    reg = HookRegistry([HookSpec(event=EVENT_PRE_TOOL_USE, command=str(script))])
    outcome = run_hooks(reg, EVENT_PRE_TOOL_USE, tool="write_file", args={"path": "orig.txt"})
    assert outcome.blocked is False
    assert outcome.transformed_args == {"path": "rewritten.txt"}


def test_run_hooks_transform_args_ignored_for_non_pretooluse(tmp_path):
    script = _make_script(
        tmp_path,
        "transform.sh",
        'echo \'{"decision": "transform", "args": {"x": 1}}\'\n',
    )
    reg = HookRegistry([HookSpec(event=EVENT_STOP, command=str(script))])
    outcome = run_hooks(reg, EVENT_STOP, final_response="done")
    assert outcome.transformed_args is None


def test_run_hooks_block_via_stdout_decision(tmp_path):
    script = _make_script(
        tmp_path,
        "block.sh",
        'echo \'{"decision": "block", "reason": "policy violation"}\'\n',
    )
    reg = HookRegistry([HookSpec(event=EVENT_PRE_TOOL_USE, command=str(script))])
    outcome = run_hooks(reg, EVENT_PRE_TOOL_USE, tool="write_file", args={})
    assert outcome.blocked
    assert outcome.block_reason == "policy violation"


def test_run_hooks_additional_context_accumulates(tmp_path):
    s1 = _make_script(tmp_path, "a.sh",
                      'echo \'{"decision": "allow", "additional_context": "ctx-a"}\'\n')
    s2 = _make_script(tmp_path, "b.sh",
                      'echo \'{"decision": "allow", "additional_context": "ctx-b"}\'\n')
    reg = HookRegistry([
        HookSpec(event=EVENT_POST_TOOL_USE, command=str(s1)),
        HookSpec(event=EVENT_POST_TOOL_USE, command=str(s2)),
    ])
    outcome = run_hooks(reg, EVENT_POST_TOOL_USE, tool="write_file", result="ok")
    assert outcome.additional_context == ["ctx-a", "ctx-b"]


@pytest.mark.live_system_guard_bypass
def test_run_hooks_timeout(tmp_path):
    script = _make_script(tmp_path, "slow.sh", "sleep 5\n")
    reg = HookRegistry([HookSpec(event=EVENT_STOP, command=str(script), timeout=1)])
    outcome = run_hooks(reg, EVENT_STOP, final_response="done")
    # Timeout is a non-fatal hook error, not a block.
    assert outcome.errors
    assert any("timed out" in e for e in outcome.errors)
    assert not outcome.blocked


def test_run_hooks_command_not_found_records_error():
    reg = HookRegistry([HookSpec(event=EVENT_STOP,
                                 command=["/does-not-exist/agent-hook"])])
    outcome = run_hooks(reg, EVENT_STOP, final_response="done")
    assert outcome.errors
    assert any("command not found" in e or "spawn failed" in e for e in outcome.errors)
    assert not outcome.blocked


def test_run_hooks_payload_includes_event_and_tool(tmp_path):
    script = _make_script(
        tmp_path,
        "echo.sh",
        # Capture stdin to a sibling file so the test can assert on it.
        f"cat - > {tmp_path/'captured.json'}\nexit 0\n",
    )
    reg = HookRegistry([HookSpec(event=EVENT_PRE_TOOL_USE, command=str(script))])
    run_hooks(
        reg,
        EVENT_PRE_TOOL_USE,
        tool="write_file",
        args={"path": "a.txt"},
        session_id="sess-123",
    )
    payload = json.loads((tmp_path / "captured.json").read_text(encoding="utf-8"))
    assert payload["event"] == EVENT_PRE_TOOL_USE
    assert payload["tool"] == "write_file"
    assert payload["args"] == {"path": "a.txt"}
    assert payload["session_id"] == "sess-123"


def test_run_hooks_unknown_event_returns_error():
    reg = HookRegistry()
    outcome = run_hooks(reg, "NotAnEvent")
    assert outcome.errors


def test_run_hooks_no_matching_specs_is_noop():
    reg = HookRegistry([HookSpec(event=EVENT_PRE_TOOL_USE, command="true",
                                 matcher="memory")])
    outcome = run_hooks(reg, EVENT_PRE_TOOL_USE, tool="write_file", args={})
    assert outcome.ok
    assert outcome.ran == []


def test_run_hooks_redactor_applied(tmp_path):
    captured = tmp_path / "captured.txt"
    script = _make_script(tmp_path, "echo.sh",
                          f"cat - > {captured}\nexit 0\n")
    reg = HookRegistry([HookSpec(event=EVENT_STOP, command=str(script))])

    def redactor(text: str) -> str:
        return text.replace("super-secret", "[REDACTED]")

    run_hooks(reg, EVENT_STOP, final_response="hello super-secret world",
              redactor=redactor)
    body = captured.read_text(encoding="utf-8")
    assert "[REDACTED]" in body
    assert "super-secret" not in body


def test_run_hooks_block_short_circuits_subsequent(tmp_path):
    blocker = _make_script(tmp_path, "block.sh", "exit 2\n")
    later = _make_script(tmp_path, "later.sh",
                         f"touch {tmp_path/'later-ran'}\nexit 0\n")
    reg = HookRegistry([
        HookSpec(event=EVENT_PRE_TOOL_USE, command=str(blocker)),
        HookSpec(event=EVENT_PRE_TOOL_USE, command=str(later)),
    ])
    outcome = run_hooks(reg, EVENT_PRE_TOOL_USE, tool="write_file", args={})
    assert outcome.blocked
    assert not (tmp_path / "later-ran").exists()


# ---------------------------------------------------------------------------
# Loader & trust store
# ---------------------------------------------------------------------------


def test_load_hook_registry_user_only(tmp_path, _isolated_hermes_home):
    user_file = _write(_isolated_hermes_home / "hooks.json",
                       json.dumps({"Stop": [{"command": "echo from-user"}]}))
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    reg, report = load_hook_registry(cwd=project_dir, user_hooks_path=user_file)
    assert len(reg) == 1
    assert report.user_loaded
    assert report.project_path is None


def test_load_hook_registry_project_requires_trust(tmp_path, _isolated_hermes_home):
    project_dir = tmp_path / "project"
    hooks_file = _write(project_dir / ".hermes" / "hooks.json",
                        json.dumps({"Stop": [{"command": "echo project"}]}))
    user_file = _isolated_hermes_home / "hooks.json"  # absent

    reg, report = load_hook_registry(cwd=project_dir, user_hooks_path=user_file)
    assert len(reg) == 0
    assert report.project_path == hooks_file
    assert report.project_needs_trust
    assert not report.project_trusted

    trust_path(hooks_file)
    reg2, report2 = load_hook_registry(cwd=project_dir, user_hooks_path=user_file)
    assert len(reg2) == 1
    assert report2.project_trusted


def test_load_hook_registry_auto_trust_bypasses(tmp_path, _isolated_hermes_home):
    project_dir = tmp_path / "project"
    hooks_file = _write(project_dir / ".hermes" / "hooks.json",
                        json.dumps({"Stop": [{"command": "echo project"}]}))
    user_file = _isolated_hermes_home / "hooks.json"

    reg, report = load_hook_registry(
        cwd=project_dir,
        user_hooks_path=user_file,
        auto_trust_project=True,
    )
    assert len(reg) == 1
    assert report.project_trusted


def test_load_hook_registry_invalid_json_surfaced(tmp_path, _isolated_hermes_home):
    user_file = _write(_isolated_hermes_home / "hooks.json", "{ not json")
    reg, report = load_hook_registry(cwd=tmp_path, user_hooks_path=user_file)
    assert len(reg) == 0
    assert report.user_error
    assert "invalid JSON" in report.user_error


def test_trust_store_invalidates_on_content_change(tmp_path, _isolated_hermes_home):
    project_dir = tmp_path / "proj"
    hooks_file = _write(project_dir / ".hermes" / "hooks.json",
                        json.dumps({"Stop": [{"command": "old"}]}))
    trust_path(hooks_file)
    assert is_path_trusted(hooks_file)
    hooks_file.write_text(json.dumps({"Stop": [{"command": "new"}]}), encoding="utf-8")
    assert not is_path_trusted(hooks_file)


def test_env_var_pre_trust(tmp_path, _isolated_hermes_home, monkeypatch):
    project_dir = tmp_path / "proj"
    hooks_file = _write(project_dir / ".hermes" / "hooks.json",
                        json.dumps({"Stop": [{"command": "x"}]}))
    monkeypatch.setenv("HERMES_TRUSTED_HOOK_FILES", str(hooks_file))
    user_file = _isolated_hermes_home / "hooks.json"
    reg, report = load_hook_registry(cwd=project_dir, user_hooks_path=user_file)
    assert len(reg) == 1
    assert report.project_trusted


# ---------------------------------------------------------------------------
# Sanity guards
# ---------------------------------------------------------------------------


def test_all_events_constant_matches_event_names():
    assert set(ALL_EVENTS) == {
        EVENT_SESSION_START,
        EVENT_USER_PROMPT_SUBMIT,
        EVENT_PRE_TOOL_USE,
        EVENT_POST_TOOL_USE,
        EVENT_STOP,
        "SubagentStop",
        "SessionEnd",
    }
