---
sidebar_position: 2
title: "Superagent Quick Start"
description: "Five minutes to your first parallel autonomous agent run with verification, lessons, and compounding intelligence."
---

# Superagent Quick Start

Five minutes to your first end-to-end autonomous agent run with everything turned on — parallel orchestration, verifier-driven rework, lessons learned, auto-skill promotion, and tamper-evident audit log.

## Prerequisites

- Hermes installed (`hermes --version` works)
- A provider configured (`hermes model` finishes)
- A `git` working directory (any will do — `cd` into a real repo for the worked example)

## Step 1 — Turn on the moat features

Edit `~/.hermes/config.yaml` (or run `hermes config edit`) and set:

```yaml
verification:
  enabled: true        # required for the autonomy loop to gate on tests
  auto_when_plan: true # default — verifier auto-runs when a plan exists
  max_attempts: 2      # cap on rework cycles per turn

audit:
  enabled: true        # tamper-evident JSONL of every event
  # Optional: set hmac_key_file or HERMES_AUDIT_HMAC_KEY env var for
  # cryptographic signing.

skill_promotion:
  lesson_threshold: 3
  min_shared_tags: 2
  usage_threshold: 5
```

That's it for config. Everything else has sensible defaults.

## Step 2 — Optional: set up a Stop hook

A Stop hook runs whenever the agent declares "done" — perfect for running your test suite as gating evidence. Drop this in `.hermes/hooks.json` at the root of any repo:

```json
{
  "Stop": [
    { "command": "pytest -q --tb=line", "timeout": 120 }
  ]
}
```

The first time Hermes encounters the file, it'll ask you to trust it (`/hooks trust`). The output of `pytest` flows directly into the verifier as evidence — that's what makes "tests failed → agent fixes → tests pass" auto-loop.

## Step 3 — Run an interactive autonomous task

```bash
$ hermes
> /plan "Add a /v2/users endpoint to the dashboard API"
  📋 Plan Mode active — read-only tools only.
  Task: Add a /v2/users endpoint to the dashboard API
  Plan will be saved to: .hermes/plans/add-a-v2-users-endpoint-...md
```

The agent investigates with read-only tools, writes a plan to disk.

```bash
> /exit-plan
  ✅ Plan saved
  ▶ Act Mode active — full toolset restored.
  📌 Seeded 6 todo step(s) from the plan.
```

Agent executes. Stop hook runs tests. Verifier reads diff + test output. If `NEEDS_REWORK`, CLI auto-queues a fix turn. Cycles capped at `verification.max_attempts`.

After convergence on `VERIFIED`, if this was the third lesson in a cluster sharing two tags, you'll see:

```text
🌱 New skill candidate ready: `learned-api-v2` (score=4.0, tags=api,v2)
   Run /promotions to review, /promotions install learned-api-v2 to adopt.
```

That's the moat — the agent literally just proposed a procedural memory built from your last three rework cycles.

## Step 4 — Run a parallel DAG (the orchestrator)

```bash
$ hermes orchestrate examples/parallel_pipeline.yaml --dry-run
✓ DAG valid — 4 task(s), fanout=4
  server                    deps=[—]          type=code-reviewer
  schema                    deps=[—]          type=default
  tests                     deps=[server, schema]  type=test-writer
  docs                      deps=[server]     type=default
```

Once you're satisfied with the plan, drop `--dry-run` to actually run it. The orchestrator dispatches independent tasks in parallel, file-lock arbitration handles shared write paths, and verifier wraps each task individually.

## Step 5 — Headless / CI mode

```bash
$ hermes -z "fix the failing OAuth integration test" \
    --output-format json \
    --auto-rework

{
  "type": "oneshot_result",
  "final_response": "Fixed by reading OAUTH_REDIRECT from env...",
  "verification": {"status": "VERIFIED", "summary": "all 3 tests pass"},
  "total_attempts": 2,
  "rework_history": [{"attempt": 1, ...}],
  "estimated_cost_usd": 0.0087,
  "completed": true
}
```

Exit code is **0** when verified, **1** when rework attempts exhaust, **2** on DAG validation failure (for `hermes orchestrate`). Plug into any CI gate.

## Step 6 — Inspect what happened

```bash
> /audit summary
  📊 142 audit event(s):
    PreToolUse           54  █████████████...
    PostToolUse          51  ████████████...
    Verification          9  ██████
    ReworkInjected        3  ██
    PlanModeEnter         1  █

> /audit verify
  ✅ Chain intact — 142/142 line(s) verified.
  🔐 HMAC: 142 signed, 0 unsigned.

> /lessons count
  📚 12 lesson(s) in /home/you/.hermes/lessons

> /promotions
  📦 2 promotion candidate(s):
    learned-oauth-auth          score=4.5  tags=oauth,auth
    learned-database-migration  score=3.5  tags=database,migration
```

## The full feature map

| Feature | How to turn on | Slash / CLI |
|---|---|---|
| Hooks | `.hermes/hooks.json` + `/hooks trust` | `/hooks`, `/hooks reload` |
| Plan mode | `/plan` | `/plan`, `/exit-plan`, `/cancel-plan`, `/plan-show` |
| Verification | `verification.enabled: true` | `/verify` |
| Differential verification | `verification.consensus.enabled: true` | _automatic_ |
| Self-rework loop | Default-on when verification is on | _automatic_ |
| Subagent profiles | Drop `.md` files in `.hermes/agents/` or `~/.hermes/agents/` | `/subagents` |
| Audit log | `audit.enabled: true` | `/audit tail|summary|path|verify` |
| Lessons | Default-on when verification is on | `/lessons`, `/lessons <q>` |
| Skill promotion | Default-on when verification is on | `/promotions` |
| Parallel orchestration | _Always available_ | `hermes orchestrate <file>`, the `orchestrate_tasks` tool |
| Headless JSON | `hermes -z --output-format json` | _CLI flag_ |

## When to use which

- **Plan mode** for any task that touches >1 file or has high blast radius.
- **`hermes orchestrate`** when the work splits into parallel branches (build/test/docs).
- **`hermes -z --auto-rework`** in CI / scripts / cron.
- **Differential verification** when stakes are high (production migrations, security-sensitive changes).
- **Skill promotion** is always-on; just trust the nudges.

## Programmatic API

Everything also has a clean Python surface — see `examples/python_orchestration.py` for a runnable script that subscribes to the bus, runs a DAG with a custom executor, and reports the speedup.

## Troubleshooting

- **"verification keeps rejecting forever"** — Drop `max_attempts` to 1 or run `/verify` to see the verifier's reasoning. The verifier is intentionally strict; for low-stakes tasks, turn `verification.enabled: false`.
- **"too many promotion nudges"** — Raise `skill_promotion.min_shared_tags` to 3 or `lesson_threshold` to 5.
- **"DAG validation failed"** — Run `hermes orchestrate <file> --dry-run` for a cycle / missing-dep diagnostic.
- **"Stop hook fires too slowly"** — Set a higher `timeout` per hook entry; default is 30s.

## Next steps

- [Plan Mode user guide](../user-guide/features/plan-mode.md)
- [Self-Verification](../user-guide/features/verification.md)
- [Parallel Orchestration](../user-guide/features/parallel-orchestration.md)
- [Auto-Skill Promotion (the moat)](../user-guide/features/skill-promotion.md)
- [Audit Log](../user-guide/features/audit-log.md)
- [Hooks](../user-guide/features/hooks.md)
