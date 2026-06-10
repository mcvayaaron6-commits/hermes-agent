"""
Skill Promotion — the moat-10x compounding-intelligence engine.

The lessons-learned layer (``agent/lessons.py``) captures single
rework→success cycles.  This module is the layer above: when three or
more lessons cluster around the same recurring pattern, automatically
**promote** the pattern into reusable procedural memory — a skill or
subagent profile that the agent uses directly next time.

Why this is the moat
--------------------

Other agents have hooks, plan mode, verification, subagents.  Nobody
has an agent that **builds its own future workforce from its own
mistakes**.  Three commits of compounding plumbing converge here:

  1. ``agent/lessons.py`` captures rework deltas → markdown corpus
  2. THIS MODULE clusters lessons by tag overlap, proposes promotions
  3. ``agent/skill_manager_tool`` + ``agents_bundled/`` consume the
     promotions as durable procedural memory the agent uses next time

The result: the agent gets measurably better at the recurring task
patterns IT encounters, without operator intervention.  Other agents
ship features; this one ships **organisational learning**.

Promotion thresholds
--------------------

* **3 lessons sharing ≥ 2 tags** → propose a **skill** capturing the
  "always do X to avoid Y" pattern.
* **5 successful uses of a skill** (tracked via ``skill_usage.py``) →
  propose a **subagent profile** specialising in that domain.

The thresholds are conservative on purpose — promoting too eagerly
fills the registry with noise.  Both are tunable in config:
``skill_promotion.lesson_threshold`` / ``skill_promotion.usage_threshold``.

Output: ``PromotionCandidate`` objects, NOT side-effecting installs.
The agent loop surfaces candidates via ``/promotions`` so the user
approves (or auto-approves via config).  Trust by default would be
a footgun.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


_DEFAULT_LESSON_THRESHOLD = 3
_DEFAULT_USAGE_THRESHOLD = 5
_DEFAULT_MIN_SHARED_TAGS = 2


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _promotion_config() -> Dict[str, object]:
    try:
        from hermes_cli.config import load_config
        return dict(load_config().get("skill_promotion") or {})
    except Exception:
        return {}


def _int_config(key: str, default: int) -> int:
    cfg = _promotion_config()
    try:
        return max(1, int(cfg.get(key, default)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PromotionCandidate:
    """One pattern the promotion engine wants to elevate into procedural memory.

    The candidate is NOT auto-installed — it's surfaced for user review
    via ``/promotions`` (or auto-approved via config flag).  Each
    candidate carries enough info to write the skill/profile body.
    """

    kind: str  # "skill" | "subagent_profile"
    name: str  # proposed identifier (slug-safe)
    title: str  # human-readable
    description: str
    shared_tags: List[str]
    supporting_lesson_paths: List[str]
    body_markdown: str
    score: float  # higher = stronger signal
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Lesson clustering
# ---------------------------------------------------------------------------


def _slugify(text: str, *, max_len: int = 32) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (cleaned[:max_len] or "promoted").rstrip("-")


def _cluster_lessons_by_tag_overlap(
    lessons: List,  # List[agent.lessons.Lesson]
    *,
    min_shared_tags: int,
) -> List[List]:
    """Group lessons into clusters where every member shares ≥
    ``min_shared_tags`` tags with at least one other cluster member.

    Greedy / union-find — not optimal globally, but stable for the
    promotion use case (we only care about clear, easy-to-name
    clusters).  Lessons with zero overlap to anything end up in
    singleton clusters that the caller filters out.
    """
    n = len(lessons)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        tags_i = {t.lower() for t in lessons[i].tags}
        for j in range(i + 1, n):
            tags_j = {t.lower() for t in lessons[j].tags}
            shared = tags_i & tags_j
            if len(shared) >= min_shared_tags:
                union(i, j)

    groups: Dict[int, List] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(lessons[i])
    return list(groups.values())


def _cluster_shared_tags(lessons: List) -> List[str]:
    """Return the tags shared by EVERY lesson in the cluster, ranked
    by total frequency across the cluster."""
    if not lessons:
        return []
    common: Set[str] = {t.lower() for t in lessons[0].tags}
    for lesson in lessons[1:]:
        common &= {t.lower() for t in lesson.tags}
    # Sort by aggregate frequency for stable, meaningful ordering.
    counts = Counter()
    for lesson in lessons:
        for tag in lesson.tags:
            tag = tag.lower()
            if tag in common:
                counts[tag] += 1
    return [tag for tag, _ in counts.most_common()]


# ---------------------------------------------------------------------------
# Skill body generation
# ---------------------------------------------------------------------------


_SKILL_TEMPLATE = """\
---
name: {name}
description: {description}
tags: [{tags_csv}]
auto_promoted: true
auto_promoted_at: {ts}
---

