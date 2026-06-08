"""
Self-Verification — closes the autonomy loop by re-checking "done."

When the model emits its final non-tool response, the loop would
ordinarily exit and the user would see the final answer.  With
verification enabled, a verifier subagent runs against the same plan +
transcript + diff before the answer is released, and reports either
VERIFIED or NEEDS_REWORK with concrete issues.  The harness reinjects
the NEEDS_REWORK report as a follow-up user message so the parent
loop can fix the gaps without operator intervention.

Why a separate subagent rather than inline self-critique?

* Fresh context — the verifier reads only the artifacts (plan, diff,
  test output), not the messy chain-of-thought that produced them.
  Empirically that catches more issues than asking the same model to
  self-grade in the same context.
* Iteration budget — the verifier runs inside the parent's
  IterationBudget so a runaway verifier can't blow the cap.
* Model routing — the verifier is allowed to use a cheaper / faster
  model than the executor (config: ``verification.model``).

This module ships the engine — prompt assembly, structured-response
parsing, rework-message rendering.  The actual subagent dispatch (via
``tools.delegate_tool``) is wired in a separate commit so each step
remains reviewable on its own.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATUS_VERIFIED = "VERIFIED"
STATUS_NEEDS_REWORK = "NEEDS_REWORK"
STATUS_ERROR = "ERROR"

VALID_STATUSES = frozenset({STATUS_VERIFIED, STATUS_NEEDS_REWORK, STATUS_ERROR})

SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"
VALID_SEVERITIES = frozenset({SEVERITY_LOW, SEVERITY_MEDIUM, SEVERITY_HIGH})

#: Maximum number of NEEDS_REWORK cycles before we give up and surface
#: the verifier report alongside the original answer.  Stops the agent
#: from looping forever on a task it genuinely cannot finish.
DEFAULT_MAX_ATTEMPTS = 2


VERIFIER_SYSTEM_PROMPT = """\
# You are the Verification Subagent

A primary agent has just declared a task complete.  Your job is to
verify the work end-to-end before the answer reaches the user.  You
have access to:

* The plan artifact (Steps + Verification section)
* The git diff since the task started
* Any test / lint / build output produced by Stop-hook commands
* The full conversation transcript

Run the verification commands listed in the plan's Verification section
(tests, type checks, lints, builds — whatever the plan says).  Inspect
the diff against the plan's Files-to-Modify and Steps.

Reply with **exactly one** JSON object — no prose before or after, no
code fences, no extra keys.  Two valid shapes:

VERIFIED (work is done correctly):
{"status": "VERIFIED", "summary": "<one-line confirmation>"}

NEEDS_REWORK (issues remain):
{
  "status": "NEEDS_REWORK",
  "summary": "<one-paragraph diagnosis>",
  "issues": [
    {"step": <step number or null>,
     "description": "<what is wrong>",
     "suggested_fix": "<what to change>",
     "severity": "low" | "medium" | "high"},
    ...
  ]
}

Be strict.  Marking work VERIFIED when issues remain wastes the user's
time downstream.  Marking VERIFIED requires evidence (test output, code
reading, command results) — not just absence of obvious failure.
"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class VerificationIssue:
    """One concrete defect surfaced by the verifier."""

    description: str
    suggested_fix: str = ""
    step: Optional[int] = None
    severity: str = SEVERITY_MEDIUM

    def render(self, idx: int) -> str:
        sev = self.severity if self.severity in VALID_SEVERITIES else SEVERITY_MEDIUM
        head = f"{idx}. [{sev}]"
        if self.step is not None:
            head += f" (step {self.step})"
        lines = [f"{head} {self.description.strip()}"]
        if self.suggested_fix.strip():
            lines.append(f"   suggested fix: {self.suggested_fix.strip()}")
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["VerificationIssue"]:
        if not isinstance(raw, dict):
            return None
        desc = str(raw.get("description") or raw.get("issue") or "").strip()
        if not desc:
            return None
        step_raw = raw.get("step")
        try:
            step = int(step_raw) if step_raw is not None else None
        except (TypeError, ValueError):
            step = None
        severity = str(raw.get("severity") or SEVERITY_MEDIUM).strip().lower()
        if severity not in VALID_SEVERITIES:
            severity = SEVERITY_MEDIUM
        return cls(
            description=desc,
            suggested_fix=str(raw.get("suggested_fix") or raw.get("fix") or "").strip(),
            step=step,
            severity=severity,
        )


