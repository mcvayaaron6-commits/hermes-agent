"""
Agent Bus — typed in-process pub/sub for inter-agent coordination.

The fundamental missing primitive in Hermes: when multiple subagents
run in parallel via ``delegate_task``, they can't talk to each other
or to the parent.  Today's parent only sees children when they finish;
children can't subscribe to sibling events, share intermediate work,
or coordinate on shared resources.

This module ships the in-process bus.  A future commit can swap in
NATS/Redis behind the same ``Bus`` protocol for cross-process /
cross-machine coordination — that's why the surface is small and the
subjects are strings, not Python objects.

Design
------

* **Subjects** are hierarchical dotted strings: ``agent.<id>.tool_started``,
  ``agent.<id>.tool_finished``, ``file.<path>.write_acquired``,
  ``lesson.<id>.captured``, ``orchestrator.task.<id>.completed``.
* **Subscribers** match subjects via prefix or wildcard (``agent.*.tool_started``).
* **Publish** is non-blocking — handlers run on a background thread pool
  so a slow subscriber can't stall the publisher.
* **Order** is per-subject FIFO; cross-subject ordering is not guaranteed.
* **No durability** in-process — events fly while the process lives.
  The audit log is the durable counterpart.

Why typed?
----------

Every event has a stable Python dataclass payload (``ToolStartedEvent``,
``FileLockedEvent``, ...).  Subscribers register against the type, not
the string subject — fewer string-typo bugs, IDE autocomplete in
plugin code, and a clear schema doc.  The subject is just the
serialised name for cross-process transports.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Type

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event dataclasses — the typed payload surface
# ---------------------------------------------------------------------------


@dataclass
class BusEvent:
    """Base class for every event published on the bus."""
    subject: str
    ts: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentSpawnedEvent(BusEvent):
    """A subagent has been constructed and is about to run."""
    agent_id: str = ""
    parent_agent_id: Optional[str] = None
    subagent_type: Optional[str] = None
    goal: str = ""
    toolsets: tuple = ()


@dataclass
class AgentFinishedEvent(BusEvent):
    """A subagent has completed (success or failure)."""
    agent_id: str = ""
    parent_agent_id: Optional[str] = None
    success: bool = True
    final_response: str = ""
    iterations_used: int = 0


@dataclass
class ToolStartedEvent(BusEvent):
    """An agent is about to invoke a tool."""
    agent_id: str = ""
    tool_name: str = ""
    args_preview: str = ""


@dataclass
class ToolFinishedEvent(BusEvent):
    """An agent's tool invocation completed."""
    agent_id: str = ""
    tool_name: str = ""
    duration_ms: float = 0.0
    success: bool = True
    error: Optional[str] = None


@dataclass
class FileLockAcquiredEvent(BusEvent):
    """Optimistic write lock acquired on a file path (single-machine).

    Used by the file-lock coordinator to broadcast that a path is
    being modified — other agents can listen and back off.
    """
    agent_id: str = ""
    path: str = ""
    intent: str = "write"  # "write" | "exclusive" | "shared"


@dataclass
class FileLockReleasedEvent(BusEvent):
    agent_id: str = ""
    path: str = ""


@dataclass
class LessonCapturedEvent(BusEvent):
    """A new lesson was written to ``~/.hermes/lessons/`` — sibling
    agents can listen and refresh their relevant_lessons() cache."""
    lesson_path: str = ""
    task: str = ""
    tags: tuple = ()


@dataclass
class OrchestratorTaskCompletedEvent(BusEvent):
    """One node in an orchestrator DAG completed."""
    task_id: str = ""
    orchestrator_id: str = ""
    success: bool = True
    output_keys: tuple = ()


# ---------------------------------------------------------------------------
# Bus implementation
# ---------------------------------------------------------------------------


_DEFAULT_HISTORY = 1_000  # per subject, for late subscribers


