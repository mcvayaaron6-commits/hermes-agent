"""Tests for the parallel-task orchestrator."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.orchestrator import (
    Orchestrator,
    TaskResult,
    TaskSpec,
    TaskState,
    validate_dag,
)


# ---------------------------------------------------------------------------
# DAG validation
# ---------------------------------------------------------------------------


def test_validate_empty():
    errs = validate_dag([])
    assert errs


def test_validate_simple_chain():
    tasks = [
        TaskSpec(id="a", goal="A"),
        TaskSpec(id="b", goal="B", depends_on=["a"]),
        TaskSpec(id="c", goal="C", depends_on=["b"]),
    ]
    assert validate_dag(tasks) == []


def test_validate_duplicate_ids():
    tasks = [
        TaskSpec(id="a", goal="A"),
        TaskSpec(id="a", goal="A again"),
    ]
    errs = validate_dag(tasks)
    assert any("duplicate" in e for e in errs)


def test_validate_missing_dep():
    tasks = [TaskSpec(id="a", goal="A", depends_on=["b"])]
    errs = validate_dag(tasks)
    assert any("missing" in e for e in errs)


def test_validate_self_dep():
    tasks = [TaskSpec(id="a", goal="A", depends_on=["a"])]
    errs = validate_dag(tasks)
    assert any("itself" in e for e in errs)


def test_validate_cycle():
    tasks = [
        TaskSpec(id="a", goal="A", depends_on=["b"]),
        TaskSpec(id="b", goal="B", depends_on=["c"]),
        TaskSpec(id="c", goal="C", depends_on=["a"]),
    ]
    errs = validate_dag(tasks)
    assert any("cycle" in e for e in errs)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _success_executor(call_log=None):
    """Build an executor that always succeeds, optionally logging calls."""
    def _exec(spec, prior):
        if call_log is not None:
            call_log.append((spec.id, time.monotonic()))
        return TaskResult(
            task_id=spec.id, state=TaskState.SUCCEEDED,
            output=f"output of {spec.id}",
            iterations_used=1,
        )
    return _exec


def test_run_single_task_succeeds():
    orch = Orchestrator(_success_executor())
    result = orch.run([TaskSpec(id="solo", goal="Just do it")])
    assert result.succeeded
    assert result.task("solo").state is TaskState.SUCCEEDED
    assert result.task("solo").output == "output of solo"
    assert result.total_iterations == 1


def test_run_diamond_dag_all_succeed():
    """
        a
       / \\
      b   c
       \\ /
        d
    """
    tasks = [
        TaskSpec(id="a", goal="A"),
        TaskSpec(id="b", goal="B", depends_on=["a"]),
        TaskSpec(id="c", goal="C", depends_on=["a"]),
        TaskSpec(id="d", goal="D", depends_on=["b", "c"]),
    ]
    orch = Orchestrator(_success_executor())
    result = orch.run(tasks)
    assert result.succeeded
    assert all(r.state is TaskState.SUCCEEDED for r in result.results.values())


def test_parallel_siblings_run_concurrently():
    """b and c depend only on a — they must overlap in time."""
    started_at = []

    def slow_exec(spec, prior):
        started_at.append((spec.id, time.monotonic()))
        time.sleep(0.2)
        return TaskResult(task_id=spec.id, state=TaskState.SUCCEEDED)

    tasks = [
        TaskSpec(id="a", goal="A"),
        TaskSpec(id="b", goal="B", depends_on=["a"]),
        TaskSpec(id="c", goal="C", depends_on=["a"]),
    ]
    orch = Orchestrator(slow_exec, fanout=4)
    orch.run(tasks)
    # b and c should have started within a small window — well under
    # the 0.2s each task takes.
    by_id = dict(started_at)
    delta = abs(by_id["b"] - by_id["c"])
    assert delta < 0.1, f"b and c not parallel (delta={delta:.3f}s)"


def test_fanout_caps_concurrency():
    """With fanout=1, b and c can't overlap."""
    started_at = []

    def slow_exec(spec, prior):
        started_at.append((spec.id, time.monotonic()))
        time.sleep(0.1)
        return TaskResult(task_id=spec.id, state=TaskState.SUCCEEDED)

    tasks = [
        TaskSpec(id="a", goal="A"),
        TaskSpec(id="b", goal="B", depends_on=["a"]),
        TaskSpec(id="c", goal="C", depends_on=["a"]),
    ]
    orch = Orchestrator(slow_exec, fanout=1)
    orch.run(tasks)
    by_id = dict(started_at)
    # With fanout=1, b and c serialise — at least 0.1s apart.
    delta = abs(by_id["b"] - by_id["c"])
    assert delta >= 0.08


