"""Tests for the Plan Mode engine."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.plan_mode import (
    DEFAULT_PLAN_MODE_ALLOWLIST,
    PLAN_MODE_REFUSAL_TEMPLATE,
    PlanArtifact,
    PlanModeState,
    PlanStep,
    default_plan_path,
    parse_steps_from_markdown,
    read_plan_artifact,
    slugify,
    write_plan_artifact,
)


# ---------------------------------------------------------------------------
# Allow-list semantics
# ---------------------------------------------------------------------------


def test_allows_all_when_disabled():
    state = PlanModeState(enabled=False)
    assert state.is_tool_allowed("write_file")
    assert state.is_tool_allowed("terminal")
    assert state.is_tool_allowed("anything_at_all")


def test_blocks_unknown_tool_when_enabled():
    state = PlanModeState()
    state.enter(task="x", plan_path=Path("p.md"))
    assert not state.is_tool_allowed("write_file")
    assert not state.is_tool_allowed("patch")
    assert not state.is_tool_allowed("terminal")
    assert not state.is_tool_allowed("send_message")
    assert not state.is_tool_allowed("image_generate")


def test_allows_readonly_tools_when_enabled():
    state = PlanModeState()
    state.enter(task="x", plan_path=Path("p.md"))
    for tool in ("read_file", "list_directory", "search_files",
                 "web_search", "web_fetch",
                 "session_search", "session_insights",
                 "skill_search", "skill_browse", "skill_view",
                 "memory_read", "todo", "clarify"):
        assert state.is_tool_allowed(tool), tool


def test_delegate_task_blocked_in_plan_mode():
    """delegate_task must be blocked because its spawned child agent
    does NOT inherit plan mode — letting it through would be a
    read-only escape hatch."""
    state = PlanModeState()
    state.enter(task="x", plan_path=Path("p.md"))
    assert not state.is_tool_allowed("delegate_task")


def test_action_filter_memory():
    state = PlanModeState()
    state.enter(task="x", plan_path=Path("p.md"))
    # The 'memory' tool is mode-aware: allowed for read/list/get,
    # blocked for write or any other action.
    assert state.is_tool_allowed("memory", args={"action": "read"})
    assert state.is_tool_allowed("memory", args={"action": "list"})
    assert state.is_tool_allowed("memory", args={"action": "get"})
    assert not state.is_tool_allowed("memory", args={"action": "write"})
    assert not state.is_tool_allowed("memory", args={"action": "delete"})
    # Missing action -> blocked (don't open a hole).
    assert not state.is_tool_allowed("memory", args={})
    assert not state.is_tool_allowed("memory")


def test_action_filter_kanban_show_only():
    state = PlanModeState()
    state.enter(task="x", plan_path=Path("p.md"))
    assert state.is_tool_allowed("kanban", args={"action": "show"})
    assert state.is_tool_allowed("kanban", args={"action": "list"})
    assert not state.is_tool_allowed("kanban", args={"action": "create"})
    assert not state.is_tool_allowed("kanban", args={"action": "complete"})


def test_extra_allow_overrides_default():
    state = PlanModeState()
    state.enter(task="x", plan_path=Path("p.md"))
    assert not state.is_tool_allowed("send_message")
    state.configure(allow_tools=["send_message"])
    assert state.is_tool_allowed("send_message")


def test_extra_deny_overrides_allowlist():
    state = PlanModeState()
    state.enter(task="x", plan_path=Path("p.md"))
    assert state.is_tool_allowed("read_file")
    state.configure(deny_tools=["read_file"])
    assert not state.is_tool_allowed("read_file")


def test_extra_deny_beats_extra_allow():
    state = PlanModeState()
    state.enter(task="x", plan_path=Path("p.md"))
    state.configure(allow_tools=["send_message"], deny_tools=["send_message"])
    assert not state.is_tool_allowed("send_message")


def test_refusal_message_mentions_plan_path():
    state = PlanModeState()
    state.enter(task="x", plan_path=Path("/tmp/plans/x.md"))
    msg = state.refusal_for("write_file")
    assert "Plan Mode" in msg
    assert "write_file" in msg
    assert "/tmp/plans/x.md" in msg


def test_refusal_template_does_not_break_when_plan_path_unset():
    state = PlanModeState()
    state.enter(task="x", plan_path=None)
    msg = state.refusal_for("write_file")
    assert "write_file" in msg
    assert "Plan Mode" in msg


# ---------------------------------------------------------------------------
# Slug / path helpers
# ---------------------------------------------------------------------------


def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"
    assert slugify("  Trim Me  ") == "trim-me"
    assert slugify("Mixed_Case-123!") == "mixed-case-123"


def test_slugify_collapses_runs():
    assert slugify("foo!!!bar???baz") == "foo-bar-baz"


def test_slugify_fallback():
    assert slugify("") == "plan"
    assert slugify("!!!") == "plan"
    assert slugify(None) == "plan"  # type: ignore[arg-type]


def test_slugify_truncates_long_input():
    long = "x" * 200
    assert len(slugify(long)) <= 48


def test_default_plan_path_pattern(tmp_path):
    now = time.mktime(time.strptime("2026-05-16 14:32:00", "%Y-%m-%d %H:%M:%S"))
    p = default_plan_path(tmp_path / "plans", "Add OAuth login", now=now)
    assert p.parent == tmp_path / "plans"
    assert p.name == "add-oauth-login-20260516-143200.md"


# ---------------------------------------------------------------------------
# Plan artifact roundtrip
# ---------------------------------------------------------------------------


def test_plan_artifact_renders_all_sections():
    plan = PlanArtifact(
        task="Add login",
        context="we need it",
        investigation="found 3 files",
        approach="use OAuth",
        files_to_modify=["src/auth.py", "tests/test_auth.py"],
        steps=[PlanStep("install dep"), PlanStep("add route", done=True)],
        verification="run pytest tests/test_auth.py",
    )
    md = plan.render_markdown()
    assert "# Plan: Add login" in md
    assert "## Context" in md
    assert "## Investigation Notes" in md
    assert "## Approach" in md
    assert "## Files to Modify" in md
    assert "## Steps" in md
    assert "## Verification" in md
    assert "- `src/auth.py`" in md
    assert "1. [ ] install dep" in md
    assert "2. [x] add route" in md


def test_plan_artifact_renders_placeholders_for_empty_sections():
    plan = PlanArtifact(task="empty plan")
    md = plan.render_markdown()
    assert "_(none)_" in md
    assert "_(no steps yet)_" in md


def test_write_and_read_roundtrip(tmp_path):
    plan = PlanArtifact(
        task="Refactor auth",
        context="legacy code is brittle",
        approach="extract module",
        files_to_modify=["a.py", "b.py"],
        steps=[
            PlanStep("extract helper"),
            PlanStep("update callers", done=True),
            PlanStep("add tests"),
        ],
        verification="pytest",
    )
    out = write_plan_artifact(plan, tmp_path / "plans" / "p.md")
    assert out.exists()
    parsed = read_plan_artifact(out)
    assert parsed is not None
    assert parsed.task == "Refactor auth"
    assert parsed.context.strip() == "legacy code is brittle"
    assert parsed.approach.strip() == "extract module"
    assert parsed.files_to_modify == ["a.py", "b.py"]
    assert len(parsed.steps) == 3
    assert parsed.steps[1].done is True
    assert parsed.steps[1].description == "update callers"
    assert parsed.steps[0].done is False


def test_read_plan_artifact_missing_returns_none(tmp_path):
    assert read_plan_artifact(tmp_path / "missing.md") is None


def test_parse_steps_skips_non_step_lines():
    md = (
        "Some intro.\n"
        "\n"
        "## Steps\n"
        "\n"
        "Some prose before the list.\n"
        "1. [ ] first\n"
        "   continuation that isn't a step\n"
        "2. [x] second\n"
        "not a step\n"
        "3. [ ] third\n"
        "\n"
        "## Verification\n"
        "4. [ ] post-steps numbered line should not be picked up\n"
    )
    steps = parse_steps_from_markdown(md)
    assert [s.description for s in steps] == ["first", "second", "third"]
    assert [s.done for s in steps] == [False, True, False]


def test_state_snapshot_serializable():
    state = PlanModeState()
    state.enter(task="x", plan_path=Path("/tmp/p.md"))
    state.configure(allow_tools=["a"], deny_tools=["b"])
    snap = state.snapshot()
    assert snap["enabled"] is True
    assert snap["task"] == "x"
    assert snap["plan_path"] == "/tmp/p.md"
    assert "a" in snap["extra_allow"]
    assert "b" in snap["extra_deny"]
    assert sorted(DEFAULT_PLAN_MODE_ALLOWLIST) == snap["allowlist"]
    # Must be JSON-serialisable.
    import json
    json.dumps(snap)


def test_exit_keeps_metadata_for_post_exit_reads(tmp_path):
    state = PlanModeState()
    state.enter(task="x", plan_path=tmp_path / "p.md")
    state.exit()
    assert not state.enabled
    # After exit we still want plan_path so the agent loop can seed todos.
    assert state.plan_path == tmp_path / "p.md"
    assert state.task == "x"


def test_reset_clears_everything(tmp_path):
    state = PlanModeState()
    state.enter(task="x", plan_path=tmp_path / "p.md")
    state.configure(allow_tools=["a"], deny_tools=["b"])
    state.reset()
    assert not state.enabled
    assert state.task == ""
    assert state.plan_path is None
    assert state.extra_allow == set()
    assert state.extra_deny == set()


def test_template_formatting_does_not_raise_on_unicode():
    state = PlanModeState()
    state.enter(task="日本語", plan_path=Path("計画.md"))
    msg = state.refusal_for("write_file")
    assert "計画.md" in msg
