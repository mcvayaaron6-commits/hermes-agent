---
sidebar_position: 8
title: "Plan Mode"
description: "Separate read-only investigation from mutating execution — produce an auditable plan before the agent touches anything."
---

# Plan Mode

Plan Mode is a Hermes session toggle that puts the agent into a
read-only state until you explicitly approve its plan.  While Plan Mode
is on, the dispatcher refuses any tool that isn't on the read-only
allowlist (`read_file`, `list_directory`, `search_files`, `web_search`,
`web_fetch`, `session_search`, `session_insights`, `skill_search`,
`skill_browse`, `skill_view`, `memory` in read modes, `todo`, `clarify`,
`todo`, `clarify`).  Anything else — `write_file`, `patch`, `terminal`,
`send_message`, browser POSTs, image generation — returns a refusal
that tells the model exactly why and what to do next.

The result: the agent's only path forward is to investigate and produce
a structured plan.  You read the plan, edit it if you like, and only
then switch to Act Mode to execute it.  Same idea Cline ships as
"Plan / Act," and Claude Code as Plan Mode.

## The slash commands

```bash
/plan <task description>   # enter Plan Mode for this task
/plan-show                 # display the current session's plan artifact
/exit-plan                 # leave Plan Mode and execute the plan
/verify                    # run a verification pass on the latest response
```

A typical Plan Mode flow:

```text
> /plan Add OAuth login to the dashboard
  📋 Plan Mode active — read-only tools only.
     Task: Add OAuth login to the dashboard
     Plan will be saved to: .hermes/plans/add-oauth-login-20260516-143215.md
     Investigate using read_file, search_files, web_search, ...
     Use /exit-plan when ready to execute, /cancel-plan to abort.

[agent investigates with search_files, read_file, web_search ...]
[agent attempts write_file — refused by Plan Mode]
[agent writes the plan content as its final response]

> /exit-plan
  ✅ Plan saved: .hermes/plans/add-oauth-login-20260516-143215.md
  ▶ Act Mode active — full toolset restored.
  📌 Seeded 7 todo step(s) from the plan.

[agent executes step 1, ticks the todo, step 2, ...]
```

## The plan artifact

Plans live at `.hermes/plans/<slug>-<YYYYmmdd-HHMMSS>.md` in the project
root.  The expected schema is fixed so a human skimming a stale plan
always finds Context first:

```markdown
# Plan: Add OAuth login to the dashboard

## Context
Why this task — link to issue, business need, etc.

## Investigation Notes
- src/auth/index.ts:42 — current auth flow uses local DB only
- web_search: oauth2-proxy v8.x supports our IdP
- 3 places in the dashboard route to /login

## Approach
Use oauth2-proxy in front of the dashboard. Considered:
- Building OAuth in-app — rejected, duplicate effort
- Auth0 — rejected, lock-in

## Files to Modify
- `docker-compose.yml`
- `src/auth/oauth.ts`
- `src/routes/login.tsx`
- `tests/auth/oauth.test.ts`

## Steps
1. [ ] Add oauth2-proxy to docker-compose with our IdP creds
2. [ ] Add `src/auth/oauth.ts` with the callback handler
3. [ ] Route /login through oauth-proxy
4. [ ] Add tests for the callback handler
5. [ ] Update README with the local-dev env vars

## Verification
- `docker compose up` and visit http://localhost:3000 — should redirect to IdP
- `pytest tests/auth/test_oauth.py` — should pass
- Manual: log out, log back in
```

Plans are plain markdown — you can hand-edit them between `/plan` and
`/exit-plan`.  When `/exit-plan` runs, the numbered Steps are parsed and
seeded into the agent's todo store so progress is visible as the
agent works through them.

## Configuration

Plan Mode's allowlist is fixed by default.  Operators who need to widen
or tighten it can do so in `~/.hermes/config.yaml`:

```yaml
plan_mode:
  # Add these tools to the allowlist (still read-only in spirit, e.g. a
  # custom static-analysis tool that ships in your team's plugins).
  allow_tools:
    - my_custom_lint_check

  # Remove these tools from the allowlist (even if Hermes considers
  # them read-only by default).
  deny_tools:
    - web_fetch
```

## What's blocked vs allowed

| Allowed                                    | Blocked                          |
|--------------------------------------------|----------------------------------|
| `read_file`, `list_directory`              | `write_file`, `patch`            |
| `search_files`                             | `terminal` (all commands)        |
| `web_search`, `web_fetch`                  | `send_message`, `discord_send`   |
| `session_search`, `session_insights`       | Browser actions that POST        |
| `skill_search`, `skill_browse`, `skill_view` | `image_generate`, `tts_speak`  |
| `memory` (`read` / `list` / `get` only)    | `memory` (`write` / `delete`)    |
| `todo`, `clarify`                          | `cronjob_create`, `kanban_create`|
|                                            | `skill_install`, `skill_delete`  |
|                                            | `delegate_task` (would spawn an unrestricted subagent) |

The refusal payload is shaped to be useful to the model rather than
opaque:

```json
{
  "error": "Refused: you are in Plan Mode and 'write_file' is not on the read-only allowlist. Finish writing your plan to .hermes/plans/x.md using read-only tools (read_file, search_files, web_search, session_search, todo, clarify, etc.) and stop. The user will switch to Act Mode after reviewing your plan."
}
```

## When Plan Mode shines

* **Unfamiliar codebases** — force the agent to read before writing.
  The Investigation Notes section is a record you can grep later.
* **High-blast-radius changes** — refactors, migrations, infra changes.
  You see the plan before any mutation lands.
* **Pairing with Verification** — Plan Mode + the [self-verification
  loop](./verification) gives you Plan → Act → Verify with rework.  The
  verifier reads the plan's Verification section and runs the
  commands listed there.

## When Plan Mode isn't worth it

* **Single-line fixes** — overhead of planning > work itself.
* **Exploratory chat** — Plan Mode forces the agent toward a concrete
  task.  If you just want to brainstorm, stay in normal mode.
