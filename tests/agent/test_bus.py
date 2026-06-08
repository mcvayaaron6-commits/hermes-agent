"""Tests for the inter-agent bus."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.bus import (
    AgentSpawnedEvent,
    Bus,
    BusEvent,
    FileLockAcquiredEvent,
    LessonCapturedEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
    get_bus,
    publish_agent_spawned,
    publish_lesson_captured,
    publish_tool_finished,
    publish_tool_started,
    reset_default_bus,
)


@pytest.fixture
def bus():
    b = Bus(max_workers=2)
    yield b
    b.close()


def _wait_for(predicate, timeout=2.0, poll=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


# ---------------------------------------------------------------------------
# Subscribe paths
# ---------------------------------------------------------------------------


def test_exact_subject_delivery(bus):
    received = []
    bus.subscribe(received.append, subject="agent.42.tool_started")
    bus.publish(ToolStartedEvent(
        subject="agent.42.tool_started", agent_id="42", tool_name="write_file",
    ), sync=True)
    assert len(received) == 1
    assert received[0].agent_id == "42"


def test_pattern_subject_delivery(bus):
    received = []
    bus.subscribe(received.append, pattern="agent.*.tool_started")
    for aid in ("1", "2", "3"):
        bus.publish(ToolStartedEvent(
            subject=f"agent.{aid}.tool_started", agent_id=aid, tool_name="x",
        ), sync=True)
    assert {e.agent_id for e in received} == {"1", "2", "3"}


def test_pattern_does_not_match_unrelated(bus):
    received = []
    bus.subscribe(received.append, pattern="agent.*.tool_started")
    bus.publish(ToolFinishedEvent(
        subject="agent.42.tool_finished", agent_id="42", tool_name="x",
        duration_ms=10.0,
    ), sync=True)
    assert received == []


def test_typed_delivery(bus):
    received_started = []
    received_finished = []
    bus.subscribe(received_started.append, event_type=ToolStartedEvent)
    bus.subscribe(received_finished.append, event_type=ToolFinishedEvent)
    bus.publish(ToolStartedEvent(
        subject="agent.1.tool_started", agent_id="1", tool_name="x",
    ), sync=True)
    bus.publish(ToolFinishedEvent(
        subject="agent.1.tool_finished", agent_id="1", tool_name="x",
        duration_ms=5.0,
    ), sync=True)
    assert len(received_started) == 1
    assert len(received_finished) == 1


def test_subscribe_requires_exactly_one_selector(bus):
    with pytest.raises(ValueError):
        bus.subscribe(lambda e: None)
    with pytest.raises(ValueError):
        bus.subscribe(lambda e: None, subject="a", pattern="b")


# ---------------------------------------------------------------------------
# Unsubscribe
# ---------------------------------------------------------------------------


def test_unsubscribe_removes_handler(bus):
    received = []
    unsub = bus.subscribe(received.append, subject="topic.a")
    bus.publish(BusEvent(subject="topic.a"), sync=True)
    assert len(received) == 1
    unsub()
    bus.publish(BusEvent(subject="topic.a"), sync=True)
    assert len(received) == 1  # no new delivery


def test_double_unsubscribe_is_safe(bus):
    unsub = bus.subscribe(lambda e: None, subject="x")
    unsub()
    unsub()  # must not raise


# ---------------------------------------------------------------------------
# Async dispatch
# ---------------------------------------------------------------------------


def test_async_dispatch_does_not_block_publisher(bus):
    barrier = threading.Event()
    started_at = []

    def slow_handler(event):
        started_at.append(time.monotonic())
        barrier.wait(2.0)

    bus.subscribe(slow_handler, subject="slow.topic")
    t0 = time.monotonic()
    bus.publish(BusEvent(subject="slow.topic"))
    publish_returned = time.monotonic()
    barrier.set()
    # Publisher must have returned without waiting for the handler.
    assert publish_returned - t0 < 1.0


def test_async_handler_exception_doesnt_break_bus(bus):
    received = []

    def bad(e):
        raise RuntimeError("boom")
    def good(e):
        received.append(e)

    bus.subscribe(bad, subject="topic")
    bus.subscribe(good, subject="topic")
    bus.publish(BusEvent(subject="topic"), sync=True)
    assert len(received) == 1


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_history_returns_recent_events(bus):
    for i in range(5):
        bus.publish(BusEvent(subject="history.test"), sync=True)
    hist = bus.history("history.test", limit=3)
    assert len(hist) == 3


def test_history_per_subject_isolated(bus):
    bus.publish(BusEvent(subject="a"), sync=True)
    bus.publish(BusEvent(subject="b"), sync=True)
    bus.publish(BusEvent(subject="a"), sync=True)
    assert len(bus.history("a")) == 2
    assert len(bus.history("b")) == 1


def test_subjects_lists_seen_topics(bus):
    bus.publish(BusEvent(subject="agent.1.x"), sync=True)
    bus.publish(BusEvent(subject="lesson.captured"), sync=True)
    seen = set(bus.subjects())
    assert "agent.1.x" in seen
    assert "lesson.captured" in seen


# ---------------------------------------------------------------------------
# Convenience publishers + global bus
# ---------------------------------------------------------------------------


def test_convenience_publishers_round_trip():
    reset_default_bus()
    started, finished, spawned, lesson = [], [], [], []
    bus = get_bus()
    bus.subscribe(started.append, event_type=ToolStartedEvent)
    bus.subscribe(finished.append, event_type=ToolFinishedEvent)
    bus.subscribe(spawned.append, event_type=AgentSpawnedEvent)
    bus.subscribe(lesson.append, event_type=LessonCapturedEvent)

    publish_tool_started("a-1", "write_file", args_preview="{path: 'x.txt'}")
    publish_tool_finished("a-1", "write_file", duration_ms=42.0)
    publish_agent_spawned("a-2", "a-1", "code-reviewer", "review src/auth.ts",
                          toolsets=("file", "search"))
    publish_lesson_captured("/tmp/lesson.md", "Fix OAuth", ["oauth", "auth"])

    assert _wait_for(lambda: started and finished and spawned and lesson)
    assert started[0].tool_name == "write_file"
    assert finished[0].duration_ms == 42.0
    assert spawned[0].subagent_type == "code-reviewer"
    assert "oauth" in lesson[0].tags
    reset_default_bus()


def test_publish_rejects_non_busevent(bus):
    with pytest.raises(TypeError):
        bus.publish("not an event")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Closed bus is harmless
# ---------------------------------------------------------------------------


def test_publish_after_close_is_silent():
    b = Bus()
    b.close()
    # Must not raise — closed bus drops events.
    b.publish(BusEvent(subject="x"))


def test_get_bus_returns_singleton():
    reset_default_bus()
    a = get_bus()
    b = get_bus()
    assert a is b
    reset_default_bus()


# ---------------------------------------------------------------------------
# Bus → audit log integration
# ---------------------------------------------------------------------------


def test_publish_writes_to_audit_log_when_enabled(tmp_path, monkeypatch):
    """Bus events flow into the tamper-evident audit chain when
    audit.enabled is true.  This is the single tamper-evident trace
    operators rely on for compliance."""
    from agent import audit_log as al
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(al, "_audit_config",
                        lambda: {"enabled": True, "path": str(audit_path)})
    al._reset_chain_state()
    b = Bus()
    try:
        b.publish(ToolStartedEvent(
            subject="agent.42.tool_started",
            agent_id="42", tool_name="write_file",
        ), sync=True)
    finally:
        b.close()
    # Audit file got the event.
    assert audit_path.exists()
    import json as _json
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = _json.loads(lines[0])
    assert entry["event"] == "ToolStartedEvent"
    assert entry["data"]["agent_id"] == "42"
    assert entry["data"]["tool_name"] == "write_file"


def test_publish_silent_when_audit_disabled(tmp_path, monkeypatch):
    from agent import audit_log as al
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(al, "_audit_config",
                        lambda: {"enabled": False, "path": str(audit_path)})
    al._reset_chain_state()
    b = Bus()
    try:
        b.publish(ToolStartedEvent(
            subject="x", agent_id="1", tool_name="x",
        ), sync=True)
    finally:
        b.close()
    # No audit file written.
    assert not audit_path.exists()


def test_audit_write_failure_does_not_block_dispatch(tmp_path, monkeypatch):
    """A broken audit log can't stop the bus from delivering to subscribers."""
    from agent import audit_log as al
    # Point audit to a path that will fail to write (parent file blocks dir creation).
    bad = tmp_path / "not-a-dir"
    bad.touch()  # exists as file, can't be a dir
    monkeypatch.setattr(al, "_audit_config",
                        lambda: {"enabled": True, "path": str(bad / "audit.jsonl")})
    al._reset_chain_state()
    received = []
    b = Bus()
    try:
        b.subscribe(received.append, subject="topic.x")
        b.publish(BusEvent(subject="topic.x"), sync=True)
    finally:
        b.close()
    assert len(received) == 1
