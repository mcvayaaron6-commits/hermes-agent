---
sidebar_position: 14
title: "Auto-Skill Promotion"
description: "The moat-10x layer: clustered lessons get auto-promoted into reusable skills, and heavily-used skills into specialised subagent profiles."
---

# Auto-Skill Promotion

The compounding-intelligence engine that turns every verifier rework cycle into procedural memory the agent uses next time. Other agents have hooks, plan mode, verification, subagents. Hermes is the only one where the agent **builds its own future workforce from its own mistakes**.

## The full compounding loop

```
verifier catches rework → lesson captured → ≥3 lessons share ≥2 tags
                                ↓
                        skill candidate proposed (/promotions)
                                ↓
                  user approves: SKILL.md installed
                                ↓
                  agent uses skill next time (or auto-injects via lesson preamble)
                                ↓
                  ≥5 successful uses of the skill
                                ↓
                  subagent profile candidate proposed
                                ↓
                  user approves: profile installed in ~/.hermes/agents/
                                ↓
                  agent delegates the pattern to its specialist
```

Each step is durable. At the end of the loop, the agent has a domain-specialist subagent it built from its own past failures. The longer Hermes runs, the more specialised its workforce becomes — without anyone editing prompts.

## Promotion thresholds

Conservative on purpose. Promoting too eagerly fills the registry with noise.

| Promotion | Threshold | Config key |
|---|---|---|
| Skill | ≥3 lessons share ≥2 tags pairwise | `skill_promotion.lesson_threshold`, `skill_promotion.min_shared_tags` |
| Subagent profile | ≥5 successful uses of a skill | `skill_promotion.usage_threshold` |

Bump these higher for stricter promotion (slower learning, fewer false positives); bump lower for faster learning (more noise, more eager surfacing).

## How clustering works

Union-find over the lesson corpus. Two lessons are linked if they share at least `min_shared_tags` tags (case-insensitive). Connected components of size ≥ `lesson_threshold` become candidates.

```text
Lessons:
  L1: tags=[oauth, auth, callback]
  L2: tags=[oauth, auth, redirect]
  L3: tags=[oauth, auth, scope]
  L4: tags=[database, migration, schema]
  L5: tags=[database, migration, rollback]

With min_shared_tags=2, lesson_threshold=3:
  Cluster A: {L1, L2, L3}  shared=[oauth, auth]  → SKILL candidate
  Cluster B: {L4, L5}      shared=[database, migration]  → BELOW threshold
```

Cluster A produces a skill named `learned-oauth-auth` (slugified from the top two shared tags). The body of the skill is auto-synthesised from the cluster's collective fixes, deduplicated by the first 40 characters of each fix text.

## Candidate body shape

Auto-generated skill markdown:

```markdown
---
name: learned-oauth-auth
description: Auto-promoted skill: 3 lessons share these tags. ...
tags: [oauth, auth]
auto_promoted: true
auto_promoted_at: 2026-05-21 14:32:15
---

# Apply learned fixes for oauth + auth

This skill was auto-promoted from 3 lesson(s) that shared the tags
`oauth, auth`.  When the agent encounters a task matching this
pattern, it should follow the **What to do** section directly
rather than re-discovering the fix.

## What to do

1. Read OAUTH_REDIRECT from env, fall back to localhost only when unset
2. Use HTTPS callback URLs in any non-localhost environment
3. Validate the state parameter on every callback

## Supporting lessons

- `~/.hermes/lessons/add-oauth-login-20260516-1432.md` — Add OAuth login
- `~/.hermes/lessons/fix-staging-oauth-20260517-0900.md` — Fix staging OAuth
- `~/.hermes/lessons/add-state-validation-20260520-1115.md` — Add state validation

## When this applies

Apply when the task mentions any of: `oauth, auth`.
```

The skill carries the audit trail (supporting lesson paths) so a human reviewer can verify the promotion is well-grounded. Skill body is plain markdown — operators are free to hand-edit before / after install.

## The post-lesson nudge

When a new lesson lands (rework→VERIFIED), the CLI checks whether the corpus just crossed a new promotion threshold. If yes, a one-line nudge surfaces *once per emerging pattern*:

```text
✅ Verifier passed.
🌱 New skill candidate ready: `learned-oauth-auth` (score=4.0, tags=oauth,auth)
   Run /promotions to review, /promotions install learned-oauth-auth to adopt.
```

The nudge tracks `self._last_promotion_candidate_count` so it fires once per pattern, not every cycle. If you ignore three nudges, you'll see one more if a NEW pattern emerges — but the same one won't re-surface unprompted.

## The `/promotions` slash command

