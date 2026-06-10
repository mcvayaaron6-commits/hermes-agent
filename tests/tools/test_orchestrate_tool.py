"""Tests for the orchestrate_tasks tool the model can call."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _StubParent:
    """Minimal parent_agent stand-in — orchestrate_tasks only needs it
    to thread through to build_default_executor; we monkeypatch the
    actual executor below."""
    pass


def _stub_executor_factory(success: bool = True):
    """Build a TaskExecutor that succeeds or fails based on the flag."""
    from agent.orchestrator import TaskResult, TaskState
    def _exec(spec, prior):
        if not success:
            return TaskResult(task_id=spec.id, state=TaskState.FAILED,
                              error="stub-fail")
        return TaskResult(
            task_id=spec.id, state=TaskState.SUCCEEDED,
            output=f"stub output for {spec.id}",
            iterations_used=1,
        )
    return _exec


@pytest.fixture
def patched_executor(monkeypatch):
    """Replace build_default_executor so tests don't try to spin up
    real subagents."""
    def _factory(parent_agent=None):
        return _stub_executor_factory(success=True)
    monkeypatch.setattr(
        "agent.orchestrator.build_default_executor", _factory,
    )
    yield


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_runs_single_task(patched_executor):
    from tools.orchestrate_tool import orchestrate_tasks
    result = json.loads(orchestrate_tasks(
        tasks=[{"id": "solo", "goal": "Do the thing"}],
        parent_agent=_StubParent(),
    ))
    assert result["succeeded"] is True
    assert len(result["results"]) == 1
    assert result["results"][0]["state"] == "succeeded"


def test_runs_diamond_dag(patched_executor):
    from tools.orchestrate_tool import orchestrate_tasks
    result = json.loads(orchestrate_tasks(
        tasks=[
            {"id": "a", "goal": "A"},
            {"id": "b", "goal": "B", "depends_on": ["a"]},
            {"id": "c", "goal": "C", "depends_on": ["a"]},
            {"id": "d", "goal": "D", "depends_on": ["b", "c"]},
        ],
        parent_agent=_StubParent(),
    ))
    assert result["succeeded"] is True
    assert len(result["results"]) == 4


def test_result_output_truncated_to_8k(patched_executor):
    from tools.orchestrate_tool import orchestrate_tasks
    from agent.orchestrator import TaskResult, TaskState
    # Override the executor to return a huge output.
    def huge_exec(spec, prior):
        return TaskResult(task_id=spec.id, state=TaskState.SUCCEEDED,
                          output="x" * 20_000)
    with patch("agent.orchestrator.build_default_executor",
               lambda parent_agent=None: huge_exec):
        result = json.loads(orchestrate_tasks(
            tasks=[{"id": "big", "goal": "big output"}],
            parent_agent=_StubParent(),
        ))
    assert len(result["results"][0]["output"]) <= 8_000


def test_fanout_clamped_to_safe_range(patched_executor):
    from tools.orchestrate_tool import orchestrate_tasks
    # fanout=1000 should be clamped to 16
    result = json.loads(orchestrate_tasks(
        tasks=[{"id": "a", "goal": "x"}],
        fanout=1000,
        parent_agent=_StubParent(),
    ))
    assert result["succeeded"] is True
    # 0 or negative fanout clamped to 1
    result = json.loads(orchestrate_tasks(
        tasks=[{"id": "a", "goal": "x"}],
        fanout=0,
        parent_agent=_StubParent(),
    ))
    assert result["succeeded"] is True


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_missing_parent_agent_errors():
    from tools.orchestrate_tool import orchestrate_tasks
    result = json.loads(orchestrate_tasks(tasks=[{"id": "x", "goal": "y"}]))
    assert "error" in result
    assert "parent agent" in result["error"].lower()


def test_empty_tasks_errors():
    from tools.orchestrate_tool import orchestrate_tasks
    result = json.loads(orchestrate_tasks(
        tasks=[], parent_agent=_StubParent(),
    ))
    assert "error" in result


def test_missing_id_errors():
    from tools.orchestrate_tool import orchestrate_tasks
    result = json.loads(orchestrate_tasks(
        tasks=[{"goal": "no id"}], parent_agent=_StubParent(),
    ))
    assert "error" in result
    assert "id" in result["error"].lower()


def test_missing_goal_errors():
    from tools.orchestrate_tool import orchestrate_tasks
    result = json.loads(orchestrate_tasks(
        tasks=[{"id": "x"}], parent_agent=_StubParent(),
    ))
    assert "error" in result
    assert "goal" in result["error"].lower()


def test_non_object_task_errors():
    from tools.orchestrate_tool import orchestrate_tasks
    result = json.loads(orchestrate_tasks(
        tasks=["not an object"], parent_agent=_StubParent(),
    ))
    assert "error" in result


def test_cycle_in_dag_errors(patched_executor):
    from tools.orchestrate_tool import orchestrate_tasks
    result = json.loads(orchestrate_tasks(
        tasks=[
            {"id": "a", "goal": "A", "depends_on": ["b"]},
            {"id": "b", "goal": "B", "depends_on": ["a"]},
        ],
        parent_agent=_StubParent(),
    ))
    assert "error" in result
    assert "invalid DAG" in result["error"]


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_tool_is_registered():
    import tools.orchestrate_tool  # noqa: F401 — triggers registration
    from tools.registry import registry
    delegation_tools = registry.get_tool_names_for_toolset("delegation")
    assert "orchestrate_tasks" in delegation_tools


def test_schema_shape_is_stable():
    from tools.orchestrate_tool import ORCHESTRATE_TASKS_SCHEMA
    # Lock in the keys CI scripts may parse on.
    assert ORCHESTRATE_TASKS_SCHEMA["type"] == "function"
    fn = ORCHESTRATE_TASKS_SCHEMA["function"]
    assert fn["name"] == "orchestrate_tasks"
    params = fn["parameters"]
    assert "tasks" in params["properties"]
    assert "fanout" in params["properties"]
    assert params["required"] == ["tasks"]
    task_props = params["properties"]["tasks"]["items"]["properties"]
    # Every documented task field is present
    for key in ("id", "goal", "subagent_type", "toolsets",
                "depends_on", "write_paths", "max_iterations",
                "requires_upstream_success"):
        assert key in task_props