@dataclass
class VerificationReport:
    """Structured outcome of one verification pass."""

    status: str
    summary: str = ""
    issues: List[VerificationIssue] = field(default_factory=list)
    attempts: int = 1
    raw_response: str = ""
    parse_error: Optional[str] = None

    @property
    def verified(self) -> bool:
        return self.status == STATUS_VERIFIED

    @property
    def needs_rework(self) -> bool:
        return self.status == STATUS_NEEDS_REWORK

    @property
    def is_error(self) -> bool:
        return self.status == STATUS_ERROR

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "issues": [
                {
                    "description": i.description,
                    "suggested_fix": i.suggested_fix,
                    "step": i.step,
                    "severity": i.severity,
                }
                for i in self.issues
            ],
            "attempts": self.attempts,
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """Strip ```json ... ``` and ``` ... ``` envelopes if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence + optional language tag.
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def parse_verification_response(text: str) -> VerificationReport:
    """Parse a verifier response into a structured ``VerificationReport``.

    Robust to common model failure modes:
    * Wrapped in ```json fences
    * Leading / trailing prose around the JSON object
    * Unknown / typo'd status strings (mapped to ERROR)
    * Issues array missing or partial
    * Severity strings outside the valid set (downgraded to medium)
    """
    if not text or not text.strip():
        return VerificationReport(
            status=STATUS_ERROR,
            summary="empty verifier response",
            raw_response=text or "",
            parse_error="empty response",
        )

    stripped = _strip_code_fences(text)
    data: Optional[dict] = None
    parse_error: Optional[str] = None

    try:
        candidate = json.loads(stripped)
        if isinstance(candidate, dict):
            data = candidate
    except ValueError as exc:
        parse_error = str(exc)
        match = _JSON_OBJECT_RE.search(stripped)
        if match is not None:
            try:
                candidate = json.loads(match.group(0))
                if isinstance(candidate, dict):
                    data = candidate
                    parse_error = None
            except ValueError as exc2:
                parse_error = str(exc2)

    if data is None:
        return VerificationReport(
            status=STATUS_ERROR,
            summary="could not parse verifier response as JSON",
            raw_response=text,
            parse_error=parse_error,
        )

    raw_status = str(data.get("status", "")).strip().upper().replace("-", "_")
    if raw_status not in VALID_STATUSES:
        return VerificationReport(
            status=STATUS_ERROR,
            summary=f"unknown verifier status {raw_status!r}",
            raw_response=text,
            parse_error=f"unknown status {raw_status!r}",
        )

    summary = str(data.get("summary") or "").strip()
    issues_raw = data.get("issues") or []
    issues: List[VerificationIssue] = []
    if isinstance(issues_raw, list):
        for entry in issues_raw:
            issue = VerificationIssue.from_dict(entry)
            if issue is not None:
                issues.append(issue)

    return VerificationReport(
        status=raw_status,
        summary=summary,
        issues=issues,
        raw_response=text,
    )


# ---------------------------------------------------------------------------
# Prompt assembly + rework-message rendering
# ---------------------------------------------------------------------------


def build_verifier_user_prompt(
    *,
    plan_text: Optional[str],
    final_response: str,
    git_diff: Optional[str] = None,
    test_output: Optional[str] = None,
    transcript_excerpt: Optional[str] = None,
) -> str:
    """Assemble the user-role prompt the verifier subagent sees.

    All sections except ``final_response`` are optional — the verifier
    is told explicitly when a section is missing so it can decide
    whether the absence itself is grounds for NEEDS_REWORK.
    """
    parts: List[str] = []
    parts.append("Please verify the following work.")
    parts.append("")
    parts.append("## Plan artifact")
    parts.append("")
    parts.append(plan_text.strip() if plan_text and plan_text.strip()
                 else "_(no plan artifact found for this session)_")
    parts.append("")
    parts.append("## Primary agent's final response")
    parts.append("")
    parts.append(final_response.strip() or "_(empty final response)_")
    parts.append("")
    parts.append("## Git diff since task start")
    parts.append("")
    if git_diff and git_diff.strip():
        parts.append("```diff")
        parts.append(git_diff.strip())
        parts.append("```")
    else:
        parts.append("_(no diff available — either no files changed or git not available)_")
    parts.append("")
    parts.append("## Test / lint / build output")
    parts.append("")
    if test_output and test_output.strip():
        parts.append("```")
        parts.append(test_output.strip())
        parts.append("```")
    else:
        parts.append("_(no test output captured — the Verification section "
                     "of the plan should name commands to run if relevant)_")
    parts.append("")
    if transcript_excerpt and transcript_excerpt.strip():
        parts.append("## Recent conversation excerpt")
        parts.append("")
        parts.append(transcript_excerpt.strip())
        parts.append("")
    parts.append("Reply with a single JSON object matching the schema in "
                 "your system prompt.  No prose, no fences.")
    return "\n".join(parts)


def render_rework_message(report: VerificationReport, plan_path: Optional[Path] = None) -> str:
    """Render a follow-up user-role message that re-enters the parent loop.

    Designed to be read by the same model that just finished — so it
    leads with the headline ("verification failed") and lists concrete,
    actionable fixes rather than dumping the raw JSON.
    """
    if not report.needs_rework:
        return ""
    lines: List[str] = []
    lines.append("Verification failed. Address the issues below and try again, "
                 "then emit a new final response.")
    lines.append("")
    if report.summary:
        lines.append(f"**Summary:** {report.summary.strip()}")
        lines.append("")
    if plan_path is not None:
        lines.append(f"Plan reference: `{plan_path}`")
        lines.append("")
    if report.issues:
        lines.append("**Issues:**")
        lines.append("")
        for i, issue in enumerate(report.issues, 1):
            lines.append(issue.render(i))
        lines.append("")
    lines.append("Once you have fixed the issues, run the plan's Verification "
                 "commands yourself and confirm they pass before declaring the "
                 "task complete.")
    return "\n".join(lines)


