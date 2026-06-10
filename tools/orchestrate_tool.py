"""
The ``orchestrate_tasks`` tool — exposes the parallel DAG executor to the model.

Before this tool, the orchestrator (`agent/orchestrator.py`) was Python-only
infrastructure: callers had to write Python to use it.  This tool wraps it
in a JSON schema so the model can spin up parallel agent DAGs from inside
the loop, the same way it uses ``delegate_task`` for single subagents.

Schema design
-------------

The model passes a list of task dicts; each dict declares:

* ``id`` — short string id (referenced by other tasks' ``depends_on``)
* ``goal`` — natural-language description of what to do
* ``subagent_type`` — optional named profile (Explore, code-reviewer, ...)
* ``toolsets`` — optional override
* ``depends_on`` — list of task ids that must SUCCEED first
* ``write_paths`` — files this task will write (orchestrator acquires
  exclusive locks before dispatch)

Return shape: JSON with per-task ``state`` / ``output`` / ``error`` so the
model can read each branch's result.

Differences from ``delegate_task``
----------------------------------

* delegate_task is a flat batch — every task runs in parallel, no deps.
* orchestrate_tasks is a DAG — tasks can depend on each other's output,
  upstream output is piped into downstream context, and file-lock
  arbitration serialises conflicting writers WITHOUT serialising the
  whole pipeline.

Use ``delegate_task`` when tasks are independent.  Use
``orchestrate_tasks`` when tasks form a dependency graph (build → test
→ deploy, or refactor + tests + docs that depend on the refactor).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


ORCHESTRATE_TASKS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "orchestrate_tasks",
        "description": (
            "Run a parallel DAG of subagent tasks.  Each task can depend "
            "on other tasks; independent branches run concurrently up to "
            "the configured fanout.  File-lock arbitration prevents two "
            "tasks from clobbering each other on shared paths.  Returns "
            "JSON with per-task state + output.  Use this when work "
            "decomposes into tasks with dependencies (build → test → "
            "deploy, refactor + tests + docs that depend on refactor).  "
            "Use delegate_task instead for flat parallel batches."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": (
                        "Array of task specs forming a DAG.  Cycles are "
                        "rejected with a clear error.  Each task is one "
                        "subagent invocation."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": (
                                    "Short stable id (e.g. 'build', "
                                    "'test', 'deploy').  Referenced by "
                                    "other tasks' depends_on."
                                ),
                            },
                            "goal": {
                                "type": "string",
                                "description": (
                                    "What this subagent should do.  "
                                    "Upstream task outputs are auto-"
                                    "prepended as context."
                                ),
                            },
                            "subagent_type": {
                                "type": "string",
                                "description": (
                                    "Optional named subagent profile "
                                    "(e.g. 'code-reviewer', 'Explore', "
                                    "'test-writer').  See /subagents "
                                    "for the registry."
                                ),
                            },
                            "toolsets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Optional toolset whitelist for this "
                                    "task.  Defaults to the subagent "
                                    "profile's toolsets, or to delegate_"
                                    "task's default if no profile is set."
                                ),
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "List of task ids that must SUCCEED "
                                    "before this task starts.  Empty "
                                    "list = no dependencies (runs as "
                                    "soon as fanout has capacity)."
                                ),
                            },
                            "write_paths": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Absolute file paths this task will "
                                    "write.  The orchestrator acquires "
                                    "exclusive write-locks before "
                                    "dispatch; conflicting writers "
                                    "serialise without serialising the "
                                    "whole DAG."
                                ),
                            },
                            "max_iterations": {
                                "type": "integer",
                                "description": (
                                    "Optional per-task iteration cap "
                                    "for the child subagent."
                                ),
                            },
                            "requires_upstream_success": {
                                "type": "boolean",
                                "description": (
                                    "When false (default true), this "
                                    "task runs even if upstream tasks "
                                    "failed.  Use for cleanup steps."
                                ),
                            },
                        },
                        "required": ["id", "goal"],
                    },
                },
                "fanout": {
                    "type": "integer",
                    "description": (
                        "Maximum number of tasks running concurrently. "
                        "Default 4; higher values use more LLM API "
                        "throughput.  Cap at 16 to keep your provider "
                        "rate limits happy."
                    ),
                },
            },
            "required": ["tasks"],
        },
    },
}


def orchestrate_tasks(
    tasks: List[Dict[str, Any]],
    fanout: int = 4,
    parent_agent=None,
) -> str:
    """Run a DAG of subagent tasks and return per-task results as JSON.

    Designed for invocation from the agent loop via the registered tool.
    Never raises — turns validation errors and runtime failures into
    structured JSON error responses so the model can read and react.
    """
    if parent_agent is None:
        return _err("orchestrate_tasks requires a parent agent context.")
    if not isinstance(tasks, list) or not tasks:
        return _err("'tasks' must be a non-empty array of task specs.")

    from agent.orchestrator import (
        Orchestrator, TaskSpec, TaskState, build_default_executor,
    )

    # Build TaskSpec objects, surfacing bad input as a clear error.
    specs: List[TaskSpec] = []
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            return _err(
                f"task {i} must be an object, got {type(t).__name__}",
            )
        tid = (t.get("id") or "").strip()
        if not tid:
            return _err(f"task {i} is missing 'id'")
        goal = (t.get("goal") or "").strip()
        if not goal:
            return _err(f"task {tid!r} is missing 'goal'")
        try:
            specs.append(TaskSpec(
                id=tid,
                goal=goal,
                subagent_type=t.get("subagent_type") or None,
                toolsets=list(t["toolsets"]) if t.get("toolsets") else None,
                depends_on=list(t.get("depends_on") or []),
                write_paths=list(t.get("write_paths") or []),
                max_iterations=t.get("max_iterations"),
                requires_upstream_success=bool(
                    t.get("requires_upstream_success", True)
                ),
            ))
        except Exception as exc:
            return _err(f"task {tid!r} spec is malformed: {exc}")

    fanout = max(1, min(int(fanout or 4), 16))

    # Wire the default executor that calls delegate_task with file
    # locks acquired for each task's write_paths.
    orch = Orchestrator(
        build_default_executor(parent_agent=parent_agent),
        fanout=fanout,
    )
    try:
        agg = orch.run(specs)
    except ValueError as exc:
        # DAG validation failure (cycle, missing dep, etc.)
        return _err(f"invalid DAG: {exc}")
    except Exception as exc:
        logger.warning("orchestrate_tasks failed: %s", exc)
        return _err(f"orchestrator runtime failure: {exc}")

    # Render the aggregate to model-readable JSON.
    return json.dumps({
        "orchestrator_id": agg.orchestrator_id,
        "succeeded": agg.succeeded,
        "total_duration_seconds": round(agg.total_duration_seconds, 3),
        "total_iterations": agg.total_iterations,
        "results": [
            {
                "task_id": tid,
                "state": r.state.value if hasattr(r.state, "value") else str(r.state),
                "output": (r.output or "")[:8_000],
                "error": r.error,
                "duration_seconds": round(r.duration_seconds, 3),
                "iterations_used": r.iterations_used,
            }
            for tid, r in agg.results.items()
        ],
    }, ensure_ascii=False)


def _err(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


# --- Registry hook ---
from tools.registry import registry  # noqa: E402

registry.register(
    name="orchestrate_tasks",
    toolset="delegation",
    schema=ORCHESTRATE_TASKS_SCHEMA,
    handler=lambda args, **kw: orchestrate_tasks(
        tasks=args.get("tasks") or [],
        fanout=int(args.get("fanout") or 4),
        parent_agent=kw.get("parent_agent"),
    ),
    emoji="🌐",
)


__all__ = ["ORCHESTRATE_TASKS_SCHEMA", "orchestrate_tasks"]
