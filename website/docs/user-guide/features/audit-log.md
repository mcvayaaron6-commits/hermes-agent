---
sidebar_position: 11
title: "Audit Log"
description: "Structured JSONL trace of every hook fire, plan-mode transition, verifier verdict, and rework injection — for observability, debugging, and post-mortem."
---

# Audit Log

When `audit.enabled: true` is set in `~/.hermes/config.yaml`, Hermes writes one structured JSON line to `~/.hermes/logs/audit.jsonl` for every significant lifecycle event. Operators get a stable, grep/jq/log-aggregator-friendly trace of what the agent's hooks, plan mode, and verifier are actually doing — without parsing the free-form `agent.log`.

The format is a hard contract: scripted consumers can rely on the keys.

## Schema

```json
{
  "ts": "2026-05-21T14:32:15.123456+00:00",
  "session_id": "sess_abc123",
  "event": "PreToolUse",
  "agent": "primary",
  "hermes_version": "0.13.0",
  "data": {
    "tool": "write_file",
    "args": {"path": "src/auth/oauth.ts"}
  }
}
```

Every line is a complete JSON object on its own — no multi-line stack traces, no continuation lines. `jq -c` works out of the box.

## Events recorded

| Event | When it fires | `data` fields |
|---|---|---|
| `SessionStart` | First `run_conversation` call per agent instance | (empty) |
| `UserPromptSubmit` | Before user message is appended to transcript | `user_message` |
| `PreToolUse` | Before each tool call | `tool`, `args` |
| `PostToolUse` | After each tool result | `tool`, `args`, `result` (truncated to 16 KB) |
| `Stop` | When a non-tool final response is emitted | `final_response` |
| `SubagentStop` | When a delegated subagent finishes | `agent_name`, `final_response` |
| `SessionEnd` | When the agent's `close()` runs | (empty) |
| `Verification` | After each verifier pass | `status`, `summary`, `issue_count`, `attempts` |
| `PlanModeEnter` | `/plan` slash command | `task`, `plan_path` |
| `PlanModeExit` | `/exit-plan` slash command | `task`, `plan_path`, `steps_seeded` |
| `ReworkInjected` | CLI auto-queues a verifier rework message | `attempt`, `max_attempts`, `reason` |

The schema is **append-only across releases** — new event types and new keys inside `data` can land, but existing ones won't silently rename or disappear. Scripted consumers can rely on this.

## Configuration

```yaml
audit:
  enabled: false              # master switch — off by default
  path: null                  # default: <hermes_home>/logs/audit.jsonl
```

Operators can flip `enabled` mid-session — subsequent writes will (start/stop) without restart. The path override is useful when shipping logs to a different mount (`/var/log/hermes/...`) or sending to a fluentbit-style collector that tails a known file.

There is no rotation, no compression, no async — audit lines are one-shot appends. A busy session writes maybe a few hundred lines. Operators that need rotation can pipe `audit.jsonl` through `logrotate` or move the file periodically (the next write recreates it).

## In-session inspection: `/audit`

```bash
/audit                # tail last 20 events, one line each
/audit tail 50        # tail last 50
/audit summary        # event-type histogram with ASCII bars
/audit path           # show the resolved log path + enabled state
```

Example:

```text
> /audit summary
  📊 142 audit event(s):
    PreToolUse           54  ██████████████████████████████████████████
    PostToolUse          51  ███████████████████████████████████████
    UserPromptSubmit     18  ██████████████
    Verification          9  ███████
    Stop                  9  ███████
    PlanModeEnter         1  █
```

## Programmatic access

The module exports two helpers for tools that need to consume the log:

```python
from agent.audit_log import tail_events, count_events_by_type

# Read the last 100 events
events = tail_events(n=100)

# Aggregate
counts = count_events_by_type()
```

Both gracefully handle a missing log file and malformed lines (skipped, not raised).

## What audit log is good for

- **Post-mortem debugging.** "Why did the agent suddenly call `terminal: rm -rf x` last Tuesday?" → grep for the `PreToolUse` event around that timestamp.
- **Cost attribution.** Count `Verification` events × verifier model rates.
- **CI observability.** `--output-format json` exposes the final envelope to CI; audit log exposes everything in between for forensics on failure.
- **Hook authoring.** When writing a `PreToolUse` hook, watch `audit.jsonl` to see exactly the payload your hook receives.
- **Safety review.** Audit `Stop` and `Verification` outcomes across a fleet of agent instances to confirm the verifier is actually catching regressions.

## What audit log is NOT

- **Not for replay.** The `data` field is truncated at 16 KB per string — full tool arguments may be cut. For replay-quality records, use trajectory export (`trajectories/`).
- **Not durable across `hermes config edit`.** Path is resolved at first write per session; if you change the path mid-session, the resolution caches for the rest of that session.
- **Not redacted by default.** Sensitive tool arguments (secrets, tokens) appear verbatim unless you've wired the redactor through. Future work: auto-apply the existing `agent/redact.py` redactor when `security.redact_secrets` is on.

## When to leave it off

- Personal use, low-volume — `agent.log` is enough.
- Disk-constrained deployments — set a small `audit.path` rotation policy first.
- Sensitive contexts where un-redacted tool arguments shouldn't be persisted.
