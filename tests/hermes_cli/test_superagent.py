"""Tests for the hermes superagent <task> synthesis command."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.superagent import (
    PLAN_PROMPT_TEMPLATE,
    _new_lessons_since,
    _parse_plan,
    _strip_fences,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_strip_fences_with_lang_tag():
    raw = "```yaml\nfanout: 4\ntasks: []\n```"
    assert _strip_fences(raw) == "fanout: 4\ntasks: []"


def test_strip_fences_no_fence():
    raw = "fanout: 4\ntasks: []"
    assert _strip_fences(raw) == "fanout: 4\ntasks: []"


def test_strip_fences_only_backticks():
    raw = "```\nfoo: bar\n```"
    assert _strip_fences(raw) == "foo: bar"


def test_parse_plan_valid_yaml():
    yaml_text = "fanout: 4\ntasks:\n  - id: a\n    goal: do A\n"
    plan = _parse_plan(yaml_text)
    assert plan["fanout"] == 4
    assert plan["tasks"][0]["id"] == "a"


def test_parse_plan_strips_fences_first():
    yaml_text = "```yaml\nfanout: 2\ntasks: []\n```"
    plan = _parse_plan(yaml_text)
    assert plan["fanout"] == 2


def test_parse_plan_rejects_invalid_yaml():
    # Unclosed flow-mapping syntax — guaranteed parse error
    with pytest.raises(RuntimeError) as exc_info:
        _parse_plan("{ fanout: 4, tasks: [{ id: a, goal:")
    assert "valid YAML" in str(exc_info.value)


def test_parse_plan_rejects_non_mapping():
    with pytest.raises(RuntimeError):
        _parse_plan("[just, a, list]")


def test_plan_prompt_template_substitutes_task():
    rendered = PLAN_PROMPT_TEMPLATE.format(task="Fix OAuth bug")
    assert "Fix OAuth bug" in rendered
    assert "fanout" in rendered  # references YAML structure
    assert "depends_on" in rendered


def test_plan_prompt_demands_yaml_only_output():
    """The planner must produce JUST YAML, no prose — locked in
    because run_superagent assumes the response parses as YAML."""
    assert "EXACTLY one YAML" in PLAN_PROMPT_TEMPLATE
    assert "No prose" in PLAN_PROMPT_TEMPLATE
    assert "No markdown code fences" in PLAN_PROMPT_TEMPLATE


# ---------------------------------------------------------------------------
# Lessons-corpus delta
# ---------------------------------------------------------------------------


def test_new_lessons_since_empty_baseline(tmp_path):
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "b.md").write_text("b", encoding="utf-8")
    new = _new_lessons_since(tmp_path, set())
    assert {p.name for p in new} == {"a.md", "b.md"}


def test_new_lessons_since_filters_existing(tmp_path):
    existing = tmp_path / "old.md"
    existing.write_text("old", encoding="utf-8")
    baseline = {existing}
    # Add a new lesson
    (tmp_path / "new.md").write_text("new", encoding="utf-8")
    new = _new_lessons_since(tmp_path, baseline)
    assert len(new) == 1
    assert new[0].name == "new.md"


def test_new_lessons_since_missing_dir():
    assert _new_lessons_since(Path("/does/not/exist"), set()) == []


# ---------------------------------------------------------------------------
# End-to-end (dry-run only — no live LLM)
# ---------------------------------------------------------------------------


def test_run_superagent_rejects_empty_task():
    from hermes_cli.superagent import run_superagent
    rc = run_superagent("")
    assert rc == 2


def test_run_superagent_dry_run_with_mocked_planner(monkeypatch, tmp_path):
    """Verify the dry-run path: mock the planner to return a valid YAML
    plan, assert dry-run prints the plan without dispatching."""
    from hermes_cli import superagent as sa
    fake_yaml = (
        "fanout: 2\n"
        "tasks:\n"
        "  - id: server\n"
        "    goal: do the server work\n"
        "    subagent_type: code-reviewer\n"
        "  - id: tests\n"
        "    goal: write tests\n"
        "    depends_on: [server]\n"
    )
    # Mock the planner oneshot to return the YAML.
    def fake_planner(prompt, model=None, provider=None):
        return fake_yaml, {"estimated_cost_usd": 0.001, "total_tokens": 250,
                           "model": "test-model"}
    monkeypatch.setattr(sa, "_run_agent_with_details", fake_planner,
                         raising=False)
    # Insert the mock into the module's import path so the function uses it.
    monkeypatch.setattr(
        "hermes_cli.oneshot._run_agent_with_details", fake_planner,
    )
    # Redirect lessons dir.
    monkeypatch.setattr(
        "agent.lessons._lessons_dir", lambda: tmp_path / "lessons",
    )
    # Capture stdout to verify the dry-run prints the plan.
    from io import StringIO
    captured = StringIO()
    monkeypatch.setattr("sys.stdout", captured)
    rc = sa.run_superagent(
        "Test task", output_format="json", dry_run=True,
    )
    assert rc == 0
    output = captured.getvalue()
    envelope = json.loads(output.strip())
    assert envelope["type"] == "superagent_dry_run"
    assert envelope["task_count"] == 2
    assert envelope["fanout"] == 2
    assert envelope["plan_cost_usd"] == 0.001