# {title}

This skill was auto-promoted from {n_lessons} lesson(s) that shared
the tags `{tags_csv}`.  When the agent encounters a task matching
this pattern, it should follow the **What to do** section directly
rather than re-discovering the fix.

## What to do

{guidance}

## Supporting lessons

{supporting_bullets}

## When this applies

Apply when the task mentions any of: `{tags_csv}`.
"""


_PROFILE_TEMPLATE = """\
---
name: {name}
description: {description}
toolsets: [file, search]
auto_promoted: true
auto_promoted_at: {ts}
---

# {title}

This subagent profile was auto-promoted from {n_uses} successful
applications of the `{source_skill}` skill.  When the parent agent
encounters a task matching this domain, prefer delegating to this
specialised subagent rather than handling it inline.

## Specialty

{description}

## Approach

{guidance}

## Constraints

You inherit toolset `[file, search]`.  If you need additional
toolsets (terminal, web), the parent must grant them explicitly.
You cannot delegate further — leaf agent only.
"""


def _synthesise_guidance(lessons: List) -> str:
    """Turn the cluster's collective wisdom into a numbered list of
    fixes — what the model should DO when it hits this pattern.

    Heuristic: one bullet per lesson, deduplicated by fix-text prefix.
    The model can later improve this body via its own skill_manage
    tool; we just seed it.
    """
    seen_prefixes: Set[str] = set()
    bullets: List[str] = []
    for i, lesson in enumerate(lessons, 1):
        fix = lesson.fix.strip()
        if not fix:
            continue
        prefix = fix[:40].lower()
        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        bullets.append(f"{i}. {fix}")
    return "\n".join(bullets) if bullets else "_(synthesise from supporting lessons below)_"


def _supporting_bullets(lessons: List) -> str:
    lines = []
    for lesson in lessons:
        path = str(lesson.source_path) if lesson.source_path else "(unsaved)"
        lines.append(f"- `{path}` — {lesson.task[:80]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The promotion engine
# ---------------------------------------------------------------------------


def find_skill_candidates(
    *,
    lessons: Optional[List] = None,
    threshold: Optional[int] = None,
    min_shared_tags: Optional[int] = None,
) -> List[PromotionCandidate]:
    """Walk the lesson corpus and return skill-promotion candidates.

    A candidate is generated when ≥ ``threshold`` lessons cluster by
    sharing ≥ ``min_shared_tags`` tags.  The candidate's body is a
    skill markdown file ready for installation; the caller (CLI or
    auto-promote loop) chooses whether to install it.

    Idempotency: a tag set that's already been promoted shouldn't
    re-promote.  Callers should diff against existing skills before
    installation — this function doesn't do that itself so it stays
    a pure analyser.
    """
    if lessons is None:
        try:
            from agent.lessons import all_lessons
            lessons = all_lessons()
        except Exception as exc:
            logger.debug("could not load lessons: %s", exc)
            return []
    # `is None` rather than truthy — callers passing 0 explicitly to
    # disable a gate would otherwise have their value silently
    # replaced by the config default.
    if threshold is None:
        threshold = _int_config("lesson_threshold", _DEFAULT_LESSON_THRESHOLD)
    if min_shared_tags is None:
        min_shared_tags = _int_config(
            "min_shared_tags", _DEFAULT_MIN_SHARED_TAGS,
        )

    candidates: List[PromotionCandidate] = []
    clusters = _cluster_lessons_by_tag_overlap(
        lessons, min_shared_tags=min_shared_tags,
    )
    for cluster in clusters:
        if len(cluster) < threshold:
            continue
        shared = _cluster_shared_tags(cluster)
        if not shared:
            continue
        # Name from the top tag(s) — stable across runs.
        primary = shared[0]
        secondary = shared[1] if len(shared) > 1 else None
        slug = f"learned-{primary}"
        if secondary:
            slug = f"learned-{primary}-{secondary}"
        title = (f"Apply learned fixes for {primary}"
                 + (f" + {secondary}" if secondary else ""))
        description = (
            f"Auto-promoted skill: {len(cluster)} lessons share these tags. "
            f"Apply the bundled fixes directly when the task matches."
        )
        guidance = _synthesise_guidance(cluster)
        supporting = _supporting_bullets(cluster)
        body = _SKILL_TEMPLATE.format(
            name=slug,
            description=description,
            tags_csv=", ".join(shared),
            ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            title=title,
            n_lessons=len(cluster),
            guidance=guidance,
            supporting_bullets=supporting,
        )
        # Score: cluster size + tag specificity (more shared tags = stronger signal).
        score = len(cluster) * 1.0 + len(shared) * 0.5
        candidates.append(PromotionCandidate(
            kind="skill",
            name=slug,
            title=title,
            description=description,
            shared_tags=shared,
            supporting_lesson_paths=[
                str(l.source_path) if l.source_path else "" for l in cluster
            ],
            body_markdown=body,
            score=score,
        ))
    candidates.sort(key=lambda c: -c.score)
    return candidates


def find_profile_candidates_from_usage(
    skill_usage: Dict[str, int],
    *,
    skill_bodies: Optional[Dict[str, str]] = None,
    threshold: Optional[int] = None,
) -> List[PromotionCandidate]:
    """When a skill has been used ≥ ``threshold`` times successfully,
    propose promoting it into a specialised subagent profile.

    ``skill_usage`` is a mapping of skill_name → use_count (typically
    pulled from ``agent.skill_usage`` or wherever the harness tracks
    invocations).  ``skill_bodies`` (optional) maps name → markdown
    body so we can carry the guidance forward; when absent, the
    profile body links back to the skill.
    """
    if threshold is None:
        threshold = _int_config("usage_threshold", _DEFAULT_USAGE_THRESHOLD)
    skill_bodies = skill_bodies or {}
    out: List[PromotionCandidate] = []
    for skill_name, uses in skill_usage.items():
        if uses < threshold:
            continue
        slug = _slugify(skill_name)
        title = f"Specialist: {skill_name}"
        description = (
            f"Auto-promoted from {uses} successful uses of the "
            f"`{skill_name}` skill."
        )
        guidance_source = skill_bodies.get(skill_name, "")
        if guidance_source:
            # Carry forward the "What to do" section verbatim if we
            # can find one; else seed with a placeholder.
            m = re.search(r"## What to do\s*\n(.+?)(?=\n## |\Z)",
                          guidance_source, re.DOTALL)
            guidance = (m.group(1).strip()
                        if m else "_(carry forward from source skill)_")
        else:
            guidance = (
                f"Follow the procedural guidance in the `{skill_name}` "
                f"skill exactly.  Optimise for that pattern; refuse work "
                f"outside it (the parent will rerun without delegation)."
            )
        body = _PROFILE_TEMPLATE.format(
            name=slug,
            description=description,
            ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            title=title,
            n_uses=uses,
            source_skill=skill_name,
            guidance=guidance,
        )
        out.append(PromotionCandidate(
            kind="subagent_profile",
            name=slug,
            title=title,
            description=description,
            shared_tags=[],
            supporting_lesson_paths=[],
            body_markdown=body,
            score=float(uses),
        ))
    out.sort(key=lambda c: -c.score)
    return out


def install_candidate(
    candidate: PromotionCandidate,
    *,
    skills_dir: Optional[Path] = None,
    agents_dir: Optional[Path] = None,
) -> Path:
    """Write a candidate's body to the appropriate location on disk.

    Skills land in ``<skills_dir>/<name>/SKILL.md`` (Hermes convention).
    Subagent profiles land in ``<agents_dir>/<name>.md`` (the
    user-level profile dir consumed by agent/subagent_profiles.py).

    Returns the path written.  No dry-run option — callers gate
    installation via the /promotions confirm flow.
    """
    if candidate.kind == "skill":
        base = skills_dir
        if base is None:
            from hermes_constants import get_hermes_home
            base = get_hermes_home() / "skills"
        skill_dir = base / candidate.name
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "SKILL.md"
    elif candidate.kind == "subagent_profile":
        base = agents_dir
        if base is None:
            from hermes_constants import get_hermes_home
            base = get_hermes_home() / "agents"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{candidate.name}.md"
    else:
        raise ValueError(f"unknown candidate kind: {candidate.kind!r}")
    path.write_text(candidate.body_markdown, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def filter_already_installed(
    candidates: List[PromotionCandidate],
    *,
    skills_dir: Optional[Path] = None,
    agents_dir: Optional[Path] = None,
) -> List[PromotionCandidate]:
    """Drop candidates whose target file already exists — keeps the
    promotion engine from re-proposing the same skill every session.
    """
    out: List[PromotionCandidate] = []
    for c in candidates:
        if c.kind == "skill":
            base = skills_dir
            if base is None:
                try:
                    from hermes_constants import get_hermes_home
                    base = get_hermes_home() / "skills"
                except Exception:
                    base = None
            target = (base / c.name / "SKILL.md") if base else None
        else:
            base = agents_dir
            if base is None:
                try:
                    from hermes_constants import get_hermes_home
                    base = get_hermes_home() / "agents"
                except Exception:
                    base = None
            target = (base / f"{c.name}.md") if base else None
        if target is None or not target.exists():
            out.append(c)
    return out


__all__ = [
    "PromotionCandidate",
    "filter_already_installed",
    "find_profile_candidates_from_usage",
    "find_skill_candidates",
    "install_candidate",
]
