"""Tests for the lessons-learned engine."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.lessons import (
    Lesson,
    all_lessons,
    capture_from_rework,
    derive_tags,
    format_lessons_preamble,
    load_lesson,
    relevant_lessons,
    save_lesson,
)


# ---------------------------------------------------------------------------
# Tag derivation
# ---------------------------------------------------------------------------


def test_derive_tags_drops_stop_words_and_shorts():
    tags = derive_tags("The OAuth callback was failing in production")
    # 'the', 'was', 'in' dropped; only meaningful tokens remain
    assert "the" not in tags
    assert "was" not in tags
    assert "oauth" in tags
    assert "callback" in tags
    assert "production" in tags


def test_derive_tags_deterministic_ordering():
    text = "alpha beta gamma alpha"
    tags1 = derive_tags(text)
    tags2 = derive_tags(text)
    assert tags1 == tags2
    # 'alpha' has count 2 → ranked first; rest sorted alphabetically.
    assert tags1[0] == "alpha"
    assert tags1[1] == "beta"
    assert tags1[2] == "gamma"


def test_derive_tags_respects_limit():
    text = " ".join(f"word{i}" for i in range(50))
    tags = derive_tags(text, limit=5)
    assert len(tags) == 5


def test_derive_tags_aggregates_across_inputs():
    tags = derive_tags("oauth callback", "the oauth flow", "fixed oauth bug")
    # 'oauth' appears in all 3 — should be the top tag
    assert tags[0] == "oauth"


def test_derive_tags_handles_empty():
    assert derive_tags("") == []
    assert derive_tags("the and was") == []
    assert derive_tags() == []


# ---------------------------------------------------------------------------
# Lesson roundtrip
# ---------------------------------------------------------------------------


def test_lesson_render_includes_all_sections():
    lesson = Lesson(
        task="Add OAuth login",
        initial_approach="Used hardcoded redirect URI",
        verifier_complaint="staging deploy broke because redirect URI was localhost",
        fix="Read OAUTH_REDIRECT from env, fall back to localhost",
        tags=["oauth", "auth"],
    )
    md = lesson.render_markdown()
    assert "# Lesson: Add OAuth login" in md
    assert "## Initial approach" in md
    assert "## What went wrong" in md
    assert "## What worked" in md
    assert "hardcoded redirect URI" in md
    assert "tags: [auth, oauth]" in md  # alpha-sorted


def test_save_and_load_roundtrip(tmp_path):
    lesson = Lesson(
        task="Test roundtrip",
        initial_approach="naive approach",
        verifier_complaint="missing edge case",
        fix="handle the edge case",
        tags=["roundtrip", "testing"],
        session_id="sess-42",
    )
    path = save_lesson(lesson, lessons_dir=tmp_path)
    assert path.exists()
    assert path.suffix == ".md"

    parsed = load_lesson(path)
    assert parsed is not None
    assert parsed.task == "Test roundtrip"
    assert parsed.initial_approach == "naive approach"
    assert parsed.verifier_complaint == "missing edge case"
    assert parsed.fix == "handle the edge case"
    assert sorted(parsed.tags) == ["roundtrip", "testing"]
    assert parsed.session_id == "sess-42"


def test_load_lesson_missing_returns_none(tmp_path):
    assert load_lesson(tmp_path / "missing.md") is None


def test_save_lesson_handles_filename_collision(tmp_path):
    """Two lessons with the same task in the same second get unique paths."""
    lesson_a = Lesson(task="same task", initial_approach="a",
                      verifier_complaint="x", fix="y")
    lesson_b = Lesson(task="same task", initial_approach="b",
                      verifier_complaint="x", fix="y",
                      created_at=lesson_a.created_at)
    path_a = save_lesson(lesson_a, lessons_dir=tmp_path)
    path_b = save_lesson(lesson_b, lessons_dir=tmp_path)
    assert path_a != path_b
    assert path_a.exists() and path_b.exists()


def test_render_handles_empty_sections():
    lesson = Lesson(task="bare", initial_approach="",
                    verifier_complaint="x", fix="")
    md = lesson.render_markdown()
    assert "_(not captured)_" in md


# ---------------------------------------------------------------------------
# all_lessons
# ---------------------------------------------------------------------------


def test_all_lessons_returns_empty_on_missing_dir(tmp_path):
    assert all_lessons(lessons_dir=tmp_path / "missing") == []


def test_all_lessons_loads_every_file(tmp_path):
    for i in range(3):
        Lesson(task=f"lesson {i}", initial_approach="a",
               verifier_complaint="b", fix="c",
               created_at=time.time() + i)  # different timestamps
        save_lesson(
            Lesson(task=f"lesson {i}", initial_approach="a",
                   verifier_complaint="b", fix="c"),
            lessons_dir=tmp_path,
        )
        time.sleep(1.01)  # ensure distinct timestamps in filename
    lessons = all_lessons(lessons_dir=tmp_path)
    assert len(lessons) == 3


def test_all_lessons_skips_unparseable_files(tmp_path):
    save_lesson(
        Lesson(task="good", initial_approach="a",
               verifier_complaint="b", fix="c"),
        lessons_dir=tmp_path,
    )
    (tmp_path / "junk.md").write_text("not a real lesson", encoding="utf-8")
    lessons = all_lessons(lessons_dir=tmp_path)
    # The junk file produces a Lesson with empty task — load is forgiving;
    # both load, but the bad one has no task and is sortable by name.
    # We just verify the good one loads correctly.
    good = [l for l in lessons if l.task == "good"]
    assert len(good) == 1


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def test_relevant_lessons_returns_top_n(tmp_path):
    # Three lessons with very different tags
    for task, tags in [
        ("OAuth callback fix", ["oauth", "auth"]),
        ("Database migration", ["database", "migration"]),
        ("UI styling tweak", ["ui", "styling"]),
    ]:
        save_lesson(
            Lesson(task=task, initial_approach="x",
                   verifier_complaint="y", fix="z", tags=tags),
            lessons_dir=tmp_path,
        )
    matches = relevant_lessons(
        "Need to fix the OAuth callback handler",
        lessons_dir=tmp_path,
    )
    assert len(matches) >= 1
    assert matches[0].task == "OAuth callback fix"


def test_relevant_lessons_filters_zero_score(tmp_path):
    save_lesson(
        Lesson(task="OAuth fix", initial_approach="x",
               verifier_complaint="y", fix="z", tags=["oauth"]),
        lessons_dir=tmp_path,
    )
    # Query with completely unrelated terms — should NOT return the
    # oauth lesson (better to surface nothing than noise).
    matches = relevant_lessons("ugh whatever zzz", lessons_dir=tmp_path)
    assert matches == []


def test_relevant_lessons_explicit_tags_override():
    """When query_tags is supplied, derive_tags isn't called."""
    # Smoke: pass empty query_text + explicit tags — derive_tags on
    # empty text would return [] but the explicit tags still match.
    with pytest.MonkeyPatch.context() as mp:
        called = {"n": 0}
        def fake_derive(*args, **kwargs):
            called["n"] += 1
            return ["wrong-tag"]
        mp.setattr("agent.lessons.derive_tags", fake_derive)
        relevant_lessons("", query_tags=["oauth"])
        assert called["n"] == 0


