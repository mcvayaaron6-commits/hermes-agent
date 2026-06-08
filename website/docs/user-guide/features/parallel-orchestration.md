---
sidebar_position: 13
title: "Parallel Orchestration"
description: "Spin up multiple subagents that coordinate via a bus, share file locks, and execute as a DAG."
---

# Parallel Orchestration

The architectural piece behind "spinning up other agents that all work together in parallel." Before this layer, Hermes's `delegate_task` was parent→children with thread-pool execution; children couldn't talk to each other, couldn't coordinate on shared resources, and the parent only saw summaries when they finished.

Three modules close the gap:

| Module | What it does |
|---|---|
| `agent/bus.py` | Typed in-process pub/sub for inter-agent events |
| `agent/file_locks.py` | Optimistic file-level mutual exclusion |
| `agent/orchestrator.py` | DAG executor that runs tasks in parallel up to a fanout cap |

## The bus

```python
from agent.bus import (
    Bus, get_bus, subscribe,
    publish_tool_started, publish_lesson_captured,
    ToolStartedEvent, LessonCapturedEvent,
)

bus = get_bus()

# Subscribe by event type — IDE autocomplete + no string typos
bus.subscribe(handle_tool_event, event_type=ToolStartedEvent)

# Subscribe by glob pattern
bus.subscribe(handle_any_agent_event, pattern="agent.*.tool_started")

# Subscribe by exact subject
bus.subscribe(handle_specific_agent, subject="agent.42.tool_started")

# Publish (non-blocking — handlers run on a thread pool)
publish_tool_started("agent-42", "write_file", args_preview="{path: x.txt}")
```

Subjects are hierarchical dotted strings. Cross-process transport (NATS / Redis) can swap behind the same `Bus` protocol without changing callers — that's how Hermes goes from single-machine parallel to distributed fleet without churning the agent layer.

### Standard event types

| Type | Subject pattern | When |
|---|---|---|
| `AgentSpawnedEvent` | `agent.<id>.spawned` | Subagent constructed |
| `AgentFinishedEvent` | `agent.<id>.finished` | Subagent terminated |
| `ToolStartedEvent` | `agent.<id>.tool_started` | About to invoke a tool |
| `ToolFinishedEvent` | `agent.<id>.tool_finished` | Tool call returned |
| `FileLockAcquiredEvent` | `file.lock.acquired` | Path locked for write |
| `FileLockReleasedEvent` | `file.lock.released` | Lock released |
| `LessonCapturedEvent` | `lesson.captured` | New lesson learned |
| `OrchestratorTaskCompletedEvent` | `orchestrator.<id>.task.<tid>.completed` | DAG node finished |

Every event has a stable Python dataclass payload — subscribers register against the type, not the string subject, so plugin code gets IDE autocomplete and there are fewer typo bugs.

## File locks

```python
from agent.file_locks import file_locks, LockOutcome

with file_locks().acquire("/path/to/x.py", owner="agent-A", timeout=5.0) as outcome:
    if outcome in (LockOutcome.GRANTED, LockOutcome.EXPIRED):
        # Safe to write
        ...
    elif outcome is LockOutcome.BUSY:
        # Another agent owns the lock — back off or queue
        ...
    elif outcome is LockOutcome.REENTRANT:
        # Same agent already owns it — write freely, outer scope releases
        ...
```

- **Exclusive locks** by absolute path with a per-agent owner id.
- **TTL expiry** (default 5 min) so a crashed agent doesn't strand the lock.
- **Reentrant** — same owner re-acquiring refreshes the TTL without blocking.
- **Bus notifications** via `FileLockAcquiredEvent` / `FileLockReleasedEvent` — sibling agents can subscribe and back off proactively.
- **Path normalisation** handles `/tmp/./x.py == /tmp/x.py` collisions.

## The orchestrator