class Bus:
    """In-process pub/sub bus.

    Thread-safe.  Subscribers can register by event class, by exact
    subject string, or by glob pattern (``agent.*.tool_started``).
    Publishing is non-blocking — handlers dispatch on a worker pool.

    Single global instance is recommended via ``get_bus()`` for
    simplicity; tests construct private instances.
    """

    def __init__(
        self,
        *,
        max_workers: int = 4,
        history_per_subject: int = _DEFAULT_HISTORY,
    ) -> None:
        self._lock = threading.RLock()
        # subject_pattern -> list of (event_class_or_None, handler)
        self._pattern_subs: List[tuple] = []
        # exact subject -> list of handlers (fast path)
        self._exact_subs: Dict[str, List[Callable]] = defaultdict(list)
        # event class -> list of handlers (typed path)
        self._typed_subs: Dict[Type, List[Callable]] = defaultdict(list)
        # subject -> deque of recent events (for late subscribers)
        self._history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=history_per_subject)
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="bus-handler",
        )
        self._closed = False

    # ---------- subscribe paths ----------

    def subscribe(
        self,
        handler: Callable[[BusEvent], None],
        *,
        subject: Optional[str] = None,
        pattern: Optional[str] = None,
        event_type: Optional[Type[BusEvent]] = None,
    ) -> Callable[[], None]:
        """Register a handler.  Returns an unsubscribe callable.

        Choose ONE of subject / pattern / event_type:
        * ``subject="agent.foo.tool_started"`` — exact match
        * ``pattern="agent.*.tool_started"`` — glob via ``fnmatch``
        * ``event_type=ToolStartedEvent`` — match on dataclass type
        """
        provided = sum(x is not None for x in (subject, pattern, event_type))
        if provided != 1:
            raise ValueError(
                "subscribe() takes exactly one of subject, pattern, event_type"
            )
        with self._lock:
            if event_type is not None:
                self._typed_subs[event_type].append(handler)
                def _unsub_typed():
                    with self._lock:
                        try:
                            self._typed_subs[event_type].remove(handler)
                        except ValueError:
                            pass
                return _unsub_typed
            if subject is not None:
                self._exact_subs[subject].append(handler)
                def _unsub_exact():
                    with self._lock:
                        try:
                            self._exact_subs[subject].remove(handler)
                        except ValueError:
                            pass
                return _unsub_exact
            # pattern
            entry = (pattern, handler)
            self._pattern_subs.append(entry)
            def _unsub_pat():
                with self._lock:
                    try:
                        self._pattern_subs.remove(entry)
                    except ValueError:
                        pass
            return _unsub_pat

    # ---------- publish ----------

    def publish(self, event: BusEvent, *, sync: bool = False) -> None:
        """Publish an event.  Non-blocking by default.

        ``sync=True`` runs handlers inline on the publisher's thread —
        useful in tests and when ordering matters across publish calls.
        """
        if self._closed:
            return
        if not isinstance(event, BusEvent):
            raise TypeError(f"publish requires BusEvent, got {type(event).__name__}")
        with self._lock:
            self._history[event.subject].append(event)
            handlers: List[Callable] = []
            handlers.extend(self._exact_subs.get(event.subject, ()))
            for pattern, h in self._pattern_subs:
                if fnmatch.fnmatchcase(event.subject, pattern):
                    handlers.append(h)
            for etype, hs in self._typed_subs.items():
                if isinstance(event, etype):
                    handlers.extend(hs)
        if sync:
            for h in handlers:
                try:
                    h(event)
                except Exception as exc:
                    logger.warning("bus handler raised on %s: %s", event.subject, exc)
        else:
            for h in handlers:
                try:
                    self._executor.submit(_safe_call, h, event)
                except RuntimeError:
                    # Executor shut down — fall through to inline (best-effort)
                    try:
                        h(event)
                    except Exception:
                        pass

    # ---------- queries ----------

    def history(self, subject: str, *, limit: int = 100) -> List[BusEvent]:
        """Return the most recent events on a subject, oldest first."""
        with self._lock:
            buf = list(self._history.get(subject, ()))
        return buf[-limit:]

    def subjects(self) -> List[str]:
        """List subjects with any historical traffic."""
        with self._lock:
            return [s for s, q in self._history.items() if q]

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


