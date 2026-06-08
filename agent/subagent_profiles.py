"""
Named Subagent Profiles — declarative, repo-checked-in agent definitions.

Claude Code's biggest delegation polish: rather than passing a free-form
``role`` string and trusting the agent to behave, callers reference a
named profile (``code-reviewer``, ``security-auditor``, ``test-writer``,
``Explore``) whose system prompt, toolset whitelist, and model routing
are checked into version control as a small markdown file with YAML
frontmatter.

File layout
-----------

::

    ~/.hermes/agents/                   # user-global profiles
        code-reviewer.md
        security-auditor.md

    .hermes/agents/                     # per-project profiles (trust-on-first-use)
        api-docs-writer.md
        migration-helper.md

File format
-----------

::

    ---
    name: code-reviewer
    description: Reviews code changes for bugs, style, and security issues.
    toolsets: [file, search, web]
    model: openrouter/anthropic/claude-sonnet-4-6
    max_iterations: 30
    ---

    # Code Reviewer

    You are an expert code reviewer. Your job is to find bugs first,
    then style issues, then suggest improvements. Be concrete — cite
    file:line for every claim. Be terse — no preamble, no "great
    code!" filler.

    Workflow:
    1. Read the diff against the base branch
    2. Read the surrounding files for context
    3. Group findings by severity (HIGH / MEDIUM / LOW / nit)
    4. Emit one structured review

The body of the markdown file (everything after the frontmatter) becomes
the subagent's ephemeral system prompt.  The frontmatter governs which
toolsets the subagent can use, optional model override, and an
iteration cap.

Why this module is small and pure
---------------------------------

Same engine-first pattern used for hooks / plan_mode / verification:
this file just defines the dataclass, the parser, and a directory
loader.  Wiring into ``tools.delegate_tool`` lives in a separate commit
so the engine is unit-testable in isolation.

Discovery precedence (highest first when names collide)
-------------------------------------------------------

1. Project file at ``<cwd>/.hermes/agents/<name>.md`` — repo-controlled
2. User file at ``~/.hermes/agents/<name>.md`` — personal defaults
3. Built-in / bundled profiles under ``hermes-agent/skills/profiles/``
   (none ship today; reserved for future built-ins like ``Explore``,
   ``code-reviewer``, ``Plan``)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Filename suffix for profile markdown files.  Anything else under the
#: agents/ directory is ignored — keeps notes, README files, etc. from
#: being misinterpreted as profiles.
_PROFILE_SUFFIX = ".md"

#: Maximum profile name length.  Long enough for descriptive names
#: ("security-auditor-strict"), short enough to fit in CLI status lines.
_MAX_NAME_LENGTH = 64

#: Regex characters allowed in a profile name.  Keeps the filesystem
#: layout predictable and avoids OS-specific footguns (spaces, slashes,
#: case-folding traps).
_VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")

#: YAML frontmatter delimiter — three dashes, both start and end.
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<front>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)

#: Keys recognised in the frontmatter.  Unknown keys are logged at debug
#: level so a typo doesn't silently lose configuration.
_KNOWN_KEYS: frozenset[str] = frozenset({
    "name", "description", "toolsets", "model", "provider",
    "max_iterations", "max_tokens", "permission_mode",
})


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class SubagentProfile:
    """One named subagent profile loaded from disk.

    Attributes are intentionally close to the parameters
    ``tools.delegate_tool.delegate_task`` already accepts — the wiring
    layer just unpacks this dataclass into the existing keyword args.
    """

    name: str
    system_prompt: str
    description: str = ""
    toolsets: Optional[List[str]] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    max_iterations: Optional[int] = None
    max_tokens: Optional[int] = None
    #: One of the standard permission profile names — ``default``,
    #: ``acceptEdits``, ``dontAsk``, ``bypassPermissions``.  Maps onto
    #: the existing approval / guardrail config.
    permission_mode: Optional[str] = None
    #: Path the profile was loaded from.  Useful for ``/agents show``
    #: and for invalidating caches when the file changes on disk.
    source_path: Optional[Path] = None
    #: One of ``"project"``, ``"user"``, ``"builtin"`` for display.
    source: str = "user"

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "toolsets": list(self.toolsets) if self.toolsets else None,
            "model": self.model,
            "provider": self.provider,
            "max_iterations": self.max_iterations,
            "max_tokens": self.max_tokens,
            "permission_mode": self.permission_mode,
            "source": self.source,
            "source_path": str(self.source_path) if self.source_path else None,
        }


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple[Dict[str, Any], str]:
    """Split markdown text into (frontmatter dict, body str).

    Tolerant of:
    * No frontmatter at all (returns empty dict + the original text)
    * Missing/empty body (returns the parsed dict + "")
    * YAML failures (returns empty dict, logs a warning, falls back to
      treating the whole file as body)
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text
    front_raw = match.group("front")
    body = match.group("body")
    try:
        import yaml  # PyYAML ships with Hermes already
    except ImportError:  # pragma: no cover — PyYAML is a hard dep
        logger.debug("PyYAML missing; treating frontmatter as opaque body")
        return {}, text
    try:
        parsed = yaml.safe_load(front_raw) or {}
    except yaml.YAMLError as exc:
        logger.warning("Could not parse subagent-profile frontmatter: %s", exc)
        return {}, body
    if not isinstance(parsed, dict):
        logger.warning("Subagent-profile frontmatter must be a YAML mapping, "
                       "got %s — ignoring", type(parsed).__name__)
        return {}, body
    return parsed, body


