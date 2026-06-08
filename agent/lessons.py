"""
Lessons Learned — agent-curated memory of mistakes corrected.

This is the "compounding intelligence" layer.  Without it, every
verifier NEEDS_REWORK cycle teaches the agent something useful inside
the session but the lesson dies at /reset.  With it, the
agent records:

    Task: <what was being attempted>
    Initial approach: <first try the agent took>
    Verifier's complaint: <what went wrong>
    Fix: <what worked>
    Tags: <topic keywords for future retrieval>

into a structured ``~/.hermes/lessons/<slug>-<ts>.md`` file.  At the
start of every subsequent session, ``relevant_lessons(task_description)``
surfaces the top-N lessons whose tags or task description match — they
get injected into the agent's system prompt as a "you have prior
experience with similar tasks" preamble.

The design is intentionally simple:

* No embedding store, no vector search — fuzzy keyword match on
  tags + task description is enough at human-readable scales (~100s
  of lessons per user).  When that breaks down, swap in a real
  retrieval layer behind ``relevant_lessons``.
* Lessons are markdown — humans can hand-edit, prune, share via git.
* Tags are derived automatically from the task and verifier summary
  when the operator doesn't supply them, so the corpus grows without
  manual annotation overhead.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)


_LESSONS_DIRNAME = "lessons"
_LESSON_SUFFIX = ".md"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TAG_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-_]+")
_DEFAULT_RELEVANT_LIMIT = 3
_DEFAULT_PROMPT_PREAMBLE_BUDGET = 2_000  # characters


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class Lesson:
    """One captured rework→success delta the agent can learn from."""

    task: str
    initial_approach: str
    verifier_complaint: str
    fix: str
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    source_path: Optional[Path] = None
    session_id: Optional[str] = None
    #: Number of times this lesson has been surfaced into a future
    #: system prompt.  Useful for pruning stale low-relevance ones.
    surfaced_count: int = 0

    def render_markdown(self) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at))
        front = [
            "---",
            f"created_at: {ts}",
            f"tags: [{', '.join(sorted(set(self.tags)))}]",
        ]
        if self.session_id:
            front.append(f"session_id: {self.session_id}")
        if self.surfaced_count:
            front.append(f"surfaced_count: {self.surfaced_count}")
        front.append("---")
        body = [
            f"# Lesson: {self.task.strip()}",
            "",
            "## Initial approach",
            "",
            self.initial_approach.strip() or "_(not captured)_",
            "",
            "## What went wrong",
            "",
            self.verifier_complaint.strip() or "_(not captured)_",
            "",
            "## What worked",
            "",
            self.fix.strip() or "_(not captured)_",
            "",
        ]
        return "\n".join(front + [""] + body)

    def to_compact_prompt(self) -> str:
        """A one-paragraph version suitable for system-prompt injection.

        Designed to be cheap on tokens but specific enough that the
        model can recognise applicability.  Format intentionally
        identical across lessons so the model treats each as a peer.
        """
        return (
            f"- **{self.task.strip()[:80]}** — "
            f"Tried: {self.initial_approach.strip()[:120]} "
            f"Failed because: {self.verifier_complaint.strip()[:120]} "
            f"Fix: {self.fix.strip()[:160]}"
        )


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _lessons_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / _LESSONS_DIRNAME
    except Exception:
        return Path.home() / ".hermes" / _LESSONS_DIRNAME


def _slugify(text: str, *, max_len: int = 48) -> str:
    cleaned = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    if not cleaned:
        return "lesson"
    return cleaned[:max_len].rstrip("-") or "lesson"


# ---------------------------------------------------------------------------
# Tag derivation
# ---------------------------------------------------------------------------


#: Stop-word list for tag derivation — keeps the corpus signal-y.
#: Intentionally small (English only) — operators with non-English
#: lessons can supply tags explicitly.
_STOP_WORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "this", "that", "from", "have", "into",
    "your", "you", "are", "was", "were", "but", "not", "any", "all",
    "can", "use", "using", "used", "their", "them", "they", "what",
    "when", "where", "why", "how", "which", "who", "whom", "whose",
    "should", "would", "could", "must", "will", "did", "does", "doing",
    "done", "make", "made", "had", "has", "been", "being", "just",
    "than", "then", "there", "here", "more", "some", "very", "much",
    "most", "many", "few", "lot", "lots", "also", "still", "now", "yet",
    "lesson", "task", "session", "agent", "model",
})


def derive_tags(*texts: str, limit: int = 6) -> List[str]:
    """Heuristic tag extractor: word frequency minus stop words.

    Used when the operator doesn't supply explicit tags.  Aggregates
    word frequency across all input texts (task + verifier summary +
    fix), drops stop words and tokens shorter than 4 characters, then
    returns the top ``limit`` distinct tokens sorted by count.

    Deterministic for stable test assertions.
    """
    counts: Dict[str, int] = {}
    for text in texts:
        for match in _TAG_RE.finditer(text or ""):
            token = match.group(0).lower()
            if len(token) < 4:
                continue
            if token in _STOP_WORDS:
                continue
            counts[token] = counts.get(token, 0) + 1
    # Sort by (-count, token) so ties break alphabetically for determinism.
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [tag for tag, _ in ranked[:limit]]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<front>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def save_lesson(lesson: Lesson, *, lessons_dir: Optional[Path] = None) -> Path:
    """Write a lesson to disk.  Returns the path.

    Filename: ``<slug>-<YYYYmmdd-HHMMSS>.md`` to avoid collisions when
    the same task triggers multiple lessons in the same minute.
    """
    target_dir = lessons_dir or _lessons_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(lesson.created_at))
    slug = _slugify(lesson.task)
    path = target_dir / f"{slug}-{stamp}{_LESSON_SUFFIX}"
    # If the file already exists (sub-second collision), append a counter.
    if path.exists():
        for i in range(1, 100):
            candidate = target_dir / f"{slug}-{stamp}-{i}{_LESSON_SUFFIX}"
            if not candidate.exists():
                path = candidate
                break
    path.write_text(lesson.render_markdown(), encoding="utf-8")
    lesson.source_path = path
    return path


def load_lesson(path: Path) -> Optional[Lesson]:
    """Parse a lesson markdown file.  Returns None on any parse failure."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _FRONTMATTER_RE.match(text)
    front: Dict[str, Any] = {}
    body = text
    if match is not None:
        body = match.group("body")
        front_raw = match.group("front")
        try:
            import yaml
            parsed = yaml.safe_load(front_raw) or {}
            if isinstance(parsed, dict):
                front = parsed
        except Exception:
            front = {}

    # Parse body into sections.
    sections: Dict[str, str] = {}
    last_pos = 0
    last_name: Optional[str] = None
    task = ""
    for line in body.splitlines():
        if line.startswith("# Lesson:"):
            task = line[len("# Lesson:"):].strip()

    cursor = 0
    matches = list(_SECTION_RE.finditer(body))
    for i, m in enumerate(matches):
        name = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[name] = body[start:end].strip()

    def _clean(text: str) -> str:
        text = text.strip()
        if text.startswith("_(") and text.endswith(")_"):
            return ""
        return text

    tags = front.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif not isinstance(tags, list):
        tags = []

    created_at = time.time()
    raw_ts = front.get("created_at")
    if isinstance(raw_ts, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                created_at = time.mktime(time.strptime(raw_ts, fmt))
                break
            except ValueError:
                continue

    surfaced_count = front.get("surfaced_count") or 0
    try:
        surfaced_count = int(surfaced_count)
    except (TypeError, ValueError):
        surfaced_count = 0

    return Lesson(
        task=task,
        initial_approach=_clean(sections.get("initial approach", "")),
        verifier_complaint=_clean(sections.get("what went wrong", "")),
        fix=_clean(sections.get("what worked", "")),
        tags=[str(t) for t in tags],
        created_at=created_at,
        source_path=path,
        session_id=front.get("session_id"),
        surfaced_count=surfaced_count,
    )


def all_lessons(*, lessons_dir: Optional[Path] = None) -> List[Lesson]:
    """Load every lesson on disk.  Skips files that fail to parse."""
    target_dir = lessons_dir or _lessons_dir()
    if not target_dir.is_dir():
        return []
    out: List[Lesson] = []
    try:
        files = sorted(target_dir.glob(f"*{_LESSON_SUFFIX}"))
    except OSError:
        return []
    for path in files:
        lesson = load_lesson(path)
        if lesson is not None:
            out.append(lesson)
    return out


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def _score_lesson(lesson: Lesson, query_text: str, query_tags: Sequence[str]) -> float:
    """Score a lesson's relevance to a query.  Higher = more relevant.

    Combines:
    * Tag overlap (each shared tag = 2 points)
    * Task-text token overlap (each shared non-stop token = 1 point)
    * Light recency boost (younger lessons slightly preferred)
    """
    query_lower = (query_text or "").lower()
    query_tokens = {
        m.group(0).lower()
        for m in _TAG_RE.finditer(query_lower)
        if len(m.group(0)) >= 4 and m.group(0).lower() not in _STOP_WORDS
    }
    lesson_tags = {t.lower() for t in lesson.tags}
    tag_score = 2 * len(lesson_tags & {t.lower() for t in query_tags})
    text_tokens = {
        m.group(0).lower()
        for m in _TAG_RE.finditer(lesson.task.lower())
        if len(m.group(0)) >= 4 and m.group(0).lower() not in _STOP_WORDS
    }
    text_score = len(query_tokens & text_tokens)
    base_score = tag_score + text_score
    if base_score == 0:
        # No signal — recency alone is noise.  Returning 0 here lets
        # relevant_lessons() filter the lesson out entirely rather
        # than surfacing an unrelated one just because it's fresh.
        return 0.0
    # Recency: lessons from the last 30 days get a small boost over
    # equally-scoring older ones.
    age_days = max(0, (time.time() - lesson.created_at) / 86400)
    recency = max(0.0, 1.0 - (age_days / 30.0))
    return base_score + recency * 0.5


def relevant_lessons(
    query_text: str,
    *,
    query_tags: Optional[Sequence[str]] = None,
    limit: int = _DEFAULT_RELEVANT_LIMIT,
    lessons_dir: Optional[Path] = None,
) -> List[Lesson]:
    """Return the top-``limit`` lessons most relevant to ``query_text``.

    Lessons with zero score are filtered out — better to surface nothing
    than to surface noise that displaces real context.
    """
    query_tags = list(query_tags or [])
    if not query_tags:
        query_tags = derive_tags(query_text, limit=6)
    scored: List[tuple[float, Lesson]] = []
    for lesson in all_lessons(lessons_dir=lessons_dir):
        score = _score_lesson(lesson, query_text, query_tags)
        if score > 0:
            scored.append((score, lesson))
    scored.sort(key=lambda pair: (-pair[0], -pair[1].created_at))
    return [lesson for _, lesson in scored[:limit]]


def format_lessons_preamble(
    lessons: Iterable[Lesson],
    *,
    budget_chars: int = _DEFAULT_PROMPT_PREAMBLE_BUDGET,
) -> str:
    """Render lessons as a system-prompt preamble.  Returns "" when no
    lessons are provided so the caller can skip injection entirely.

    The output stays under ``budget_chars`` characters; lessons are
    dropped from the end until it fits.
    """
    items = list(lessons)
    if not items:
        return ""
    header = (
        "## Prior lessons learned\n\n"
        "You have completed similar tasks before.  Each bullet below is a "
        "lesson distilled from a previous session where your first attempt "
        "needed rework.  Apply the FIX directly when relevant.\n\n"
    )
    rendered: List[str] = []
    running = len(header)
    for lesson in items:
        line = lesson.to_compact_prompt() + "\n"
        if running + len(line) > budget_chars:
            break
        rendered.append(line)
        running += len(line)
    if not rendered:
        return ""
    return header + "".join(rendered)


# ---------------------------------------------------------------------------
# Capture from verifier rework cycles
# ---------------------------------------------------------------------------


def capture_from_rework(
    *,
    task: str,
    initial_response: str,
    needs_rework_summary: str,
    final_response: str,
    session_id: Optional[str] = None,
    extra_tags: Optional[Sequence[str]] = None,
    lessons_dir: Optional[Path] = None,
) -> Optional[Lesson]:
    """Capture a lesson from a "first attempt → rework → success" cycle.

    Designed to be called by the harness after a verifier-driven rework
    converges on VERIFIED.  Returns the saved Lesson, or None if the
    capture is too thin to be useful (empty task, empty rework summary).
    """
    if not task.strip() or not needs_rework_summary.strip():
        return None
    tags = list(extra_tags or [])
    if not tags:
        tags = derive_tags(task, needs_rework_summary, final_response, limit=6)
    lesson = Lesson(
        task=task.strip(),
        initial_approach=initial_response.strip(),
        verifier_complaint=needs_rework_summary.strip(),
        fix=final_response.strip(),
        tags=tags,
        session_id=session_id,
    )
    try:
        save_lesson(lesson, lessons_dir=lessons_dir)
    except OSError as exc:
        logger.warning("could not save lesson to disk: %s", exc)
        return None
    return lesson


__all__ = [
    "Lesson",
    "all_lessons",
    "capture_from_rework",
    "derive_tags",
    "format_lessons_preamble",
    "load_lesson",
    "relevant_lessons",
    "save_lesson",
]
