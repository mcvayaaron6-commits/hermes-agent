"""Tests for the self-verification engine."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.verification import (
    DEFAULT_MAX_ATTEMPTS,
    STATUS_ERROR,
    STATUS_NEEDS_REWORK,
    STATUS_VERIFIED,
    VerificationIssue,
    VerificationReport,
    build_verifier_user_prompt,
    parse_verification_response,
    render_rework_message,
    render_summary_for_user,
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_verified_minimal():
    report = parse_verification_response(
        '{"status": "VERIFIED", "summary": "tests pass"}'
    )
    assert report.verified
    assert report.summary == "tests pass"
    assert report.issues == []
    assert report.is_error is False


def test_parse_needs_rework_with_issues():
    raw = json.dumps({
        "status": "NEEDS_REWORK",
        "summary": "tests fail",
        "issues": [
            {"step": 2, "description": "missing import",
             "suggested_fix": "add `import os`", "severity": "high"},
            {"description": "no test for new branch", "severity": "medium"},
        ],
    })
    report = parse_verification_response(raw)
    assert report.needs_rework
    assert len(report.issues) == 2
    assert report.issues[0].step == 2
    assert report.issues[0].severity == "high"
    assert report.issues[1].step is None


def test_parse_strips_code_fences():
    raw = '```json\n{"status": "VERIFIED", "summary": "ok"}\n```'
    report = parse_verification_response(raw)
    assert report.verified
    assert report.summary == "ok"


def test_parse_strips_unlabeled_code_fence():
    raw = '```\n{"status": "VERIFIED", "summary": "ok"}\n```'
    report = parse_verification_response(raw)
    assert report.verified


def test_parse_extracts_json_from_surrounding_prose():
    raw = (
        "Sure, here's my verdict:\n\n"
        '{"status": "NEEDS_REWORK", "summary": "incomplete", "issues": []}\n\n'
        "Hope that helps!"
    )
    report = parse_verification_response(raw)
    assert report.needs_rework
    assert report.summary == "incomplete"


def test_parse_empty_response_is_error():
    report = parse_verification_response("")
    assert report.is_error
    assert report.parse_error


def test_parse_invalid_json_is_error():
    report = parse_verification_response("not json at all")
    assert report.is_error
    assert "could not parse" in report.summary.lower()


def test_parse_unknown_status_is_error():
    report = parse_verification_response(
        '{"status": "MAYBE_OK", "summary": "unsure"}'
    )
    assert report.is_error
    assert "MAYBE_OK" in (report.parse_error or "")


def test_parse_status_case_insensitive_and_dash_to_underscore():
    report = parse_verification_response(
        '{"status": "needs-rework", "summary": "off-spec"}'
    )
    assert report.needs_rework


def test_parse_issues_with_invalid_severity_default_medium():
    raw = json.dumps({
        "status": "NEEDS_REWORK",
        "summary": "x",
        "issues": [{"description": "thing", "severity": "BLOCKER"}],
    })
    report = parse_verification_response(raw)
    assert report.issues[0].severity == "medium"


def test_parse_issues_with_missing_description_dropped():
    raw = json.dumps({
        "status": "NEEDS_REWORK",
        "summary": "x",
        "issues": [
            {"suggested_fix": "do x"},  # no description
            {"description": "real issue"},
        ],
    })
    report = parse_verification_response(raw)
    assert len(report.issues) == 1
    assert report.issues[0].description == "real issue"


def test_parse_issues_non_list_ignored():
    raw = json.dumps({"status": "NEEDS_REWORK", "summary": "x", "issues": "oops"})
    report = parse_verification_response(raw)
    assert report.needs_rework
    assert report.issues == []


def test_parse_step_coercion():
    raw = json.dumps({
        "status": "NEEDS_REWORK",
        "summary": "x",
        "issues": [{"description": "y", "step": "3"}],
    })
    report = parse_verification_response(raw)
    assert report.issues[0].step == 3

    raw = json.dumps({
        "status": "NEEDS_REWORK",
        "summary": "x",
        "issues": [{"description": "y", "step": "not a number"}],
    })
    report = parse_verification_response(raw)
    assert report.issues[0].step is None


def test_parse_accepts_alternate_field_names():
    raw = json.dumps({
        "status": "NEEDS_REWORK",
        "summary": "x",
        "issues": [{"issue": "alt-key description", "fix": "alt-key fix"}],
    })
    report = parse_verification_response(raw)
    assert len(report.issues) == 1
    assert report.issues[0].description == "alt-key description"
    assert report.issues[0].suggested_fix == "alt-key fix"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_issue_render_basic():
    issue = VerificationIssue(description="bug here", step=4, severity="high",
                              suggested_fix="change X")
    rendered = issue.render(1)
    assert "1." in rendered
    assert "[high]" in rendered
    assert "step 4" in rendered
    assert "bug here" in rendered
    assert "change X" in rendered


def test_issue_render_without_step_or_fix():
    issue = VerificationIssue(description="bug here", severity="low")
    rendered = issue.render(1)
    assert "step" not in rendered
    assert "suggested fix" not in rendered.lower()
    assert "[low]" in rendered


def test_render_rework_message_combines_summary_and_issues():
    report = VerificationReport(
        status=STATUS_NEEDS_REWORK,
        summary="2 of 3 steps incomplete",
        issues=[
            VerificationIssue(description="step 1 not done", step=1, severity="high"),
            VerificationIssue(description="missing test", step=3, severity="medium"),
        ],
    )
    msg = render_rework_message(report, plan_path=Path("/tmp/x.md"))
    assert "Verification failed" in msg
    assert "2 of 3 steps incomplete" in msg
    assert "step 1 not done" in msg
    assert "missing test" in msg
    assert "/tmp/x.md" in msg


def test_render_rework_message_returns_empty_for_verified():
    report = VerificationReport(status=STATUS_VERIFIED, summary="ok")
    assert render_rework_message(report) == ""


def test_render_summary_for_user_variants():
    verified = VerificationReport(status=STATUS_VERIFIED, summary="tests pass")
    assert render_summary_for_user(verified) == "verified: tests pass"

    rework = VerificationReport(
        status=STATUS_NEEDS_REWORK,
        summary="bad",
        issues=[VerificationIssue(description="x"), VerificationIssue(description="y")],
    )
    rendered = render_summary_for_user(rework)
    assert "needs rework" in rendered
    assert "2 issue" in rendered

    err = VerificationReport(status=STATUS_ERROR, summary="parse failed")
    assert "error" in render_summary_for_user(err)


# ---------------------------------------------------------------------------
# Verifier prompt assembly
# ---------------------------------------------------------------------------


def test_build_prompt_includes_all_sections_when_provided():
    prompt = build_verifier_user_prompt(
        plan_text="# Plan\n- step",
        final_response="done",
        git_diff="diff --git a/x b/y",
        test_output="3 passed",
        transcript_excerpt="user: do it\nassistant: ok",
    )
    assert "## Plan artifact" in prompt
    assert "step" in prompt
    assert "## Primary agent's final response" in prompt
    assert "done" in prompt
    assert "## Git diff since task start" in prompt
    assert "diff --git" in prompt
    assert "3 passed" in prompt
    assert "## Recent conversation excerpt" in prompt


def test_build_prompt_marks_missing_sections_explicitly():
    prompt = build_verifier_user_prompt(
        plan_text=None,
        final_response="done",
    )
    assert "no plan artifact" in prompt.lower()
    assert "no diff available" in prompt.lower()
    assert "no test output" in prompt.lower()


def test_build_prompt_handles_empty_strings_same_as_none():
    prompt = build_verifier_user_prompt(
        plan_text="   ",
        final_response="done",
        git_diff="",
        test_output="",
    )
    assert "no plan artifact" in prompt.lower()
    assert "no diff available" in prompt.lower()
    assert "no test output" in prompt.lower()


def test_build_prompt_demands_json_only_response():
    prompt = build_verifier_user_prompt(plan_text=None, final_response="x")
    assert "single JSON object" in prompt
    assert "No prose" in prompt or "no prose" in prompt.lower()


# ---------------------------------------------------------------------------
# Report serialisation
# ---------------------------------------------------------------------------


def test_to_dict_roundtrip_shape():
    report = VerificationReport(
        status=STATUS_NEEDS_REWORK,
        summary="x",
        issues=[VerificationIssue(description="y", step=1, severity="high")],
        attempts=2,
    )
    d = report.to_dict()
    assert d["status"] == STATUS_NEEDS_REWORK
    assert d["summary"] == "x"
    assert d["attempts"] == 2
    assert d["issues"][0]["description"] == "y"
    assert d["issues"][0]["step"] == 1
    assert d["issues"][0]["severity"] == "high"
    # Must be JSON-serialisable.
    json.dumps(d)


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


def test_default_max_attempts_positive():
    assert DEFAULT_MAX_ATTEMPTS >= 1


def test_status_constants_distinct():
    assert STATUS_VERIFIED != STATUS_NEEDS_REWORK != STATUS_ERROR
