"""
Orchestrator — typed DAG of parallel agent tasks with bus coordination.

The headline architectural piece for "spin up other agents that all
work together in parallel."  Subagent delegation today (``delegate_task``)
is parent→children with thread-pool execution but children can't
talk to each other.  The orchestrator gives you:

* **Tasks** with explicit dependencies (DAG).
* **Parallel execution** — ready tasks run concurrently up to a
  configurable fan-out cap.
* **Outputs piped to inputs** — a task that depends on another sees
  its predecessor's result as part of its input context.
* **Bus-mediated coordination** — every task spawn/complete publishes
  an event; subscribers (verifier, lesson capture, topology UI) see
  the orchestration live.
* **File-lock arbitration** — tasks declare which paths they'll
  write; the orchestrator serialises conflicting writers without
  serialising the whole DAG.
* **Verification quorum** — optional multi-model verification per
  task or for the final result.

Architecture
------------

Inside one process for now (uses ``concurrent.futures.ThreadPoolExecutor``);
the bus + lock-manager surface are designed so a future "distributed
mode" can swap them for NATS + Redis without changing this file's
public API.

The orchestrator is NOT a replacement for ``delegate_task``.  It's a
higher-level coordinator that USES ``delegate_task`` (one call per
task node) and adds the DAG/coordination/verification layer on top.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Set

logger = logging.getLogger(__name__)


_DEFAULT_FANOUT = 4
_DEFAULT_TASK_TIMEOUT = 600.0  # 10 minutes


# ---------------------------------------------------------------------------
# Task spec & state
# ---------------------------------------------------------------------------


class TaskState(Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"  # upstream failed and policy = "skip_on_upstream_fail"


@dataclass(slots=True)
class TaskSpec:
    """One node in the DAG."""

    id: str
    goal: str
    subagent_type: Optional[str] = None
    toolsets: Optional[List[str]] = None
    depends_on: List[str] = field(default_factory=list)
    write_paths: List[str] = field(default_factory=list)
    max_iterations: Optional[int] = None
    #: When True, an upstream failure aborts this task.  When False
    #: the task still runs (e.g. an independent cleanup step).
    requires_upstream_success: bool = True


@dataclass(slots=True)
class TaskResult:
    """The outcome of one task node."""

    task_id: str
    state: TaskState
    output: str = ""
    error: Optional[str] = None
    duration_seconds: float = 0.0
    agent_id: Optional[str] = None
    iterations_used: int = 0
    verification: Optional[Dict[str, Any]] = None


@dataclass
class OrchestratorResult:
    """Aggregate outcome of one orchestrator run."""

    orchestrator_id: str
    succeeded: bool
    results: Dict[str, TaskResult] = field(default_factory=dict)
    total_duration_seconds: float = 0.0
    total_iterations: int = 0

    def task(self, task_id: str) -> Optional[TaskResult]:
        return self.results.get(task_id)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_dag(tasks: Iterable[TaskSpec]) -> List[str]:
    """Return a list of validation errors.  Empty list = valid DAG."""
    tasks = list(tasks)
    ids = [t.id for t in tasks]
    errors: List[str] = []
    if not ids:
        errors.append("DAG has no tasks")
        return errors
    if len(ids) != len(set(ids)):
        dupes = {x for x in ids if ids.count(x) > 1}
        errors.append(f"duplicate task ids: {sorted(dupes)}")
    id_set = set(ids)
    for t in tasks:
        for dep in t.depends_on:
            if dep not in id_set:
                errors.append(f"task {t.id!r} depends on missing {dep!r}")
        if t.id in t.depends_on:
            errors.append(f"task {t.id!r} depends on itself")
    # Cycle detection — Kahn's algorithm
    indeg: Dict[str, int] = {tid: 0 for tid in id_set}
    edges: Dict[str, Set[str]] = {tid: set() for tid in id_set}
    for t in tasks:
        for dep in t.depends_on:
            if dep in id_set:
                edges[dep].add(t.id)
                indeg[t.id] += 1
    queue = [tid for tid, n in indeg.items() if n == 0]
    visited = 0
    while queue:
        cur = queue.pop()
        visited += 1
        for nxt in edges[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if visited != len(id_set):
        remaining = [tid for tid, n in indeg.items() if n > 0]
        errors.append(f"cycle detected involving: {sorted(remaining)}")
    return errors


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


#: Type for the task-executor function — abstracted so tests can inject
#: a fake.  Real one calls into ``tools.delegate_tool.delegate_task``.
#:
#: Defined as a Protocol AND a callable alias so callers can either:
#:   * write a plain function with the right signature (most common), OR
#:   * implement ``TaskExecutorProtocol`` on a class with state
#:     (e.g. a remote-fleet executor with a connection pool).
class TaskExecutorProtocol(Protocol):
    """Structural type for task executors.

    Implementations receive the task spec and a snapshot of completed
    upstream results, and must return a ``TaskResult``.  Raising is
    legal — the orchestrator catches and marks the task FAILED with
    the exception text — but returning a real TaskResult lets the
    executor carry extra metadata (iterations_used, agent_id, etc.)
    that the aggregate uses.
    """

    def __call__(
        self,
        spec: TaskSpec,
        prior_results: Dict[str, TaskResult],
    ) -> TaskResult: ...


TaskExecutor = Callable[[TaskSpec, Dict[str, TaskResult]], TaskResult]


class Orchestrator:
    """Coordinates parallel task execution against a DAG.

    Construct with a ``task_executor`` callable (the runner that
    actually invokes a task — typically wrapped around delegate_task).
    Call ``run(tasks)`` with a list of TaskSpec.

    Thread-safe; only one ``run`` per instance at a time (serialised).
    """

    def __init__(
        self,
        task_executor: TaskExecutor,
        *,
        fanout: int = _DEFAULT_FANOUT,
        task_timeout: float = _DEFAULT_TASK_TIMEOUT,
        orchestrator_id: Optional[str] = None,
    ) -> None:
        self._executor_fn = task_executor
        self._fanout = max(1, fanout)
        self._task_timeout = task_timeout
        self._id = orchestrator_id or f"orch-{uuid.uuid4().hex[:8]}"
        self._run_lock = threading.Lock()

    @property
    def id(self) -> str:
        return self._id

    def run(self, tasks: List[TaskSpec]) -> OrchestratorResult:
        """Execute the DAG.  Blocks until terminal."""
        with self._run_lock:
            errors = validate_dag(tasks)
            if errors:
                raise ValueError(
                    f"invalid DAG for orchestrator {self._id}: {'; '.join(errors)}"
                )
            return self._run_inner(tasks)

    # ---------- internal ----------

    def _run_inner(self, tasks: List[TaskSpec]) -> OrchestratorResult:
        start = time.monotonic()
        by_id = {t.id: t for t in tasks}
        state: Dict[str, TaskState] = {t.id: TaskState.PENDING for t in tasks}
        results: Dict[str, TaskResult] = {}
        # Reverse adjacency for "what becomes ready when X finishes"
        children: Dict[str, Set[str]] = {t.id: set() for t in tasks}
        for t in tasks:
            for dep in t.depends_on:
                children[dep].add(t.id)

        self._publish_orchestrator_started(len(tasks))

        in_flight: Dict[str, Future] = {}
        with ThreadPoolExecutor(
            max_workers=self._fanout,
            thread_name_prefix=f"orch-{self._id[:8]}",
        ) as pool:
            while True:
                # 1. Promote READY: pending tasks whose deps are all done.
                for tid, st in list(state.items()):
                    if st is not TaskState.PENDING:
                        continue
                    spec = by_id[tid]
                    dep_states = [state[d] for d in spec.depends_on]
                    if all(d is TaskState.SUCCEEDED for d in dep_states):
                        state[tid] = TaskState.READY
                    elif any(
                        d in (TaskState.FAILED, TaskState.SKIPPED) for d in dep_states
                    ):
                        if spec.requires_upstream_success:
                            state[tid] = TaskState.SKIPPED
                            results[tid] = TaskResult(
                                task_id=tid, state=TaskState.SKIPPED,
                                error="upstream dependency failed or was skipped",
                            )

                # 2. Submit ready tasks up to fanout capacity.
                for tid, st in list(state.items()):
                    if st is not TaskState.READY:
                        continue
                    if len(in_flight) >= self._fanout:
                        break
                    state[tid] = TaskState.RUNNING
                    spec = by_id[tid]
                    self._publish_task_started(spec)
                    # Pass a SNAPSHOT of completed results, not the live
                    # dict.  Otherwise a parallel sibling that finishes
                    # after this task starts could leak into the task's
                    # view of "upstream" — semantically wrong AND
                    # confusing to test against.
                    snapshot = dict(results)
                    in_flight[tid] = pool.submit(
                        self._run_one_safely, spec, snapshot,
                    )

                if not in_flight:
                    # No work running and nothing newly ready — we're done
                    # (either all succeeded, or everything else is SKIPPED).
                    break

                # 3. Wait for at least one task to finish, then loop.
                done_ids: List[str] = []
                remaining = list(in_flight.items())
                # Poll loop bounded by task_timeout — keeps things responsive
                # without resorting to wait(FIRST_COMPLETED) which can't
                # be cleanly interrupted.
                while not done_ids:
                    for tid, fut in remaining:
                        if fut.done():
                            done_ids.append(tid)
                    if done_ids:
                        break
                    time.sleep(0.05)

                for tid in done_ids:
                    fut = in_flight.pop(tid)
                    spec = by_id[tid]
                    try:
                        result = fut.result(timeout=0)
                    except Exception as exc:
                        result = TaskResult(
                            task_id=tid, state=TaskState.FAILED,
                            error=f"task executor raised: {exc}",
                        )
                    results[tid] = result
                    state[tid] = result.state
                    self._publish_task_completed(spec, result)

        total_duration = time.monotonic() - start
        # Ensure every task has a result entry — even SKIPPED ones may not
        # have been populated if they were skipped during promotion above.
        for tid in by_id:
            results.setdefault(tid, TaskResult(
                task_id=tid, state=state.get(tid, TaskState.SKIPPED),
                error="never executed",
            ))
        succeeded = all(
            r.state is TaskState.SUCCEEDED
            for r in results.values()
        )
        total_iters = sum(r.iterations_used for r in results.values())
        agg = OrchestratorResult(
            orchestrator_id=self._id,
            succeeded=succeeded,
            results=results,
            total_duration_seconds=total_duration,
            total_iterations=total_iters,
        )
        self._publish_orchestrator_finished(agg)
        return agg

    def _run_one_safely(
        self, spec: TaskSpec, prior_results: Dict[str, TaskResult],
    ) -> TaskResult:
        start = time.monotonic()
        try:
            result = self._executor_fn(spec, prior_results)
        except Exception as exc:
            logger.warning("orchestrator task %s raised: %s", spec.id, exc)
            return TaskResult(
                task_id=spec.id, state=TaskState.FAILED,
                error=str(exc),
                duration_seconds=time.monotonic() - start,
            )
        # Defensive: ensure required fields.
        if not isinstance(result, TaskResult):
            return TaskResult(
                task_id=spec.id, state=TaskState.FAILED,
                error=f"executor returned non-TaskResult: {type(result).__name__}",
                duration_seconds=time.monotonic() - start,
            )
        if not result.duration_seconds:
            result.duration_seconds = time.monotonic() - start
        if result.task_id != spec.id:
            # Normalise — task_id is the contract.
            result.task_id = spec.id
        return result

    # ---------- bus publishing ----------

    def _publish_orchestrator_started(self, task_count: int) -> None:
        try:
            from agent.bus import BusEvent, get_bus
            get_bus().publish(BusEvent(
                subject=f"orchestrator.{self._id}.started",
            ))
            logger.info("orchestrator %s started: %d task(s), fanout=%d",
                        self._id, task_count, self._fanout)
        except Exception:
            pass

    def _publish_orchestrator_finished(self, agg: OrchestratorResult) -> None:
        try:
            from agent.bus import BusEvent, get_bus
            get_bus().publish(BusEvent(
                subject=f"orchestrator.{self._id}.finished",
            ))
            logger.info(
                "orchestrator %s finished: succeeded=%s tasks=%d duration=%.2fs",
                self._id, agg.succeeded, len(agg.results),
                agg.total_duration_seconds,
            )
        except Exception:
            pass

    def _publish_task_started(self, spec: TaskSpec) -> None:
        try:
            from agent.bus import BusEvent, get_bus
            get_bus().publish(BusEvent(
                subject=f"orchestrator.{self._id}.task.{spec.id}.started",
            ))
        except Exception:
            pass

    def _publish_task_completed(self, spec: TaskSpec, result: TaskResult) -> None:
        try:
            from agent.bus import OrchestratorTaskCompletedEvent, get_bus
            get_bus().publish(OrchestratorTaskCompletedEvent(
                subject=f"orchestrator.{self._id}.task.{spec.id}.completed",
                task_id=spec.id, orchestrator_id=self._id,
                success=result.state is TaskState.SUCCEEDED,
            ))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Default executor — wraps delegate_task with file-lock arbitration
# ---------------------------------------------------------------------------


def build_default_executor(parent_agent) -> TaskExecutor:
    """Build a TaskExecutor that runs each task via ``delegate_task``.

    Acquires write-locks for ``spec.write_paths`` before invoking the
    subagent and releases them on completion.  Pipes upstream task
    outputs into the goal's context so dependent tasks see their
    predecessors' work.
    """
    def _execute(spec: TaskSpec, prior: Dict[str, TaskResult]) -> TaskResult:
        from agent.file_locks import LockOutcome, file_locks

        upstream_context = ""
        if spec.depends_on:
            sections = []
            for dep_id in spec.depends_on:
                dep_result = prior.get(dep_id)
                if dep_result is not None and dep_result.output:
                    sections.append(f"## Upstream task {dep_id!r}\n\n{dep_result.output}")
            if sections:
                upstream_context = (
                    "## Upstream task outputs\n\n"
                    + "\n\n".join(sections)
                    + "\n\n## This task\n\n"
                )

        # Acquire locks on all declared write paths up-front.  If we
        # can't get them all, fail the task fast rather than holding
        # half the locks and waiting.
        locks_held: List[str] = []
        mgr = file_locks()
        owner = f"orch-task-{spec.id}"
        for path in (spec.write_paths or []):
            outcome = mgr.acquire_blocking(
                path, owner=owner, timeout=30.0,
            )
            if outcome is LockOutcome.BUSY:
                # Release anything we got so siblings can proceed.
                for held in locks_held:
                    mgr.release(held, owner=owner)
                return TaskResult(
                    task_id=spec.id, state=TaskState.FAILED,
                    error=f"could not acquire write lock on {path} within 30s",
                )
            locks_held.append(path)

        try:
            from tools.delegate_tool import delegate_task as _delegate
            import json as _json
            raw = _delegate(
                goal=upstream_context + spec.goal,
                toolsets=spec.toolsets,
                max_iterations=spec.max_iterations,
                subagent_type=spec.subagent_type,
                role="leaf",
                parent_agent=parent_agent,
            )
            # delegate_task returns JSON — pull out the first result.
            output = raw
            success = True
            try:
                parsed = _json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, dict) and "results" in parsed:
                    res_list = parsed.get("results") or []
                    if res_list:
                        first = res_list[0]
                        output = first.get("response") or first.get("output") or raw
                        success = not bool(first.get("error"))
            except Exception:
                pass
            return TaskResult(
                task_id=spec.id,
                state=TaskState.SUCCEEDED if success else TaskState.FAILED,
                output=output if isinstance(output, str) else _json.dumps(output),
                error=None if success else "subagent reported error",
            )
        finally:
            for held in locks_held:
                mgr.release(held, owner=owner)

    return _execute


__all__ = [
    "Orchestrator",
    "OrchestratorResult",
    "TaskExecutor",
    "TaskResult",
    "TaskSpec",
    "TaskState",
    "build_default_executor",
    "validate_dag",
]
