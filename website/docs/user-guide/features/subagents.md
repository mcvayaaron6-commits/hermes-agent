---
sidebar_position: 10
title: "Subagent Profiles"
description: "Named, repo-checked-in subagent definitions — system prompt, toolsets, and model routing as small markdown files."
---

# Subagent Profiles

Rather than pass a free-form `role` string and trust the agent to behave, callers can reference a **named profile** — `code-reviewer`, `security-auditor`, `test-writer`, `Explore` — whose system prompt, toolset whitelist, and (in a future commit) model routing all live in a small markdown file with YAML frontmatter, checked into version control.

Same idea Claude Code ships as named subagents in the Agent SDK; this is Hermes's version, designed to interoperate with the existing `delegate_task` tool rather than replace it.

## File layout

```
~/.hermes/agents/                user-global profiles
    code-reviewer.md
    security-auditor.md

<project>/.hermes/agents/        per-project profiles (project > user precedence)
    api-docs-writer.md
    migration-helper.md
```

Profiles are plain `.md` files. Anything else under those directories is ignored.

## File format

```markdown
---
name: code-reviewer
description: Reviews code changes for bugs, style, and security issues.
toolsets: [file, search, web]
model: openrouter/anthropic/claude-sonnet-4-6
max_iterations: 30
permission_mode: acceptEdits
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
```

**Frontmatter keys** (all optional except `name`, which defaults to the filename stem):

| Key | Type | What it does |
|---|---|---|
| `name` | string | Profile identifier; must match `[A-Za-z0-9_\-]+`. Defaults to the filename stem. |
| `description` | string | One-line summary shown in `/subagents`. |
| `toolsets` | list or comma-string | Restricts the subagent to these toolsets only. Falls back to defaults when omitted. |
| `model` | string | Provider-prefixed model override (`openrouter/anthropic/claude-sonnet-4-6`). Defaults to the parent's model. |
| `provider` | string | Provider name when not implied by `model`. |
| `max_iterations` | int | Per-subagent iteration cap. |
| `max_tokens` | int | Per-call output budget. |
| `permission_mode` | string | One of `default`, `acceptEdits`, `dontAsk`, `bypassPermissions` (reserved for future enforcement). |

**Body** (everything after the closing `---`): the subagent's system prompt. Plain markdown. Empty body → the profile is skipped with a logged warning.

## Using a profile

From within the agent loop, `delegate_task` accepts a `subagent_type` parameter:

```python
delegate_task(
    subagent_type="code-reviewer",
    goal="Review the OAuth callback handler at src/auth/oauth.ts",
)
```

Or per-task in a batch:

```python
delegate_task(tasks=[
    {"goal": "Audit auth code", "subagent_type": "security-auditor"},
    {"goal": "Write tests for the new endpoint", "subagent_type": "test-writer"},
])
```

The profile's system prompt is prepended to the task's context (separated by `---`); the toolset whitelist applies when the task hasn't set its own. Per-task fields explicitly set on the task beat profile defaults (least-surprise).

Unknown profile names return a clear error listing available profiles — `delegate_task` never silently falls through to a default identity.

## The slash commands

```bash
/subagents                # list all loaded profiles, with source + toolsets
/subagents reload         # re-read profile files from disk
/subagents show <name>    # show one profile's full body
/profiles                 # alias of /subagents
```

Example:

```text
> /subagents
  3 subagent profile(s) loaded:
    code-reviewer        [user]     tools=file,search,web   model=inherit
      Reviews code changes for bugs, style, and security issues.
    security-auditor     [project]  tools=file,search       model=openrouter/anthropic/claude-sonnet-4-6
      Audits diffs for OWASP-style issues using the project's threat model.
    Explore              [user]     tools=file,search       model=inherit
      Fast read-only search agent for locating code. Cannot mutate anything.
```

## Precedence

When two profiles share a name, the higher-precedence source wins:

1. `<cwd>/.hermes/agents/<name>.md` — **project**, highest
2. `~/.hermes/agents/<name>.md` — **user**
3. Bundled built-ins (none ship today; reserved) — **builtin**, lowest

Same-precedence collisions log a warning and the first one loaded wins (filesystem order is unstable, so don't rely on this).

## Recommended starter profiles

Copy these into `~/.hermes/agents/` to get going. Each is one self-contained markdown file.

### `code-reviewer.md`

```markdown
---
name: code-reviewer
description: Reviews code changes for bugs, style, and security
toolsets: [file, search, web]
---

# Code Reviewer

You are a senior code reviewer. For each change you see:

1. **Bugs first.** Look for null/None handling, off-by-one, race
   conditions, missing error handling, resource leaks. Cite file:line.
2. **Then style.** Project-convention adherence — read 2-3 sibling
   files before suggesting a refactor. Be confident the suggestion is
   actually consistent with the existing codebase.
3. **Then suggestions.** Performance, readability, test coverage.

Format: group by severity (HIGH / MEDIUM / LOW / nit). One line per
finding plus a code fence for the fix. No preamble, no "great work!"
filler. End with a recommendation: APPROVE, APPROVE WITH NITS, or
REQUEST CHANGES.
```

### `Explore.md`

```markdown
---
name: Explore
description: Fast read-only search agent for locating code
toolsets: [file, search, web]
---

# Explorer

You are a read-only locator. Given a question like "where is X
defined?" or "which files reference Y?", find it efficiently.

Workflow:
1. Use `search_files` for symbol or string searches.
2. Use `read_file` only when needed to confirm a match — don't read
   whole files speculatively.
3. Return a terse, structured answer: file paths with line numbers,
   a one-line description of each match. No prose explanations
   unless asked.

You cannot mutate anything. Refuse any tool call that would write.
```

### `test-writer.md`

```markdown
---
name: test-writer
description: Writes pytest tests against an existing implementation
toolsets: [file, search, terminal]
max_iterations: 25
---

# Test Writer

You write pytest tests against existing code. Workflow:

1. Read the implementation file thoroughly.
2. Find sibling test files for the project's test conventions
   (fixtures, marks, naming, parametrize patterns).
3. Write tests that cover: happy path, edge cases, error paths.
4. Run pytest against the new tests to confirm they actually pass
   against the current implementation.
5. If a test reveals a real bug in the implementation, surface it
   in your final message — don't silently fix the implementation
   from a test-writer profile.
```

## When subagent profiles shine

- **Repeated specialised tasks** — code review, doc writing, test
  generation, audits. The profile makes "be a strict reviewer" or
  "stay read-only" a one-line directive instead of a long preamble
  every call.
- **Team-shared conventions** — checked into `.hermes/agents/` in the
  repo, every contributor's Hermes spawns the same code-reviewer.
- **Cost optimisation** — route the `Explore` profile to a cheap fast
  model, keep the executor on Opus.
- **Permission isolation** — narrow toolsets keep an exploratory
  subagent from accidentally mutating state.

## Limitations (deliberate)

- **No model override yet** — the `model` and `max_iterations`
  frontmatter keys are parsed and shown in `/subagents`, but the
  wiring doesn't yet thread them into `_build_child_agent`'s
  credential-resolution path. Tracked as a follow-up.
- **No hot-reload on file change** — use `/subagents reload` after
  editing a profile.
- **No project-trust prompt** — `.hermes/agents/<name>.md` files are
  trusted implicitly (unlike `.hermes/hooks.json` which is sha256-
  pinned). The risk is lower because profiles only inject prompts
  and toolset filters, not arbitrary shell commands. If you want
  stricter handling, configure `delegation.allow_project_profiles:
  false` in `config.yaml` (planned).
