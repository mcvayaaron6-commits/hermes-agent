"""
Plan Mode — separate "think and investigate" from "act and mutate."

A first-class Plan/Act separation, as shipped by Cline and Claude Code.
While Plan Mode is on, the agent has access to a read-only toolset
(file reads, search, web fetch, session-memory lookups, skill browsing,
todo planning, clarification questions) and is instructed to produce a
structured plan artifact at ``.hermes/plans/<slug>-<ts>.md``.  Any tool
not on the allowlist is refused with a one-line message that tells the
model "you're in plan mode — finish the plan artifact and stop."

Switching out of Plan Mode ("Act Mode") releases the full toolset and
seeds the in-memory todo list from the plan's Steps so the agent has
checklist-grained progress visible to the user as it executes.

This module is the engine.  Integration into ``model_tools.py``,
``agent/prompt_builder.py``, and the CLI slash-command handler lives
elsewhere — keeping the engine standalone makes it unit-testable in
isolation and lets the integration land in small, reviewable commits.

Allowlist philosophy
--------------------

We use an allowlist rather than a denylist on purpose.  New tools get
added to Hermes regularly; a denylist would silently grow more permissive
over time as plan mode would let through any tool not yet on the deny
list.  An allowlist means a new tool is "block by default" in plan mode
until someone explicitly classifies it as read-only.

The allowlist below covers the read-only members of the core toolsets:
file reads, search, web, session memory, skills (browse/view), todos,
clarify, and delegation (only when the child is also in plan mode).
Operators who want a tighter or wider list can override via the
``plan_mode.allow_tools`` / ``plan_mode.deny_tools`` config keys.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set


# ---------------------------------------------------------------------------
# Default allow-list
# ---------------------------------------------------------------------------

#: Tools whose only effect is to read state or ask the user a question.
#: Safe to invoke while in Plan Mode.
DEFAULT_PLAN_MODE_ALLOWLIST: frozenset[str] = frozenset({
    # File reads / search
    "read_file",
    "list_directory",
    "search_files",
    # Web read-only
    "web_search",
    "web_fetch",
    # Session memory & history
    "session_search",
    "session_insights",
    # Skills (read paths only — install/delete are mutating)
    "skill_search",
    "skill_browse",
    "skill_view",
    # Memory READ — write must use a separate tool name; we filter on
    # action below for tools that have both modes.
    "memory_read",
    # Plan-time planning helpers
    "todo",
    "clarify",
    # NOTE: delegate_task is deliberately NOT on the allowlist.  The
    # child AIAgent that delegate_task spawns is constructed with a
    # fresh PlanModeState(enabled=False), so allowing delegation
    # during /plan would be a read-only escape hatch — the subagent
    # would have full mutating-tool access while the parent appears
    # to be in Plan Mode.  If you need parallel investigation during
    # planning, do it inline (the parent has plenty of read-only
    # tools); cross-agent parallel planning is a future feature that
    # needs plan-mode propagation in delegate_tool.
})


#: Mode-aware tools that have both read and write actions.  When in
#: plan mode, only the listed actions are permitted.
DEFAULT_PLAN_MODE_ACTION_FILTERS: dict[str, frozenset[str]] = {
    "memory": frozenset({"read", "list", "get"}),
    "kanban": frozenset({"show", "list"}),
}


#: A refusal payload returned to the model when a tool is blocked by
#: plan mode.  Phrased so the model knows what to do next.
PLAN_MODE_REFUSAL_TEMPLATE = (
    "Refused: you are in Plan Mode and {tool!r} is not on the read-only "
    "allowlist. Finish writing your plan to {plan_path} using read-only "
    "tools (read_file, search_files, web_search, session_search, "
    "todo, clarify, etc.) and stop. The user will switch to Act Mode "
    "after reviewing your plan."
)


# ---------------------------------------------------------------------------
# Plan artifact
# ---------------------------------------------------------------------------


@dataclass
class PlanStep:
    """One numbered step in the Steps section of a plan artifact."""

    description: str
    done: bool = False

    def render(self, idx: int) -> str:
        mark = "x" if self.done else " "
        return f"{idx}. [{mark}] {self.description}"


@dataclass
class PlanArtifact:
    """Structured plan that lives on disk at ``.hermes/plans/<slug>-<ts>.md``.

    Section order matches the slash-command UX and is fixed: a human
    skimming a stale plan should always find Context first.
    """

    task: str
    context: str = ""
    investigation: str = ""
    approach: str = ""
    files_to_modify: List[str] = field(default_factory=list)
    steps: List[PlanStep] = field(default_factory=list)
    verification: str = ""
    created_at: float = field(default_factory=time.time)

    def render_markdown(self) -> str:
        lines: List[str] = []
        lines.append(f"# Plan: {self.task.strip() or 'Untitled task'}")
        lines.append("")
        lines.append(f"_Created: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.created_at))}_")
        lines.append("")
        lines.append("## Context")
        lines.append("")
        lines.append(self.context.strip() or "_(none)_")
        lines.append("")
        lines.append("## Investigation Notes")
        lines.append("")
        lines.append(self.investigation.strip() or "_(none)_")
        lines.append("")
        lines.append("## Approach")
        lines.append("")
        lines.append(self.approach.strip() or "_(none)_")
        lines.append("")
        lines.append("## Files to Modify")
        lines.append("")
        if self.files_to_modify:
            for path in self.files_to_modify:
                lines.append(f"- `{path}`")
        else:
            lines.append("_(none — read-only changes only)_")
        lines.append("")
        lines.append("## Steps")
        lines.append("")
        if self.steps:
            for i, step in enumerate(self.steps, 1):
                lines.append(step.render(i))
        else:
            lines.append("_(no steps yet)_")
        lines.append("")
        lines.append("## Verification")
        lines.append("")
        lines.append(self.verification.strip() or "_(none)_")
        lines.append("")
        return "\n".join(lines)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_len: int = 48) -> str:
    """Lower-kebab-case slug suitable for a filename.

    Empty or all-punctuation input falls back to ``"plan"`` so the
    returned slug always passes ``Path`` validation.
    """
    cleaned = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    if not cleaned:
        return "plan"
    return cleaned[:max_len].rstrip("-") or "plan"


def default_plan_path(plans_dir: Path, task: str, *, now: Optional[float] = None) -> Path:
    """Return ``<plans_dir>/<slug>-<YYYYmmdd-HHMMSS>.md`` for a given task."""
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    return plans_dir / f"{slugify(task)}-{ts}.md"


def parse_steps_from_markdown(text: str) -> List[PlanStep]:
    """Best-effort parse of the Steps section into ``PlanStep`` objects.

    Recognises numbered checklist items in the form ``N. [ ] description``
    or ``N. [x] description``.  Skips blank lines and section headers.
    Forgiving: lines that don't match the pattern are ignored rather than
    raising, since users will edit plan artifacts by hand.
    """
    steps: List[PlanStep] = []
    in_steps = False
    pattern = re.compile(r"^\s*\d+\.\s*\[(?P<mark>[ xX])\]\s*(?P<desc>.+?)\s*$")
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.lower().startswith("## steps"):
            in_steps = True
            continue
        if in_steps and line.lower().startswith("## "):
            break
        if not in_steps:
            continue
        m = pattern.match(line)
        if not m:
            continue
        steps.append(
            PlanStep(
                description=m.group("desc").strip(),
                done=m.group("mark").lower() == "x",
            )
        )
    return steps


# ---------------------------------------------------------------------------
# Mode state + tool gate
# ---------------------------------------------------------------------------


@dataclass
class PlanModeState:
    """Tracks whether plan mode is active and which tools to allow.

    The state object is created once per session and toggled via
    ``enter()`` / ``exit()``.  ``is_tool_allowed`` is the single source
    of truth for whether a given tool call should proceed.
    """

    enabled: bool = False
    plan_path: Optional[Path] = None
    task: str = ""
    #: Transient flag — set by ``exit()`` and consumed by the harness
    #: on the first execution turn after ``/exit-plan``.  When True,
    #: ``verification.auto_when_plan`` continues to fire so the plan's
    #: execution is verified, BUT only for those execution turns —
    #: cleared on first VERIFIED so subsequent unrelated chat doesn't
    #: keep paying for verifier LLM calls.
    executing_plan: bool = False
    allowlist: Set[str] = field(default_factory=lambda: set(DEFAULT_PLAN_MODE_ALLOWLIST))
    action_filters: dict[str, Set[str]] = field(
        default_factory=lambda: {k: set(v) for k, v in DEFAULT_PLAN_MODE_ACTION_FILTERS.items()}
    )
    extra_allow: Set[str] = field(default_factory=set)
    extra_deny: Set[str] = field(default_factory=set)

    def enter(self, *, task: str, plan_path: Path) -> None:
        self.enabled = True
        self.executing_plan = False
        self.task = task
        self.plan_path = plan_path

    def exit(self) -> None:
        self.enabled = False
        # Mark the just-exited session as in execution mode so the
        # harness keeps auto_when_plan verification active until the
        # plan executes to VERIFIED — at which point the harness
        # clears this flag.  Without the flag, verification would
        # fire for every unrelated chat turn until /new (the prior
        # bug); with it, the verifier fires only during execution.
        self.executing_plan = True
        # We intentionally keep plan_path / task around so callers that
        # need to read the artifact after exit (e.g. to seed todos) can.

    def reset(self) -> None:
        self.enabled = False
        self.executing_plan = False
        self.task = ""
        self.plan_path = None
        self.extra_allow.clear()
        self.extra_deny.clear()

    # ------------------------------------------------------------------
    # Tool gating
    # ------------------------------------------------------------------

    def is_tool_allowed(self, tool_name: str, *, args: Optional[dict] = None) -> bool:
        """Return True when ``tool_name`` may run in the current state."""
        if not self.enabled:
            return True
        if tool_name in self.extra_deny:
            return False
        if tool_name in self.extra_allow:
            return True
        if tool_name in self.allowlist:
            return True
        filter_actions = self.action_filters.get(tool_name)
        if filter_actions and isinstance(args, dict):
            action = (args.get("action") or args.get("op") or "").strip().lower()
            if action and action in {a.lower() for a in filter_actions}:
                return True
        return False

    def refusal_for(self, tool_name: str) -> str:
        """Return the user-facing refusal string for a blocked tool.

        Uses plain ``str.replace`` rather than ``str.format`` so a path
        like ``/tmp/proj-{abc}/plan.md`` doesn't trip the format engine
        into re-evaluating ``{abc}`` as a field reference (KeyError).
        """
        plan_path = str(self.plan_path) if self.plan_path else ".hermes/plans/<plan>.md"
        return (PLAN_MODE_REFUSAL_TEMPLATE
                .replace("{tool!r}", repr(tool_name))
                .replace("{tool}", str(tool_name))
                .replace("{plan_path}", plan_path))

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def configure(
        self,
        *,
        allow_tools: Optional[Iterable[str]] = None,
        deny_tools: Optional[Iterable[str]] = None,
    ) -> None:
        if allow_tools:
            self.extra_allow.update(allow_tools)
        if deny_tools:
            self.extra_deny.update(deny_tools)

    def snapshot(self) -> dict:
        """Return a JSON-serialisable dict describing the current state."""
        return {
            "enabled": self.enabled,
            "task": self.task,
            "plan_path": str(self.plan_path) if self.plan_path else None,
            "allowlist": sorted(self.allowlist),
            "extra_allow": sorted(self.extra_allow),
            "extra_deny": sorted(self.extra_deny),
        }


# ---------------------------------------------------------------------------
# System-prompt addendum
# ---------------------------------------------------------------------------


PLAN_MODE_SYSTEM_PROMPT = """\
# Plan Mode is active

