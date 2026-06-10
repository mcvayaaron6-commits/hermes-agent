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
        "prev_hash",
    }
    assert parsed["agent"] == "subagent"
    assert parsed["hermes_version"] == "0.13.0"
    # Default prev_hash is the genesis sentinel.
    assert parsed["prev_hash"] == al.GENESIS_PREV_HASH


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


# ---------------------------------------------------------------------------
# Hash-chain + HMAC tamper evidence
# ---------------------------------------------------------------------------


def test_chain_first_line_starts_at_genesis(enabled):
    al._reset_chain_state()
    al.write_event("first", session_id="s", data={"i": 1}, path=enabled)
    line = json.loads(enabled.read_text(encoding="utf-8").strip())
    assert line["prev_hash"] == al.GENESIS_PREV_HASH


def test_chain_subsequent_lines_link_to_prior(enabled):
    al._reset_chain_state()
    al.write_event("a", session_id="s", data={"i": 1}, path=enabled)
    al.write_event("b", session_id="s", data={"i": 2}, path=enabled)
    al.write_event("c", session_id="s", data={"i": 3}, path=enabled)
    lines = [json.loads(l) for l in enabled.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["prev_hash"] == al.GENESIS_PREV_HASH
    # Each prev_hash matches hash of prior raw line.
    raw_lines = enabled.read_text(encoding="utf-8").splitlines()
    for i in range(1, len(raw_lines)):
        expected = al._line_hash(raw_lines[i - 1])
        actual = lines[i]["prev_hash"]
        assert expected == actual, f"line {i}: prev_hash mismatch"


def test_verify_chain_intact(enabled):
    al._reset_chain_state()
    for i in range(5):
        al.write_event("E", session_id="s", data={"i": i}, path=enabled)
    result = al.verify_chain(path=enabled)
    assert result.ok
    assert result.lines_total == 5
    assert result.lines_ok == 5
    assert result.first_bad_line is None


def test_verify_chain_detects_truncated_front(enabled):
    al._reset_chain_state()
    for i in range(3):
        al.write_event("E", session_id="s", data={"i": i}, path=enabled)
    # Attacker deletes the first line.
    lines = enabled.read_text(encoding="utf-8").splitlines()
    enabled.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
    result = al.verify_chain(path=enabled)
    assert not result.ok
    assert result.first_bad_line == 1
    assert "prev_hash" in result.failure_reason.lower()


def test_verify_chain_detects_edited_middle(enabled):
    al._reset_chain_state()
    for i in range(5):
        al.write_event("E", session_id="s", data={"i": i}, path=enabled)
    # Attacker rewrites line 2.
    lines = enabled.read_text(encoding="utf-8").splitlines()
    edited = json.loads(lines[1])
    edited["data"]["i"] = 999
    lines[1] = json.dumps(edited, ensure_ascii=False)
    enabled.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = al.verify_chain(path=enabled)
    assert not result.ok
    # Line 2 itself still has the correct prev_hash, but line 3's
    # prev_hash no longer matches the (now-edited) line 2's hash.
    assert result.first_bad_line == 3


def test_verify_chain_detects_deleted_middle(enabled):
    al._reset_chain_state()
    for i in range(5):
        al.write_event("E", session_id="s", data={"i": i}, path=enabled)
    lines = enabled.read_text(encoding="utf-8").splitlines()
    del lines[2]  # remove line 3
    enabled.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = al.verify_chain(path=enabled)
    assert not result.ok
    assert result.first_bad_line is not None


def test_verify_chain_malformed_line_caught(enabled):
    al._reset_chain_state()
    al.write_event("a", session_id="s", data={"i": 1}, path=enabled)
    with enabled.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
    result = al.verify_chain(path=enabled)
    assert not result.ok
    assert "JSON" in result.failure_reason


def test_hmac_signing_when_key_supplied(enabled, monkeypatch):
    al._reset_chain_state()
    monkeypatch.setenv("HERMES_AUDIT_HMAC_KEY", "deadbeef" * 8)
    al.write_event("E", session_id="s", data={"i": 1}, path=enabled)
    line = json.loads(enabled.read_text(encoding="utf-8").strip())
    assert "sig" in line
    assert len(line["sig"]) == 64  # SHA-256 hex


def test_hmac_omitted_when_no_key(enabled, monkeypatch):
    al._reset_chain_state()
    monkeypatch.delenv("HERMES_AUDIT_HMAC_KEY", raising=False)
    al.write_event("E", session_id="s", data={"i": 1}, path=enabled)
    line = json.loads(enabled.read_text(encoding="utf-8").strip())
    assert "sig" not in line


def test_verify_chain_hmac_match(enabled, monkeypatch):
    al._reset_chain_state()
    key_hex = "0123456789abcdef" * 4
    monkeypatch.setenv("HERMES_AUDIT_HMAC_KEY", key_hex)
    for i in range(3):
        al.write_event("E", session_id="s", data={"i": i}, path=enabled)
    result = al.verify_chain(path=enabled)
    assert result.ok
    assert result.sig_checked
    assert result.sig_ok == 3


def test_verify_chain_hmac_mismatch_detected(enabled, monkeypatch):
    al._reset_chain_state()
    monkeypatch.setenv("HERMES_AUDIT_HMAC_KEY", "aa" * 32)
    for i in range(3):
        al.write_event("E", session_id="s", data={"i": i}, path=enabled)
    # Attacker tampers with content AND fixes the prev_hash chain
    # (recomputes hashes downstream), but DOESN'T have the HMAC key.
    lines = enabled.read_text(encoding="utf-8").splitlines()
    line_dict = json.loads(lines[1])
    line_dict["data"]["i"] = 999
    # Note: attacker can't recompute the sig without the key.
    # They might leave the old sig (HMAC check fails) or omit it.
    # Either way, verification detects.
    lines[1] = json.dumps(line_dict, ensure_ascii=False)
    # Recompute prev_hashes downstream so the chain itself passes.
    for j in range(2, len(lines)):
        downstream = json.loads(lines[j])
        downstream["prev_hash"] = al._line_hash(lines[j - 1])
        lines[j] = json.dumps(downstream, ensure_ascii=False)
    enabled.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = al.verify_chain(path=enabled)
    assert not result.ok
    assert "HMAC" in result.failure_reason or "signature" in result.failure_reason


def test_verify_chain_missing_file(tmp_path):
    result = al.verify_chain(path=tmp_path / "missing.jsonl")
    assert not result.ok
    assert "not found" in result.failure_reason


def test_signing_key_from_file(enabled, monkeypatch, tmp_path):
    al._reset_chain_state()
    monkeypatch.delenv("HERMES_AUDIT_HMAC_KEY", raising=False)
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(b"my-secret-key-bytes")
    monkeypatch.setattr(al, "_audit_config",
                        lambda: {"enabled": True, "hmac_key_file": str(key_file)})
    key = al._signing_key()
    assert key == b"my-secret-key-bytes"