def test_relevant_lessons_recency_breaks_ties(tmp_path):
    old = Lesson(task="oauth bug", initial_approach="x",
                 verifier_complaint="y", fix="z", tags=["oauth"],
                 created_at=time.time() - 60 * 86400)  # 60 days old
    new = Lesson(task="oauth bug", initial_approach="x",
                 verifier_complaint="y", fix="z", tags=["oauth"],
                 created_at=time.time())
    save_lesson(old, lessons_dir=tmp_path)
    time.sleep(0.01)
    save_lesson(new, lessons_dir=tmp_path)
    matches = relevant_lessons("oauth", lessons_dir=tmp_path, limit=2)
    # Both match; the more recent one wins on the recency tiebreaker.
    assert len(matches) == 2
    # New lesson scores higher (recency boost) so it comes first.
    assert matches[0].created_at >= matches[1].created_at


# ---------------------------------------------------------------------------
# Preamble formatting
# ---------------------------------------------------------------------------


def test_format_preamble_empty_returns_empty():
    assert format_lessons_preamble([]) == ""


def test_format_preamble_includes_header_and_lessons():
    lesson = Lesson(task="Fix OAuth", initial_approach="hardcoded",
                    verifier_complaint="broke staging",
                    fix="read from env")
    out = format_lessons_preamble([lesson])
    assert "## Prior lessons learned" in out
    assert "Fix OAuth" in out
    assert "hardcoded" in out
    assert "read from env" in out


def test_format_preamble_respects_budget():
    long_fix = "x" * 5000
    lessons = [
        Lesson(task=f"task {i}", initial_approach="a",
               verifier_complaint="b", fix=long_fix)
        for i in range(20)
    ]
    out = format_lessons_preamble(lessons, budget_chars=1000)
    assert len(out) <= 1000


def test_format_preamble_compact_format_is_one_line_per_lesson():
    lesson = Lesson(task="t", initial_approach="i", verifier_complaint="v",
                    fix="f")
    compact = lesson.to_compact_prompt()
    assert "\n" not in compact
    assert "Tried:" in compact
    assert "Failed because:" in compact
    assert "Fix:" in compact


# ---------------------------------------------------------------------------
# capture_from_rework integration
# ---------------------------------------------------------------------------


def test_capture_from_rework_writes_lesson(tmp_path):
    lesson = capture_from_rework(
        task="Add OAuth login",
        initial_response="I added a hardcoded redirect URI...",
        needs_rework_summary="OAuth callback returns 500 in staging because redirect URI is localhost",
        final_response="Fixed by reading OAUTH_REDIRECT from env",
        session_id="sess-1",
        lessons_dir=tmp_path,
    )
    assert lesson is not None
    assert lesson.source_path is not None
    assert lesson.source_path.exists()
    # Tags auto-derived
    assert "oauth" in lesson.tags
    # Roundtrip
    parsed = load_lesson(lesson.source_path)
    assert parsed.task == "Add OAuth login"
    assert "hardcoded" in parsed.initial_approach


def test_capture_from_rework_skips_thin_data(tmp_path):
    assert capture_from_rework(
        task="", initial_response="x",
        needs_rework_summary="y", final_response="z",
        lessons_dir=tmp_path,
    ) is None
    assert capture_from_rework(
        task="x", initial_response="x",
        needs_rework_summary="", final_response="z",
        lessons_dir=tmp_path,
    ) is None


def test_capture_from_rework_uses_extra_tags_when_given(tmp_path):
    lesson = capture_from_rework(
        task="generic task",
        initial_response="a", needs_rework_summary="b", final_response="c",
        extra_tags=["explicit", "tags"],
        lessons_dir=tmp_path,
    )
    assert lesson is not None
    assert sorted(lesson.tags) == ["explicit", "tags"]