You are in **Plan Mode**.  You may only call read-only tools (read_file,
list_directory, search_files, web_search, web_fetch, session_search,
session_insights, skill_search, skill_browse, skill_view, memory READ,
todo, clarify).  Any other tool — including ``delegate_task``, because
it spawns an unrestricted subagent — will be refused.

Your job in Plan Mode is to produce a structured plan that, once approved,
the agent will execute end-to-end in Act Mode.  Use `write_file` is NOT
allowed in Plan Mode — instead, when your plan is ready, write its content
to the plan artifact via `todo` (or just present it as your final message;
the harness writes it to disk).

The plan must include:

- **Context** — why the task exists, what prompted it, the intended outcome
- **Investigation Notes** — what you learned with read-only tools, citing
  file paths and line numbers
- **Approach** — your chosen strategy and a one-line rejection of
  significant alternatives
- **Files to Modify** — explicit paths the agent will write to
- **Steps** — a numbered checklist; each step is one verifiable change
- **Verification** — how to confirm the task is done (tests, commands,
  manual checks)

When the plan is complete and you have nothing more to investigate,
stop.  Do NOT call mutating tools — they will be refused.  The user will
review your plan and switch to Act Mode with `/exit-plan`.
"""


# ---------------------------------------------------------------------------
# Disk persistence helpers
# ---------------------------------------------------------------------------


def write_plan_artifact(plan: PlanArtifact, path: Path) -> Path:
    """Write a plan to disk, creating parent dirs.  Returns the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.render_markdown(), encoding="utf-8")
    return path