```python
from agent.orchestrator import (
    Orchestrator, TaskSpec, TaskState, build_default_executor,
)

tasks = [
    TaskSpec(id="lint",  goal="Run ruff on src/", subagent_type="Explore"),
    TaskSpec(id="tests", goal="Run pytest tests/", subagent_type="test-writer",
             depends_on=["lint"]),
    TaskSpec(id="docs",  goal="Update docs/api.md",
             write_paths=["docs/api.md"]),  # serialised with anyone else
                                            # writing the same file
    TaskSpec(id="ship",  goal="Open PR with results",
             depends_on=["tests", "docs"]),
]

orch = Orchestrator(
    build_default_executor(parent_agent=self.agent),
    fanout=4,
)
result = orch.run(tasks)
# result.task("lint").output, result.task("tests").state, ...
```

What you get:

- **Topology**: cycle detection at submit time (Kahn's algorithm); invalid DAG raises `ValueError`.
- **Parallel execution**: tasks whose dependencies are all satisfied run concurrently up to `fanout`.
- **Output piping**: each task sees a snapshot of upstream outputs in its context (snapshot at submission time — siblings don't bleed into each other's view).
- **File-lock arbitration**: `build_default_executor` acquires write-locks for every path in `spec.write_paths` before invoking the subagent — conflicting writers serialise without serialising the whole DAG.
- **Failure semantics**: an upstream failure auto-SKIPS downstream tasks (configurable per task via `requires_upstream_success=False`).
- **Live bus events**: every spawn / complete publishes through the bus, so the future React-Flow topology view just subscribes and renders.

## End-to-end example: parallel feature ship

```python
from agent.orchestrator import Orchestrator, TaskSpec, build_default_executor

# A PR that touches three independent areas — perfect for parallel work.
tasks = [
    TaskSpec(id="server",
             goal="Add /v2/users endpoint to api/server.py",
             subagent_type="code-reviewer",
             write_paths=["api/server.py"]),
    TaskSpec(id="schema",
             goal="Add User v2 schema to api/schemas/user.py",
             write_paths=["api/schemas/user.py"]),
    TaskSpec(id="tests",
             goal="Add tests for /v2/users covering happy path + 4 edges",
             subagent_type="test-writer",
             depends_on=["server", "schema"],
             write_paths=["tests/api/test_users_v2.py"]),
    TaskSpec(id="docs",
             goal="Document /v2/users in docs/api/users.md",
             depends_on=["server"],
             write_paths=["docs/api/users.md"]),
]

orch = Orchestrator(
    build_default_executor(parent_agent),
    fanout=4,
)
result = orch.run(tasks)

if result.succeeded:
    print(f"All {len(tasks)} parallel tasks completed in "
          f"{result.total_duration_seconds:.1f}s "
          f"(would have been ~{sum(r.duration_seconds for r in result.results.values()):.1f}s serial).")
```

`server` and `schema` run in parallel (no dependency). `tests` and `docs` start as soon as their deps land — `tests` waits for both `server` and `schema`; `docs` only waits for `server`. The file-lock layer prevents two tasks from clobbering each other on shared paths even if their goals overlap.

## Cost model

- **Single-machine fanout** is constrained by your provider rate limits and your local thread pool size (default 4). Higher fanouts trade more parallelism for more concurrent LLM API calls.
- **Distributed mode** (future): swap `get_bus()` for a NATS-backed bus and `file_locks()` for Redis SETNX — same Python surface, agents now coordinate across machines.
- **Verification adds 1x per task** when `verification.enabled` is on; with consensus quorum it's N×.

## When parallel orchestration shines

- **Independent file ranges** in the same task (different modules, different test files).
- **Bench / lint / typecheck / build** runs that have no dependencies on each other.
- **Multi-file refactors** where you can shard by directory.
- **Documentation updates** that mirror code changes — start the docs task as soon as code is in.

## When it doesn't help

- **Sequential dependencies** all the way down — the orchestrator can't parallelise a chain.
- **Tasks under ~10 seconds** — fanout overhead dominates; just delegate normally.
- **File-heavy tasks that conflict** — every conflict serialises; the orchestrator helps less than batching by hand.

## What you don't have to do

Subscribe to the bus to make it run — orchestrator events are already published and the audit log already records them. Add subscribers only when you want to react (build a topology UI, ship trajectories to a training run, gate on a custom rule).
