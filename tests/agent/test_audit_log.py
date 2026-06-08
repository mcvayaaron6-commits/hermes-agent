"""Tests for the audit-log module."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent import audit_log as al


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    """Force audit.enabled=true and route writes to a tmp path."""
    monkeypatch.setattr(al, "_audit_config", lambda: {"enabled": True})
    return tmp_path / "audit.jsonl"


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.setattr(al, "_audit_config", lambda: {"enabled": False})
    return None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def test_write_event_writes_one_line(enabled):
    ok = al.write_event(
        al.EVENT_PRE_TOOL_USE,
        session_id="s-1",
        data={"tool": "write_file", "args": {"path": "x.txt"}},
        path=enabled,
    )
    assert ok
    content = enabled.read_text(encoding="utf-8")
    assert content.endswith("\n")
    line = content.strip().splitlines()[0]
    parsed = json.loads(line)
    assert parsed["event"] == al.EVENT_PRE_TOOL_USE
    assert parsed["session_id"] == "s-1"
    assert parsed["data"]["tool"] == "write_file"
    assert "ts" in parsed
    # Timestamp must be ISO-format with timezone
    assert parsed["ts"].endswith("+00:00")


def test_write_event_appends_subsequent_events(enabled):
    al.write_event("E1", session_id="s", data={"i": 1}, path=enabled)
    al.write_event("E2", session_id="s", data={"i": 2}, path=enabled)
    lines = enabled.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["data"]["i"] == 1
    assert json.loads(lines[1])["data"]["i"] == 2


def test_write_event_no_op_when_disabled(disabled, tmp_path):
    path = tmp_path / "audit.jsonl"
    ok = al.write_event("E1", session_id="s", data={"x": 1}, path=path)
    assert ok is False
    assert not path.exists()


def test_write_event_creates_parent_dir(enabled, tmp_path):
    nested = tmp_path / "deeply" / "nested" / "audit.jsonl"
    ok = al.write_event("E1", session_id="s", data={}, path=nested)
    assert ok
    assert nested.exists()


def test_write_event_truncates_oversize_payloads(enabled):
    big = "x" * (al._MAX_FIELD_BYTES + 1000)
    al.write_event("E1", session_id="s", data={"big": big}, path=enabled)
    parsed = json.loads(enabled.read_text(encoding="utf-8").strip())
    assert "truncated" in parsed["data"]["big"]
    assert len(parsed["data"]["big"]) <= al._MAX_FIELD_BYTES


def test_write_event_applies_redactor(enabled):
    def red(s: str) -> str:
        return s.replace("api-key-12345", "[REDACTED]")
    al.write_event(
        "E1", session_id="s",
        data={"command": "curl -H 'Authorization: api-key-12345'"},
        path=enabled, redactor=red,
    )
    parsed = json.loads(enabled.read_text(encoding="utf-8").strip())
    assert "[REDACTED]" in parsed["data"]["command"]
    assert "api-key-12345" not in parsed["data"]["command"]


def test_write_event_redactor_recurses_into_nested_dicts(enabled):
    def red(s: str) -> str:
        return s.replace("secret", "[X]")
    al.write_event(
        "E1", session_id="s",
        data={"outer": {"inner": "a secret value", "list": ["secret"]}},
        path=enabled, redactor=red,
    )
    parsed = json.loads(enabled.read_text(encoding="utf-8").strip())
    assert "[X]" in parsed["data"]["outer"]["inner"]
    assert parsed["data"]["outer"]["list"] == ["[X]"]


def test_write_event_non_serialisable_fallback(enabled):
    class Weird:
        def __repr__(self):
            return "<Weird>"
    al.write_event("E1", session_id="s", data={"obj": Weird()}, path=enabled)
    parsed = json.loads(enabled.read_text(encoding="utf-8").strip())
    # default=str in json.dumps converts unknown objects to repr
    assert "<Weird>" in str(parsed["data"]["obj"])


def test_write_event_returns_false_on_permission_error(enabled, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("permission denied")
    monkeypatch.setattr("pathlib.Path.open", boom)
    ok = al.write_event("E1", session_id="s", data={}, path=enabled)
    assert ok is False


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def test_tail_events_returns_last_n(enabled):
    for i in range(10):
        al.write_event("E", session_id="s", data={"i": i}, path=enabled)
    last3 = al.tail_events(n=3, path=enabled)
    assert len(last3) == 3
    assert [e["data"]["i"] for e in last3] == [7, 8, 9]


def test_tail_events_handles_missing_file(tmp_path):
    assert al.tail_events(n=5, path=tmp_path / "missing.jsonl") == []


def test_tail_events_skips_malformed_lines(enabled):
    enabled.parent.mkdir(parents=True, exist_ok=True)
    enabled.write_text(
        '{"event": "ok"}\n'
        'not json at all\n'
        '\n'  # blank
        '{"event": "ok2"}\n',
        encoding="utf-8",
    )
    events = al.tail_events(n=10, path=enabled)
    assert len(events) == 2
    assert events[0]["event"] == "ok"
    assert events[1]["event"] == "ok2"


def test_count_events_by_type(enabled):
    for event in ["PreToolUse", "PreToolUse", "PostToolUse",
                  "Stop", "PreToolUse"]:
        al.write_event(event, session_id="s", data={}, path=enabled)
    counts = al.count_events_by_type(path=enabled)
    assert counts == {"PreToolUse": 3, "PostToolUse": 1, "Stop": 1}


# ---------------------------------------------------------------------------
# Format
# ---------------------------------------------------------------------------


def test_format_line_includes_required_keys(enabled):
    line = al._format_line(
        "E", session_id="s-1", data={"x": 1},
        agent="subagent", hermes_version="0.13.0",
    )
    parsed = json.loads(line)
    assert set(parsed.keys()) == {
        "ts", "session_id", "event", "agent", "hermes_version", "data",
    }
    assert parsed["agent"] == "subagent"
    assert parsed["hermes_version"] == "0.13.0"


def test_format_line_handles_unicode(enabled):
    line = al._format_line(
        "E", session_id="s",
        data={"prompt": "日本語 + emoji 🎉"},
    )
    parsed = json.loads(line)
    assert parsed["data"]["prompt"] == "日本語 + emoji 🎉"


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_resolve_log_path_uses_explicit_when_provided(monkeypatch, tmp_path):
    explicit = tmp_path / "custom" / "audit.jsonl"
    monkeypatch.setattr(al, "_audit_config",
                        lambda: {"enabled": True, "path": str(explicit)})
    assert al._resolve_log_path() == explicit.resolve()