def read_plan_artifact(path: Path) -> Optional[PlanArtifact]:
    """Read a plan artifact from disk.  Returns None if the file is missing.

    This is a best-effort parser — it recovers the Steps list (so todos
    can be seeded) and stuffs the rest of each section into the matching
    field.  Round-trip fidelity isn't a goal; users may hand-edit plans
    and any reasonable format should survive.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    plan = PlanArtifact(task="")
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("# Plan:"):
            plan.task = line[len("# Plan:"):].strip()
            continue
        if line.startswith("## "):
            current = line[3:].strip().lower()
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    plan.context = _join_section(sections.get("context"))
    plan.investigation = _join_section(sections.get("investigation notes"))
    plan.approach = _join_section(sections.get("approach"))
    files_text = _join_section(sections.get("files to modify"))
    plan.files_to_modify = [
        m.group(1) for m in re.finditer(r"^\s*-\s*`([^`]+)`", files_text, flags=re.M)
    ]
    plan.steps = parse_steps_from_markdown(text)
    plan.verification = _join_section(sections.get("verification"))
    return plan


def _join_section(lines: Optional[Sequence[str]]) -> str:
    if not lines:
        return ""
    out = "\n".join(lines).strip()
    if out.startswith("_(") and out.endswith(")_"):
        return ""
    return out


__all__ = [
    "DEFAULT_PLAN_MODE_ACTION_FILTERS",
    "DEFAULT_PLAN_MODE_ALLOWLIST",
    "PLAN_MODE_REFUSAL_TEMPLATE",
    "PLAN_MODE_SYSTEM_PROMPT",
    "PlanArtifact",
    "PlanModeState",
    "PlanStep",
    "default_plan_path",
    "parse_steps_from_markdown",
    "read_plan_artifact",
    "slugify",
    "write_plan_artifact",
]