def render_summary_for_user(report: VerificationReport) -> str:
    """One-line user-visible summary suitable for status lines / logs."""
    if report.verified:
        return f"verified: {report.summary or 'ok'}"
    if report.needs_rework:
        n = len(report.issues)
        return f"needs rework: {report.summary or f'{n} issue(s)'} ({n} issue(s))"
    return f"verification error: {report.summary or report.parse_error or 'unknown'}"


# ---------------------------------------------------------------------------
# Differential verification — multi-model consensus
# ---------------------------------------------------------------------------


@dataclass
class ConsensusReport:
    """The outcome of running N verifiers in parallel and voting.

    The aggregated ``status`` is decided by quorum: ``quorum_required``
    out of ``len(reports)`` must agree.  When no status reaches quorum,
    the result is ERROR (caller decides — usually escalate to a human).
    """

    status: str
    quorum_required: int
    reports: List["VerificationReport"] = field(default_factory=list)
    summary: str = ""
    issues: List["VerificationIssue"] = field(default_factory=list)
    voted_status_counts: Dict[str, int] = field(default_factory=dict)
    models_used: List[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.status == STATUS_VERIFIED

    @property
    def needs_rework(self) -> bool:
        return self.status == STATUS_NEEDS_REWORK

    @property
    def is_error(self) -> bool:
        return self.status == STATUS_ERROR

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "quorum_required": self.quorum_required,
            "voted_status_counts": dict(self.voted_status_counts),
            "models_used": list(self.models_used),
            "summary": self.summary,
            "issues": [
                {
                    "description": i.description,
                    "suggested_fix": i.suggested_fix,
                    "step": i.step,
                    "severity": i.severity,
                }
                for i in self.issues
            ],
            "per_verifier": [r.to_dict() for r in self.reports],
        }


def aggregate_consensus(
    reports: List[VerificationReport],
    *,
    quorum_required: int = 2,
    models_used: Optional[List[str]] = None,
) -> ConsensusReport:
    """Vote across N verifier reports.  Quorum-based consensus.

    Rules:
    * If ``quorum_required`` reports share the same status, that
      status wins.  Cosmic tiebreaker order is NEEDS_REWORK >
      VERIFIED > ERROR (safety first — when in doubt, rework).
    * Issues are unioned across all NEEDS_REWORK voters and
      deduplicated by (description prefix).  Severity is the max
      severity across duplicates.
    * Summary aggregates the most-cited concerns.

    Raises ValueError if ``reports`` is empty.
    """
    if not reports:
        raise ValueError("aggregate_consensus requires at least one report")
    if quorum_required < 1:
        quorum_required = 1
    if quorum_required > len(reports):
        # Can't reach quorum that's larger than the population — clamp.
        quorum_required = len(reports)

    # Count votes by status.
    counts: Dict[str, int] = {}
    for r in reports:
        counts[r.status] = counts.get(r.status, 0) + 1

    # Find a status that meets quorum.  Apply the safety-first
    # tiebreaker order: NEEDS_REWORK > VERIFIED > ERROR.
    consensus_status: Optional[str] = None
    for candidate in (STATUS_NEEDS_REWORK, STATUS_VERIFIED, STATUS_ERROR):
        if counts.get(candidate, 0) >= quorum_required:
            consensus_status = candidate
            break

    if consensus_status is None:
        # No quorum — surface as ERROR so the caller can decide.
        consensus_status = STATUS_ERROR

    # Aggregate summary + issues from voters whose status matches.
    matching = [r for r in reports if r.status == consensus_status]
    summaries = [r.summary for r in matching if r.summary]
    summary = " | ".join(summaries[:3])

    # Union of issues, deduped by description prefix (40 chars).
    seen_prefixes: Set[str] = set()
    merged_issues: List[VerificationIssue] = []
    for r in matching:
        for issue in r.issues:
            key = issue.description[:40].lower()
            if key in seen_prefixes:
                continue
            seen_prefixes.add(key)
            merged_issues.append(issue)

    return ConsensusReport(
        status=consensus_status,
        quorum_required=quorum_required,
        reports=list(reports),
        summary=summary,
        issues=merged_issues,
        voted_status_counts=counts,
        models_used=list(models_used or []),
    )


__all__ = [
    "ConsensusReport",
    "DEFAULT_MAX_ATTEMPTS",
    "STATUS_ERROR",
    "STATUS_NEEDS_REWORK",
    "STATUS_VERIFIED",
    "VALID_STATUSES",
    "VERIFIER_SYSTEM_PROMPT",
    "VerificationIssue",
    "VerificationReport",
    "aggregate_consensus",
    "build_verifier_user_prompt",
    "parse_verification_response",
    "render_rework_message",
    "render_summary_for_user",
]