def test_upstream_failure_skips_downstream():
    def fail_b_succeed_others(spec, prior):
        if spec.id == "b":
            return TaskResult(task_id=spec.id, state=TaskState.FAILED,
                              error="boom")
        return TaskResult(task_id=spec.id, state=TaskState.SUCCEEDED)

    tasks = [
        TaskSpec(id="a", goal="A"),
        TaskSpec(id="b", goal="B", depends_on=["a"]),
        TaskSpec(id="c", goal="C", depends_on=["b"]),  # blocked by b
        TaskSpec(id="d", goal="D", depends_on=["a"]),  # independent of b
    ]
    orch = Orchestrator(fail_b_succeed_others)
    result = orch.run(tasks)
    assert not result.succeeded
    assert result.task("a").state is TaskState.SUCCEEDED
    assert result.task("b").state is TaskState.FAILED
    assert result.task("c").state is TaskState.SKIPPED  # upstream failed
    assert result.task("d").state is TaskState.SUCCEEDED  # independent path


def test_requires_upstream_success_false_still_runs():
    """A task with requires_upstream_success=False must run even when
    upstream failed — that's the whole point of the flag.  Before the
    fix this stranded the task PENDING forever; now it's promoted to
    READY as soon as every dep reaches a terminal state."""
    executed = []

    def fail_a_track_cleanup(spec, prior):
        executed.append(spec.id)
        if spec.id == "a":
            return TaskResult(task_id=spec.id, state=TaskState.FAILED,
                              error="boom")
        return TaskResult(task_id=spec.id, state=TaskState.SUCCEEDED)

    tasks = [
        TaskSpec(id="a", goal="A"),
        TaskSpec(id="cleanup", goal="cleanup", depends_on=["a"],
                 requires_upstream_success=False),
    ]
    orch = Orchestrator(fail_a_track_cleanup)
    result = orch.run(tasks)
    assert result.task("a").state is TaskState.FAILED
    # The cleanup MUST have executed and succeeded.
    assert "cleanup" in executed
    assert result.task("cleanup").state is TaskState.SUCCEEDED
    # The overall result is failed (because A failed) but cleanup
    # got its chance.
    assert not result.succeeded


def test_requires_upstream_success_false_waits_for_terminal_state():
    """Cleanup task waits for ALL deps to reach terminal state before
    promoting — doesn't run prematurely on partial completion."""
    import threading
    block_a = threading.Event()
    executed_order = []

    def slow_a_then_cleanup(spec, prior):
        executed_order.append(spec.id)
        if spec.id == "a":
            block_a.wait(timeout=2.0)
            return TaskResult(task_id=spec.id, state=TaskState.FAILED,
                              error="boom")
        return TaskResult(task_id=spec.id, state=TaskState.SUCCEEDED)

    tasks = [
        TaskSpec(id="a", goal="A"),
        TaskSpec(id="cleanup", goal="cleanup", depends_on=["a"],
                 requires_upstream_success=False),
    ]
    orch = Orchestrator(slow_a_then_cleanup, fanout=2)

    import time
    def release():
        time.sleep(0.1)
        block_a.set()
    t = threading.Thread(target=release)
    t.start()
    result = orch.run(tasks)
    t.join()

    # cleanup must come after a finished, not interleaved.
    assert executed_order[0] == "a"
    assert "cleanup" in executed_order[1:]


def test_executor_raises_becomes_failed():
    def boom(spec, prior):
        raise RuntimeError("executor exploded")

    orch = Orchestrator(boom)
    result = orch.run([TaskSpec(id="solo", goal="x")])
    assert not result.succeeded
    assert result.task("solo").state is TaskState.FAILED
    assert "exploded" in result.task("solo").error


def test_executor_returns_non_taskresult():
    def bogus(spec, prior):
        return "not a TaskResult"

    orch = Orchestrator(bogus)
    result = orch.run([TaskSpec(id="solo", goal="x")])
    assert result.task("solo").state is TaskState.FAILED


def test_upstream_output_piped_into_context():
    captured_inputs = {}

    def capture(spec, prior):
        captured_inputs[spec.id] = (spec.goal, prior)
        return TaskResult(task_id=spec.id, state=TaskState.SUCCEEDED,
                          output=f"OUT-{spec.id}")

    tasks = [
        TaskSpec(id="a", goal="A"),
        TaskSpec(id="b", goal="B", depends_on=["a"]),
    ]
    Orchestrator(capture).run(tasks)
    a_goal, a_prior = captured_inputs["a"]
    b_goal, b_prior = captured_inputs["b"]
    # a has no upstream.
    assert "a" not in a_prior
    # b sees a's result via the prior dict.
    assert "a" in b_prior
    assert b_prior["a"].output == "OUT-a"


def test_invalid_dag_raises_value_error():
    orch = Orchestrator(_success_executor())
    with pytest.raises(ValueError):
        orch.run([
            TaskSpec(id="a", goal="A", depends_on=["b"]),
            TaskSpec(id="b", goal="B", depends_on=["a"]),
        ])


def test_orchestrator_id_is_set():
    orch1 = Orchestrator(_success_executor())
    orch2 = Orchestrator(_success_executor())
    assert orch1.id != orch2.id
    custom = Orchestrator(_success_executor(), orchestrator_id="my-orch")
    assert custom.id == "my-orch"
