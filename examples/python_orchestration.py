"""
Programmatic example — the same parallel feature-ship pipeline from
``parallel_pipeline.yaml``, but constructed in Python.

Use this when you want to:

* Conditionally include tasks based on file state (e.g. only run
  ``schema`` if api/schemas/user.py needs changes).
* Subscribe to bus events for live monitoring while the DAG runs.
* Wire a custom ``TaskExecutor`` (e.g. one that talks to a remote
  agent fleet instead of in-process delegate_task).

Run with::

    python examples/python_orchestration.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Make repo root importable when running this file directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    from agent.bus import (
        OrchestratorTaskCompletedEvent,
        ToolStartedEvent,
        get_bus,
    )
    from agent.orchestrator import (
        Orchestrator,
        TaskSpec,
        TaskState,
        TaskResult,
    )

    # 1) Subscribe to live events so we can watch the DAG execute.
    #
    #    In production you'd subscribe a React-Flow component to render
    #    a live topology graph; for the example we just print.
    bus = get_bus()

    def on_task_completed(event: OrchestratorTaskCompletedEvent) -> None:
        glyph = "✓" if event.success else "✗"
        print(f"  bus: {glyph} task {event.task_id!r} completed")

    def on_tool_started(event: ToolStartedEvent) -> None:
        print(f"  bus: agent {event.agent_id!r} → {event.tool_name}")

    bus.subscribe(on_task_completed, event_type=OrchestratorTaskCompletedEvent)
    bus.subscribe(on_tool_started, event_type=ToolStartedEvent)

    # 2) Define the DAG.
    #
    #    Identical structure to parallel_pipeline.yaml but built as
    #    Python so you can compute spec content dynamically.
    tasks = [
        TaskSpec(
            id="server",
            goal=(
                "Add a GET /v2/users endpoint to api/server.py.  Use the "
                "existing router patterns; don't introduce a new framework."
            ),
            subagent_type="code-reviewer",
            write_paths=["api/server.py"],
        ),
        TaskSpec(
            id="schema",
            goal=(
                "Add a User v2 Pydantic schema to api/schemas/user.py.  "
                "Mirror conventions in api/schemas/post.py."
            ),
            write_paths=["api/schemas/user.py"],
        ),
        TaskSpec(
            id="tests",
            goal=(
                "Write pytest tests at tests/api/test_users_v2.py covering "
                "happy path + 4 edge cases.  Run pytest before declaring done."
            ),
            subagent_type="test-writer",
            depends_on=["server", "schema"],
            write_paths=["tests/api/test_users_v2.py"],
        ),
        TaskSpec(
            id="docs",
            goal=(
                "Update docs/api/users.md to document the new endpoint.  "
                "Add 'Migration from v1' subsection."
            ),
            depends_on=["server"],
            write_paths=["docs/api/users.md"],
        ),
    ]

    # 3) Run with a stub executor — replace this block with
    #    ``build_default_executor(parent_agent=...)`` once you have a
    #    constructed AIAgent.  The stub here just sleeps and succeeds so
    #    the example is runnable without any API credentials.
    def _stub_executor(spec: TaskSpec, prior_results) -> TaskResult:
        print(f"  exec: starting {spec.id!r} "
              f"(deps={spec.depends_on or '—'})")
        time.sleep(0.5)
        return TaskResult(
            task_id=spec.id,
            state=TaskState.SUCCEEDED,
            output=f"Stub output for {spec.id}",
            iterations_used=1,
        )

    orch = Orchestrator(_stub_executor, fanout=4)

    print(f"Starting orchestrator with {len(tasks)} task(s), fanout=4...")
    start = time.monotonic()
    result = orch.run(tasks)
    duration = time.monotonic() - start

    # 4) Report.
    print()
    print(f"{'SUCCESS' if result.succeeded else 'FAILED'} — "
          f"{len(result.results)} task(s) in {duration:.2f}s")
    print(f"  (serial would have been ~{0.5 * len(tasks):.1f}s — "
          f"speedup ~{(0.5 * len(tasks)) / duration:.1f}x)")
    for tid, r in result.results.items():
        glyph = {
            TaskState.SUCCEEDED: "✓",
            TaskState.FAILED:    "✗",
            TaskState.SKIPPED:   "↳",
        }.get(r.state, "?")
        print(f"  {glyph} {tid:<10}  {r.state.value:<10}  {r.duration_seconds:.2f}s")

    return 0 if result.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