def _safe_call(handler: Callable, event: BusEvent) -> None:
    try:
        handler(event)
    except Exception as exc:
        logger.warning("bus handler raised on %s: %s", event.subject, exc)


# ---------------------------------------------------------------------------
# Process-global default bus
# ---------------------------------------------------------------------------


_DEFAULT_BUS: Optional[Bus] = None
_DEFAULT_LOCK = threading.Lock()


def get_bus() -> Bus:
    """Return the process-global default bus, constructing on first use."""
    global _DEFAULT_BUS
    with _DEFAULT_LOCK:
        if _DEFAULT_BUS is None:
            _DEFAULT_BUS = Bus()
        return _DEFAULT_BUS


def reset_default_bus() -> None:
    """Test-only: drop the global bus so the next get_bus() makes a fresh one."""
    global _DEFAULT_BUS
    with _DEFAULT_LOCK:
        if _DEFAULT_BUS is not None:
            try:
                _DEFAULT_BUS.close()
            except Exception:
                pass
        _DEFAULT_BUS = None


# ---------------------------------------------------------------------------
# Convenience publishers — the wire format every agent should use
# ---------------------------------------------------------------------------


def publish_tool_started(agent_id: str, tool_name: str,
                          args_preview: str = "", *, bus: Optional[Bus] = None) -> None:
    (bus or get_bus()).publish(ToolStartedEvent(
        subject=f"agent.{agent_id}.tool_started",
        agent_id=agent_id, tool_name=tool_name, args_preview=args_preview[:200],
    ))


def publish_tool_finished(agent_id: str, tool_name: str, *, duration_ms: float,
                           success: bool = True, error: Optional[str] = None,
                           bus: Optional[Bus] = None) -> None:
    (bus or get_bus()).publish(ToolFinishedEvent(
        subject=f"agent.{agent_id}.tool_finished",
        agent_id=agent_id, tool_name=tool_name,
        duration_ms=duration_ms, success=success, error=error,
    ))


def publish_agent_spawned(agent_id: str, parent_agent_id: Optional[str],
                           subagent_type: Optional[str], goal: str,
                           toolsets: Iterable[str] = (),
                           *, bus: Optional[Bus] = None) -> None:
    (bus or get_bus()).publish(AgentSpawnedEvent(
        subject=f"agent.{agent_id}.spawned",
        agent_id=agent_id, parent_agent_id=parent_agent_id,
        subagent_type=subagent_type, goal=goal[:200],
        toolsets=tuple(toolsets),
    ))


def publish_agent_finished(agent_id: str, parent_agent_id: Optional[str],
                            *, success: bool, final_response: str,
                            iterations_used: int = 0,
                            bus: Optional[Bus] = None) -> None:
    (bus or get_bus()).publish(AgentFinishedEvent(
        subject=f"agent.{agent_id}.finished",
        agent_id=agent_id, parent_agent_id=parent_agent_id,
        success=success, final_response=final_response[:2000],
        iterations_used=iterations_used,
    ))


def publish_lesson_captured(lesson_path: str, task: str, tags: Iterable[str],
                             *, bus: Optional[Bus] = None) -> None:
    (bus or get_bus()).publish(LessonCapturedEvent(
        subject=f"lesson.captured",
        lesson_path=lesson_path, task=task[:200], tags=tuple(tags),
    ))


__all__ = [
    "AgentFinishedEvent",
    "AgentSpawnedEvent",
    "Bus",
    "BusEvent",
    "FileLockAcquiredEvent",
    "FileLockReleasedEvent",
    "LessonCapturedEvent",
    "OrchestratorTaskCompletedEvent",
    "ToolFinishedEvent",
    "ToolStartedEvent",
    "get_bus",
    "publish_agent_finished",
    "publish_agent_spawned",
    "publish_lesson_captured",
    "publish_tool_finished",
    "publish_tool_started",
    "reset_default_bus",
]
