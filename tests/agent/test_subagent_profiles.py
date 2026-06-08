"""Tests for the named subagent profile registry."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.subagent_profiles import (
    SubagentProfile,
    SubagentProfileRegistry,
    discover_profiles,
    parse_profile,
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_minimal():
    text = (
        "---\n"
        "name: reviewer\n"
        "---\n"
        "You are a code reviewer.\n"
    )
    profile = parse_profile(text)
    assert profile is not None
    assert profile.name == "reviewer"
    assert profile.system_prompt == "You are a code reviewer."
    assert profile.toolsets is None
    assert profile.source == "user"


def test_parse_full_frontmatter():
    text = (
        "---\n"
        "name: security-auditor\n"
        "description: Finds OWASP-style issues\n"
        "toolsets:\n"
        "  - file\n"
        "  - search\n"
        "  - web\n"
        "model: openrouter/anthropic/claude-sonnet-4-6\n"
        "max_iterations: 25\n"
        "max_tokens: 4000\n"
        "permission_mode: acceptEdits\n"
        "---\n"
        "Audit the change for security issues.\n"
    )
    profile = parse_profile(text, source="project")
    assert profile is not None
    assert profile.name == "security-auditor"
    assert profile.description == "Finds OWASP-style issues"
    assert profile.toolsets == ["file", "search", "web"]
    assert profile.model == "openrouter/anthropic/claude-sonnet-4-6"
    assert profile.max_iterations == 25
    assert profile.max_tokens == 4000
    assert profile.permission_mode == "acceptEdits"
    assert profile.source == "project"


def test_parse_falls_back_to_default_name():
    text = (
        "---\n"
        "description: missing the name key\n"
        "---\n"
        "Body here.\n"
    )
    profile = parse_profile(text, default_name="from-filename")
    assert profile is not None
    assert profile.name == "from-filename"


def test_parse_toolsets_comma_string():
    text = (
        "---\n"
        "name: x\n"
        "toolsets: file, search, web\n"
        "---\n"
        "body\n"
    )
    profile = parse_profile(text)
    assert profile.toolsets == ["file", "search", "web"]


def test_parse_rejects_invalid_name():
    text = "---\nname: bad name with spaces\n---\nbody\n"
    assert parse_profile(text) is None
    text = "---\nname: 'has/slash'\n---\nbody\n"
    assert parse_profile(text) is None
    text = "---\nname: ''\n---\nbody\n"
    assert parse_profile(text) is None


def test_parse_rejects_empty_body():
    text = "---\nname: ok\n---\n"
    assert parse_profile(text) is None


def test_parse_invalid_yaml_falls_back():
    text = (
        "---\n"
        "name: ok\n"
        "toolsets: [not-valid-yaml,\n"  # broken list
        "---\n"
        "body\n"
    )
    profile = parse_profile(text)
    # Bad YAML returns dict={}, body preserved — name validation fails
    assert profile is None


def test_parse_no_frontmatter_with_default_name():
    text = "Just body text, no frontmatter.\n"
    profile = parse_profile(text, default_name="plain")
    assert profile is not None
    assert profile.name == "plain"
    assert profile.system_prompt == "Just body text, no frontmatter."


def test_parse_int_coercion():
    text = (
        "---\n"
        "name: x\n"
        "max_iterations: '15'\n"
        "max_tokens: not-a-number\n"
        "---\n"
        "body\n"
    )
    profile = parse_profile(text)
    assert profile.max_iterations == 15  # coerced from string
    assert profile.max_tokens is None     # invalid silently dropped


def test_parse_int_must_be_positive():
    text = (
        "---\n"
        "name: x\n"
        "max_iterations: 0\n"
        "---\n"
        "body\n"
    )
    profile = parse_profile(text)
    assert profile.max_iterations is None


def test_parse_unknown_keys_silently_ignored():
    text = (
        "---\n"
        "name: x\n"
        "unknown_key: hello\n"
        "another_typo: 42\n"
        "---\n"
        "body\n"
    )
    profile = parse_profile(text)
    assert profile is not None
    assert profile.name == "x"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_basics():
    p1 = SubagentProfile(name="a", system_prompt="A")
    p2 = SubagentProfile(name="b", system_prompt="B")
    reg = SubagentProfileRegistry([p1, p2])
    assert len(reg) == 2
    assert "a" in reg
    assert "b" in reg
    assert "missing" not in reg
    assert reg.get("a") is p1
    assert reg.names() == ["a", "b"]


def test_registry_project_overrides_user():
    user = SubagentProfile(name="x", system_prompt="USER", source="user")
    project = SubagentProfile(name="x", system_prompt="PROJECT",
                              source="project")
    reg = SubagentProfileRegistry([user])
    reg.register(project)
    assert reg.get("x").system_prompt == "PROJECT"


def test_registry_user_overrides_builtin():
    builtin = SubagentProfile(name="x", system_prompt="BUILTIN",
                              source="builtin")
    user = SubagentProfile(name="x", system_prompt="USER", source="user")
    reg = SubagentProfileRegistry([builtin])
    reg.register(user)
    assert reg.get("x").system_prompt == "USER"


def test_registry_does_not_downgrade():
    project = SubagentProfile(name="x", system_prompt="PROJECT",
                              source="project")
    user = SubagentProfile(name="x", system_prompt="USER", source="user")
    reg = SubagentProfileRegistry([project])
    reg.register(user)
    assert reg.get("x").system_prompt == "PROJECT"  # project wins


def test_registry_describe_serialisable():
    import json
    reg = SubagentProfileRegistry([
        SubagentProfile(name="a", system_prompt="A",
                        toolsets=["file"], model="m"),
    ])
    desc = reg.describe()
    assert len(desc) == 1
    json.dumps(desc)  # must be JSON-friendly


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _write_profile(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_discover_picks_up_user_dir(tmp_path):
    user_dir = tmp_path / "hermes-home" / "agents"
    _write_profile(
        user_dir / "code-reviewer.md",
        "---\nname: code-reviewer\n---\nReview code carefully.\n",
    )
    _write_profile(
        user_dir / "explorer.md",
        "---\nname: explorer\n---\nLocate files.\n",
    )
    reg = discover_profiles(cwd=tmp_path / "empty", user_agents_dir=user_dir)
    assert sorted(reg.names()) == ["code-reviewer", "explorer"]
    assert reg.get("code-reviewer").source == "user"


def test_discover_picks_up_project_dir(tmp_path):
    cwd = tmp_path / "proj"
    _write_profile(
        cwd / ".hermes" / "agents" / "api-docs.md",
        "---\nname: api-docs\n---\nWrite API docs.\n",
    )
    reg = discover_profiles(cwd=cwd, user_agents_dir=tmp_path / "missing")
    assert "api-docs" in reg
    assert reg.get("api-docs").source == "project"


def test_discover_project_overrides_user(tmp_path):
    user_dir = tmp_path / "hermes-home" / "agents"
    _write_profile(
        user_dir / "reviewer.md",
        "---\nname: reviewer\n---\nuser version\n",
    )
    cwd = tmp_path / "proj"
    _write_profile(
        cwd / ".hermes" / "agents" / "reviewer.md",
        "---\nname: reviewer\n---\nproject version\n",
    )
    reg = discover_profiles(cwd=cwd, user_agents_dir=user_dir)
    assert reg.get("reviewer").system_prompt == "project version"
    assert reg.get("reviewer").source == "project"


def test_discover_skips_non_md_files(tmp_path):
    user_dir = tmp_path / "agents"
    _write_profile(user_dir / "ok.md", "---\nname: ok\n---\nbody\n")
    _write_profile(user_dir / "README.txt", "not a profile")
    _write_profile(user_dir / "notes.markdown", "---\nname: x\n---\nbody\n")
    reg = discover_profiles(cwd=tmp_path / "noproj", user_agents_dir=user_dir)
    assert reg.names() == ["ok"]


def test_discover_recovers_from_one_bad_file(tmp_path):
    user_dir = tmp_path / "agents"
    _write_profile(
        user_dir / "good.md",
        "---\nname: good\n---\nworks\n",
    )
    _write_profile(
        user_dir / "broken.md",
        "---\nname: bad name with spaces!\n---\nbody\n",
    )
    reg = discover_profiles(cwd=tmp_path / "noproj", user_agents_dir=user_dir)
    assert reg.names() == ["good"]  # bad one skipped, others still load


def test_discover_handles_missing_dirs(tmp_path):
    reg = discover_profiles(
        cwd=tmp_path / "nowhere",
        user_agents_dir=tmp_path / "missing",
    )
    assert len(reg) == 0


def test_filename_supplies_default_name(tmp_path):
    user_dir = tmp_path / "agents"
    # No name in frontmatter — should default to filename stem
    _write_profile(
        user_dir / "from-file.md",
        "---\ndescription: x\n---\nBody.\n",
    )
    reg = discover_profiles(cwd=tmp_path / "noproj", user_agents_dir=user_dir)
    assert reg.get("from-file") is not None


def test_builtin_dir_lowest_precedence(tmp_path):
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    _write_profile(
        builtin / "shared.md",
        "---\nname: shared\n---\nBUILTIN\n",
    )
    _write_profile(
        user / "shared.md",
        "---\nname: shared\n---\nUSER\n",
    )
    reg = discover_profiles(
        cwd=tmp_path / "noproj",
        user_agents_dir=user,
        builtin_dirs=[builtin],
    )
    assert reg.get("shared").system_prompt == "USER"
    assert reg.get("shared").source == "user"
