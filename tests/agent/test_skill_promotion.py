"""Tests for the moat-10x skill-promotion engine."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.lessons import Lesson, save_lesson
from agent.skill_promotion import (
    PromotionCandidate,
    filter_already_installed,
    find_profile_candidates_from_usage,
    find_skill_candidates,
    install_candidate,
)


def _make_lesson(task: str, tags, fix: str = "do the thing"):
    return Lesson(
        task=task,
        initial_approach="naive way",
        verifier_complaint="that didn't work",
        fix=fix,
        tags=list(tags),
    )


# ---------------------------------------------------------------------------
# Skill promotion from lesson clustering
# ---------------------------------------------------------------------------


def test_three_lessons_sharing_two_tags_promotes(tmp_path):
    lessons = [
        _make_lesson("OAuth bug #1", ["oauth", "auth", "callback"]),
        _make_lesson("OAuth bug #2", ["oauth", "auth", "redirect"]),
        _make_lesson("OAuth bug #3", ["oauth", "auth", "scope"]),
    ]
    candidates = find_skill_candidates(lessons=lessons, threshold=3,
                                        min_shared_tags=2)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.kind == "skill"
    assert "oauth" in c.shared_tags
    assert "auth" in c.shared_tags
    assert len(c.supporting_lesson_paths) == 3


def test_two_lessons_below_threshold_no_promotion():
    lessons = [
        _make_lesson("OAuth bug #1", ["oauth", "auth"]),
        _make_lesson("OAuth bug #2", ["oauth", "auth"]),
    ]
    candidates = find_skill_candidates(lessons=lessons, threshold=3,
                                        min_shared_tags=2)
    assert candidates == []


def test_unrelated_lessons_dont_cluster():
    lessons = [
        _make_lesson("OAuth bug", ["oauth", "auth"]),
        _make_lesson("DB migration", ["database", "migration"]),
        _make_lesson("UI tweak", ["ui", "styling"]),
    ]
    candidates = find_skill_candidates(lessons=lessons, threshold=3,
                                        min_shared_tags=2)
    assert candidates == []


def test_cluster_separates_overlapping_groups():
    """Two distinct clusters of 3 each should produce 2 candidates."""
    lessons = [
        _make_lesson("OAuth bug #1", ["oauth", "auth", "redirect"]),
        _make_lesson("OAuth bug #2", ["oauth", "auth", "token"]),
        _make_lesson("OAuth bug #3", ["oauth", "auth", "scope"]),
        _make_lesson("DB issue #1", ["database", "migration", "schema"]),
        _make_lesson("DB issue #2", ["database", "migration", "rollback"]),
        _make_lesson("DB issue #3", ["database", "migration", "indexes"]),
    ]
    candidates = find_skill_candidates(lessons=lessons, threshold=3,
                                        min_shared_tags=2)
    assert len(candidates) == 2
    kinds = {c.shared_tags[0] for c in candidates}
    assert kinds == {"auth", "migration"} or kinds == {"oauth", "database"} or \
           "oauth" in {c.shared_tags[0] for c in candidates}


def test_candidate_body_includes_fixes_from_lessons():
    lessons = [
        _make_lesson("OAuth #1", ["oauth", "auth"],
                     fix="Read OAUTH_REDIRECT from env"),
        _make_lesson("OAuth #2", ["oauth", "auth"],
                     fix="Use HTTPS callback URLs"),
        _make_lesson("OAuth #3", ["oauth", "auth"],
                     fix="Validate the state parameter"),
    ]
    candidates = find_skill_candidates(lessons=lessons, threshold=3,
                                        min_shared_tags=2)
    body = candidates[0].body_markdown
    assert "OAUTH_REDIRECT" in body
    assert "HTTPS callback" in body
    assert "state parameter" in body


def test_candidate_name_is_deterministic():
    lessons = [
        _make_lesson("X #1", ["oauth", "auth", "callback"]),
        _make_lesson("X #2", ["oauth", "auth", "callback"]),
        _make_lesson("X #3", ["oauth", "auth", "callback"]),
    ]
    c1 = find_skill_candidates(lessons=lessons, threshold=3,
                                min_shared_tags=2)[0]
    c2 = find_skill_candidates(lessons=lessons, threshold=3,
                                min_shared_tags=2)[0]
    assert c1.name == c2.name


def test_candidate_score_rewards_larger_clusters():
    small = [
        _make_lesson(f"X #{i}", ["alpha", "beta"]) for i in range(3)
    ]
    large = [
        _make_lesson(f"Y #{i}", ["gamma", "delta"]) for i in range(7)
    ]
    candidates = find_skill_candidates(
        lessons=small + large, threshold=3, min_shared_tags=2,
    )
    # larger cluster scores higher
    assert candidates[0].score > candidates[-1].score


def test_dedupes_identical_fix_text():
    lessons = [
        _make_lesson(f"X #{i}", ["t1", "t2"],
                     fix="Always sanitize input before SQL execution")
        for i in range(3)
    ]
    candidates = find_skill_candidates(lessons=lessons, threshold=3,
                                        min_shared_tags=2)
    body = candidates[0].body_markdown
    # Only one bullet for the same fix text
    assert body.count("Always sanitize input") == 1


def test_zero_lessons_no_crash():
    assert find_skill_candidates(lessons=[], threshold=3, min_shared_tags=2) == []


# ---------------------------------------------------------------------------
# Profile promotion from skill usage
# ---------------------------------------------------------------------------


def test_high_use_skill_promotes_to_profile():
    usage = {"learned-oauth-auth": 7, "learned-database-migration": 2}
    candidates = find_profile_candidates_from_usage(usage, threshold=5)
    names = {c.name for c in candidates}
    assert "learned-oauth-auth" in names
    assert "learned-database-migration" not in names


def test_profile_body_references_source_skill():
    usage = {"learned-oauth": 10}
    candidates = find_profile_candidates_from_usage(usage, threshold=5)
    assert "learned-oauth" in candidates[0].body_markdown


def test_profile_body_carries_skill_guidance():
    usage = {"x": 6}
    skill_body = (
        "---\nname: x\n---\n# X\n## What to do\n"
        "1. Step one\n2. Step two\n## When this applies\n..."
    )
    candidates = find_profile_candidates_from_usage(
        usage, skill_bodies={"x": skill_body}, threshold=5,
    )
    assert "Step one" in candidates[0].body_markdown


def test_profile_promotion_below_threshold_skipped():
    usage = {"x": 2}
    candidates = find_profile_candidates_from_usage(usage, threshold=5)
    assert candidates == []


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def test_install_skill_writes_file(tmp_path):
    c = PromotionCandidate(
        kind="skill", name="my-skill",
        title="t", description="d",
        shared_tags=["a"], supporting_lesson_paths=[],
        body_markdown="# Skill body\n", score=1.0,
    )
    path = install_candidate(c, skills_dir=tmp_path)
    assert path.exists()
    assert path.parent.name == "my-skill"
    assert path.name == "SKILL.md"
    assert "Skill body" in path.read_text(encoding="utf-8")


def test_install_profile_writes_file(tmp_path):
    c = PromotionCandidate(
        kind="subagent_profile", name="my-profile",
        title="t", description="d",
        shared_tags=[], supporting_lesson_paths=[],
        body_markdown="# Profile body\n", score=1.0,
    )
    path = install_candidate(c, agents_dir=tmp_path)
    assert path.exists()
    assert path.name == "my-profile.md"


def test_install_unknown_kind_raises(tmp_path):
    c = PromotionCandidate(
        kind="bogus", name="x", title="t", description="d",
        shared_tags=[], supporting_lesson_paths=[],
        body_markdown="", score=0.0,
    )
    with pytest.raises(ValueError):
        install_candidate(c, skills_dir=tmp_path, agents_dir=tmp_path)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_filter_skips_already_installed_skill(tmp_path):
    c = PromotionCandidate(
        kind="skill", name="installed",
        title="t", description="d",
        shared_tags=[], supporting_lesson_paths=[],
        body_markdown="x", score=1.0,
    )
    # Pre-install it
    install_candidate(c, skills_dir=tmp_path)
    # filter should now drop it
    filtered = filter_already_installed([c], skills_dir=tmp_path)
    assert filtered == []


def test_filter_keeps_new_candidate(tmp_path):
    c = PromotionCandidate(
        kind="skill", name="never-installed",
        title="t", description="d",
        shared_tags=[], supporting_lesson_paths=[],
        body_markdown="x", score=1.0,
    )
    filtered = filter_already_installed([c], skills_dir=tmp_path)
    assert len(filtered) == 1


# ---------------------------------------------------------------------------
# Integration with on-disk lessons corpus
# ---------------------------------------------------------------------------


def test_find_candidates_reads_corpus_from_disk(tmp_path):
    for i in range(3):
        save_lesson(
            Lesson(task=f"OAuth bug #{i}",
                   initial_approach="naive",
                   verifier_complaint="failed",
                   fix=f"fix {i}",
                   tags=["oauth", "auth"]),
            lessons_dir=tmp_path,
        )
    from agent.lessons import all_lessons
    lessons = all_lessons(lessons_dir=tmp_path)
    candidates = find_skill_candidates(
        lessons=lessons, threshold=3, min_shared_tags=2,
    )
    assert len(candidates) == 1