```bash
/promotions               # list candidates with scores, tags, descriptions
/promotions install <name>     # install one candidate
/promotions install-all   # install every candidate (skips pre-existing)
```

Example:

```text
> /promotions
  📦 2 promotion candidate(s):
    learned-oauth-auth               score=  4.5  tags=oauth,auth
      ↳ Auto-promoted skill: 3 lessons share these tags...
    learned-database-migration       score=  3.5  tags=database,migration
      ↳ Auto-promoted skill: 3 lessons share these tags...

  Install one: /promotions install <name>
  Install all: /promotions install-all

> /promotions install learned-oauth-auth
  ✅ Installed learned-oauth-auth  →  ~/.hermes/skills/learned-oauth-auth/SKILL.md
```

After install, the skill is discoverable like any other Hermes skill (`/skills`, `/skill-name`, hub etc.).

## Subagent profile promotion (the deepening loop)

When a skill installed via `/promotions install` is used ≥ `usage_threshold` times successfully (tracked via `tools/skill_usage.py`), the engine proposes promoting it further into a **subagent profile** — a named specialist the parent can delegate the entire pattern to:

```markdown
---
name: learned-oauth-auth
description: Auto-promoted from 7 successful uses of the `learned-oauth-auth` skill.
toolsets: [file, search]
auto_promoted: true
---

# Specialist: learned-oauth-auth

This subagent profile was auto-promoted from 7 successful applications
of the `learned-oauth-auth` skill.  When the parent agent encounters
a task matching this domain, prefer delegating to this specialised
subagent rather than handling it inline.

## Specialty

OAuth + auth implementation patterns the agent has refined over time.

## Approach

1. Read OAUTH_REDIRECT from env, fall back to localhost only when unset
2. Use HTTPS callback URLs in any non-localhost environment
3. Validate the state parameter on every callback

## Constraints

You inherit toolset `[file, search]`.  If you need additional toolsets
(terminal, web), the parent must grant them explicitly.  You cannot
delegate further — leaf agent only.
```

Now the parent delegates entire OAuth tasks via `subagent_type="learned-oauth-auth"` and gets a tuned specialist — built from past failures — handling them.

## Why this is the moat

Other agents ship features. **This agent ships organisational learning.** Three properties make the moat hard to copy:

1. **Network effects with self.** The more you use it, the better it gets at YOUR recurring patterns. A fresh-install competitor has no corpus.
2. **The lessons are durable artifacts.** Markdown files in `~/.hermes/lessons/` — hand-editable, shareable via git, transferable across machines.
3. **The promotion is auditable.** Every promoted skill cites the lessons that justified it. A new team member reading `learned-oauth-auth/SKILL.md` sees the receipts.

Bundle this loop into your team's `~/.hermes/lessons/` git repo and every team member's Hermes instantly knows everything anyone has learned. That's the institutional knowledge moat.

## Configuration

```yaml
skill_promotion:
  # Minimum lessons that must cluster before a skill is proposed.
  lesson_threshold: 3
  # Minimum tag overlap pairwise for cluster membership.
  min_shared_tags: 2
  # Successful uses before a skill promotes to subagent profile.
  usage_threshold: 5
```

## Idempotency guarantee

`filter_already_installed(candidates)` drops any candidate whose target file already exists, so the engine never re-proposes the same skill or profile every session. The promotion loop is monotonic — installations only accrue; nothing is auto-removed.

If you delete a skill or profile manually, the next session's check will re-propose it from the still-clustered lessons. To stop a pattern from re-promoting, prune the supporting lessons or merge them into a non-clustering set.

## When it doesn't fire

- **No reworks happen.** The agent succeeds first try every time — no lessons, no clustering, no promotions. (Verifier needs to be enabled for capture to start.)
- **Lessons don't cluster.** Patterns are too diverse / one-offs. Try lowering `min_shared_tags` or `lesson_threshold` (carefully — false positives waste your `/promotions` time).
- **English-only matching.** Tag derivation favours English tokens. Non-English projects should supply `extra_tags` in `capture_from_rework()`.

## What this isn't

- **Not auto-install.** Every promotion is a candidate; the user approves. No skill or profile gets installed without explicit `/promotions install <name>` or `--auto-approve-promotions` (planned, off by default).
- **Not cross-machine sharing.** The corpus lives at `~/.hermes/lessons/`. Put it in a synced directory (Dropbox, git) to share across boxes.
- **Not graph reasoning.** The clustering is fuzzy keyword overlap — strong, simple, fast at human-scale corpora. A vector store would generalise further; we'd build it when the no-embedding model breaks down.
