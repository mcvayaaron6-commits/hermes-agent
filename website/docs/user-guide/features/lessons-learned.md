---
sidebar_position: 12
title: "Lessons Learned"
description: "Hermes's compounding-intelligence layer — capture verifier rework cycles as markdown lessons, auto-inject relevant ones into future sessions."
---

# Lessons Learned

Without this layer, every [verifier](./verification) `NEEDS_REWORK` cycle teaches the agent something useful inside the session, but the lesson dies at `/reset`. With it, Hermes captures the "first attempt → rework → success" delta as a structured markdown file, then surfaces relevant lessons at the start of future sessions working on similar tasks.

This is the **compounding-intelligence** loop: the agent genuinely gets better at recurring task patterns across sessions without anyone touching the prompt.

## The loop

```
1. User asks for X.
2. Agent tries X → initial_response.
3. Verifier surfaces NEEDS_REWORK with a specific complaint.
4. CLI buffers: task + initial_response + rework_summary.
5. Agent fixes the issues; verifier returns VERIFIED.
6. CLI calls capture_from_rework() → writes a lesson markdown file.
7. Next session, on the first user prompt:
   - relevant_lessons(prompt) scores all lessons by tag + text overlap
   - Top 3 are formatted into a compact preamble
   - Preamble is prepended inside a <lessons-learned> envelope on the
     user message before the model sees it
8. Model treats it as "you've done similar tasks before — apply the
   fix directly when relevant."
```

## File format

Lessons live at `~/.hermes/lessons/<slug>-<YYYYmmdd-HHMMSS>.md`:

```markdown
---
created_at: 2026-05-21 14:32:15
tags: [auth, callback, oauth, redirect, staging]
session_id: sess_abc123
---

# Lesson: Add OAuth login to the dashboard

## Initial approach

Used a hardcoded `redirect_uri: "http://localhost:3000/callback"` in
the OAuth client config.

## What went wrong

OAuth callback returned 500 in staging because the redirect URI was
localhost — the IdP rejected it because the staging deploy is at
`https://staging.example.com`.

## What worked

Read `OAUTH_REDIRECT` from env, fall back to localhost only when
unset.  Updated `docker-compose.staging.yml` to set the env var.
```

Tags are auto-derived from word frequency (minus stop words) across the task + verifier complaint + fix. Operators can also pass `extra_tags` to `capture_from_rework()` directly.

## In-session browsing: `/lessons`

```bash
/lessons                # 5 most recent
/lessons <query>        # relevance search
/lessons count          # total + path
/lessons show <name>    # full body of one lesson
```

Example:

```text
> /lessons
  📚 17 lesson(s) total — showing 5 most recent:
    2026-05-21  Add OAuth login to the dashboard      [auth,oauth,redirect,staging]
    2026-05-20  Fix flaky integration test (#1234)    [flaky,integration,test]
    2026-05-19  Migrate session table to bigint       [bigint,migration,session]
    2026-05-19  Add rate limiting to /api/login       [api,limiting,login,rate]
    2026-05-18  Patch CVE-2026-1234 in deps           [cve,deps,patch,security]

> /lessons oauth
  📚 3 relevant lesson(s) for 'oauth':
    Add OAuth login to the dashboard      [auth,oauth,redirect,staging]
    Refresh-token rotation broke on Safari  [oauth,rotation,safari,token]
    Backfill missing OAuth scopes          [backfill,oauth,scopes]
```

## How relevance is scored

```
tag_score   = 2 × |lesson.tags ∩ query.tags|
text_score  = |lesson.task_tokens ∩ query.task_tokens|
base_score  = tag_score + text_score
if base_score == 0:
    return 0    # filter out — better silence than noise
recency_bonus = max(0, 1.0 - (age_days / 30)) × 0.5
final_score = base_score + recency_bonus
```

A lesson with zero tag and text overlap scores **0**, not 0.5 — recency alone is noise. Recency only matters as a tiebreaker among lessons that already have signal.

This is intentionally simple (no embeddings, no vector store). At ~100s of lessons per user, fuzzy keyword + tag overlap is enough. When that breaks down, swap in a real retrieval layer behind `relevant_lessons()`.

## Preamble shape

When a session has matching lessons, the user's first message becomes:

```
<lessons-learned>
## Prior lessons learned

You have completed similar tasks before.  Each bullet below is a
lesson distilled from a previous session where your first attempt
needed rework.  Apply the FIX directly when relevant.

- **Add OAuth login to the dashboard** — Tried: Used hardcoded redirect URI Failed because: OAuth callback returned 500 in staging Fix: Read OAUTH_REDIRECT from env, fall back to localhost
- **Backfill missing OAuth scopes** — Tried: Wrote a one-shot migration Failed because: Concurrent writes during backfill Fix: Use SELECT FOR UPDATE on each row batch
</lessons-learned>

<original user message>
```

The 2 KB character budget is shared across all bullets — older lessons drop first when the budget runs out.

## Programmatic API

```python
from agent.lessons import (
    capture_from_rework,    # call after a rework-then-success cycle
    relevant_lessons,       # retrieve top-N matches for a query
    format_lessons_preamble,  # render for prompt injection
    all_lessons,            # full corpus
)

# Capture
lesson = capture_from_rework(
    task="Refactor auth module",
    initial_response="...",
    needs_rework_summary="...",
    final_response="...",
    extra_tags=["auth", "refactor"],   # optional override
)

# Retrieve
for lesson in relevant_lessons("auth refactor", limit=5):
    print(lesson.task, lesson.tags)
```

## When this shines

- **Recurring failure modes.** "Hardcoded URLs broke staging" — agent fails once, learns once, never fails for the same reason again.
- **Project-specific gotchas.** Hand-edit `~/.hermes/lessons/` to seed your own corpus with "always remember to run migrations after schema changes" etc.
- **Onboarding new repos.** Share a `lessons/` directory across the team via git so new contributors' agents inherit the institutional knowledge.

## When this doesn't help (yet)

- **One-shot tasks with no rework.** The capture only fires on `VERIFIED` after at least one `NEEDS_REWORK` cycle. Tasks that succeed first try don't generate lessons.
- **Cross-language transfer.** Token-based matching favours English. Non-English lesson corpora work but score less precisely.
- **Lesson rot.** No auto-pruning — old, stale, or contradicted lessons keep ranking until a human deletes them. `/lessons count` shows the corpus size; eyeball it occasionally.

## Limitations

- Capture is best-effort. A disk failure during `capture_from_rework()` logs at debug level and continues.
- Preamble injection is one-shot per agent session — subsequent turns in the same session don't get fresh lessons. (The agent already has the context from the first turn's preamble.)
- The retrieval is local-only. No cross-machine sharing unless you put `~/.hermes/lessons/` in a synced directory or git repo.