def _validate_name(name: str) -> Optional[str]:
    """Return an error string if ``name`` is invalid, None if OK."""
    if not name or not isinstance(name, str):
        return "name must be a non-empty string"
    if len(name) > _MAX_NAME_LENGTH:
        return f"name too long (>{_MAX_NAME_LENGTH} chars)"
    if not _VALID_NAME_RE.match(name):
        return ("name may only contain letters, digits, hyphen, underscore "
                f"— got {name!r}")
    return None


def _coerce_toolsets(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    logger.warning("toolsets must be a list or comma-separated string; "
                   "got %r — ignoring", type(value).__name__)
    return None


def _coerce_int(value: Any, *, field_name: str) -> Optional[int]:
    if value is None:
        return None
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        logger.warning("%s must be an integer; got %r — ignoring",
                       field_name, value)
        return None
    if coerced <= 0:
        logger.warning("%s must be positive; got %d — ignoring",
                       field_name, coerced)
        return None
    return coerced


def parse_profile(
    text: str,
    *,
    source_path: Optional[Path] = None,
    source: str = "user",
    default_name: Optional[str] = None,
) -> Optional[SubagentProfile]:
    """Parse one markdown-with-frontmatter document into a SubagentProfile.

    Returns None on any validation failure (with the reason logged).
    Forgiving by design — a typo in one profile file shouldn't break
    discovery of the others.
    """
    front, body = _parse_frontmatter(text)
    body = (body or "").strip()
    if not body:
        logger.warning("subagent profile at %s has empty body — skipping",
                       source_path)
        return None

    name = front.get("name") or default_name
    err = _validate_name(name or "")
    if err is not None:
        logger.warning("subagent profile at %s: %s — skipping",
                       source_path, err)
        return None

    description = str(front.get("description") or "").strip()
    toolsets = _coerce_toolsets(front.get("toolsets"))
    model = front.get("model")
    if model is not None and not isinstance(model, str):
        logger.warning("model must be a string in %s — ignoring", source_path)
        model = None
    provider = front.get("provider")
    if provider is not None and not isinstance(provider, str):
        provider = None
    max_iterations = _coerce_int(front.get("max_iterations"),
                                 field_name="max_iterations")
    max_tokens = _coerce_int(front.get("max_tokens"), field_name="max_tokens")
    permission_mode = front.get("permission_mode")
    if permission_mode is not None and not isinstance(permission_mode, str):
        permission_mode = None

    # Log unknown keys so typos surface but don't crash discovery.
    unknown = set(front.keys()) - _KNOWN_KEYS
    if unknown:
        logger.debug("subagent profile %s has unknown frontmatter keys: %s",
                     name, ", ".join(sorted(unknown)))

    return SubagentProfile(
        name=str(name),
        system_prompt=body,
        description=description,
        toolsets=toolsets,
        model=model,
        provider=provider,
        max_iterations=max_iterations,
        max_tokens=max_tokens,
        permission_mode=permission_mode,
        source_path=source_path,
        source=source,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SubagentProfileRegistry:
    """Holds the loaded set of profiles and resolves lookups by name.

    The registry is built once per agent at startup.  Profile-file
    edits don't hot-reload — call ``reload()`` (or use the ``/agents
    reload`` slash command) when you change a file on disk.
    """

    def __init__(self, profiles: Iterable[SubagentProfile] = ()) -> None:
        self._by_name: Dict[str, SubagentProfile] = {}
        for profile in profiles:
            self.register(profile)

    def __len__(self) -> int:
        return len(self._by_name)

    def __iter__(self):
        return iter(self._by_name.values())

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def register(self, profile: SubagentProfile) -> None:
        """Add a profile, with project > user > builtin precedence.

        If a profile with the same ``name`` already exists, the one with
        the higher-precedence ``source`` wins.  Same-precedence
        collisions log a warning and the later registration loses
        (filesystem order is unstable).
        """
        existing = self._by_name.get(profile.name)
        if existing is None:
            self._by_name[profile.name] = profile
            return
        if _source_precedence(profile.source) > _source_precedence(existing.source):
            logger.debug(
                "subagent profile %r: %s entry overrides %s entry",
                profile.name, profile.source, existing.source,
            )
            self._by_name[profile.name] = profile
            return
        if _source_precedence(profile.source) == _source_precedence(existing.source):
            logger.warning(
                "subagent profile %r defined twice at %s and %s — keeping the first",
                profile.name, existing.source_path, profile.source_path,
            )

    def get(self, name: str) -> Optional[SubagentProfile]:
        return self._by_name.get(name)

    def names(self) -> List[str]:
        return sorted(self._by_name.keys())

    def describe(self) -> List[Dict[str, Any]]:
        return [p.describe() for p in self]


_SOURCE_RANK = {"builtin": 0, "user": 1, "project": 2}


def _source_precedence(source: str) -> int:
    return _SOURCE_RANK.get(source, 0)


# ---------------------------------------------------------------------------
# Disk loader
# ---------------------------------------------------------------------------


def _safe_iter_dir(path: Path) -> List[Path]:
    if not path.is_dir():
        return []
    try:
        return sorted(path.iterdir())
    except OSError as exc:
        logger.debug("could not iterate %s: %s", path, exc)
        return []


def _load_profile_from_file(
    path: Path, *, source: str,
) -> Optional[SubagentProfile]:
    if not path.is_file():
        return None
    if path.suffix != _PROFILE_SUFFIX:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("could not read profile %s: %s", path, exc)
        return None
    default_name = path.stem
    return parse_profile(text, source_path=path, source=source,
                         default_name=default_name)


def discover_profiles(
    *,
    cwd: Optional[Path] = None,
    user_agents_dir: Optional[Path] = None,
    builtin_dirs: Sequence[Path] = (),
) -> SubagentProfileRegistry:
    """Walk the standard locations and return a populated registry.

    Order of loading (later overrides earlier where ``source`` rank is
    higher — see ``SubagentProfileRegistry.register``):

    1. Built-in dirs (typically empty for now; reserved for bundled
       profiles like ``Explore`` or ``code-reviewer``)
    2. ``user_agents_dir`` (defaults to ``~/.hermes/agents``)
    3. ``<cwd>/.hermes/agents`` if it exists

    The function never raises — discovery failures (bad file, bad
    frontmatter) log warnings and continue.  An empty registry is a
    valid, working state.
    """
    registry = SubagentProfileRegistry()
    for builtin_dir in builtin_dirs:
        for child in _safe_iter_dir(builtin_dir):
            profile = _load_profile_from_file(child, source="builtin")
            if profile is not None:
                registry.register(profile)

    if user_agents_dir is None:
        try:
            from hermes_constants import get_hermes_home  # local to avoid cycle
            user_agents_dir = get_hermes_home() / "agents"
        except Exception:
            user_agents_dir = None
    if user_agents_dir is not None:
        for child in _safe_iter_dir(user_agents_dir):
            profile = _load_profile_from_file(child, source="user")
            if profile is not None:
                registry.register(profile)

    if cwd is None:
        try:
            cwd = Path.cwd()
        except (OSError, RuntimeError):
            cwd = None
    if cwd is not None:
        project_dir = cwd / ".hermes" / "agents"
        for child in _safe_iter_dir(project_dir):
            profile = _load_profile_from_file(child, source="project")
            if profile is not None:
                registry.register(profile)

    return registry


__all__ = [
    "SubagentProfile",
    "SubagentProfileRegistry",
    "discover_profiles",
    "parse_profile",
]
