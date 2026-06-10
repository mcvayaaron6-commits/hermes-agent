---
sidebar_position: 9
title: "Self-Verification"
description: "Close the autonomy loop — automatically check the agent actually finished what it claimed."
---

# Self-Verification

When the agent emits a final non-tool response, Hermes can spawn a
**verifier** subagent that reads the plan, the git diff, and any test
output, and emits a strict JSON verdict — `VERIFIED`, `NEEDS_REWORK`
with concrete issues, or `ERROR`.  If the verdict is `NEEDS_REWORK`,
the rework message gets surfaced so the caller (CLI, gateway, batch
runner) can re-enter the loop and let the agent fix the gaps before
the answer reaches the user.

It's the difference between "the model said done" and "the work is
actually done."  MetaGPT, LangGraph's reflective patterns, and the
OpenHands harness all ship variants of this; Hermes's flavor is
deliberately simple: one model call, JSON output, optional integration
with [Plan Mode](./plan-mode) and [Stop hooks](./hooks).

## How it fires

The verifier runs at the end of each `run_conversation()` call when
either of these is true:

1. `verification.enabled: true` is set in `~/.hermes/config.yaml`, OR
2. A [Plan Mode](./plan-mode) artifact exists for this session and
   `verification.auto_when_plan` is on (the default).

Rationale for the second case: if you typed `/plan`, you already opted
into "do this carefully" mode.  No need to flip a second switch to get
the verifier running too.

The pass is skipped when a [Stop hook](./hooks) blocked completion —
the operator's hook is the authority on done in that case.

## What the verifier sees

A user-role prompt with these sections:

* **Plan artifact** — the markdown plan, including the Verification
  section that names test/lint/build commands to run.
* **Primary agent's final response** — what the agent claimed it did.
* **Git diff since task start** — the actual code changes.  Best-effort:
  works in any git repo, silently absent elsewhere.
* **Test / lint / build output** — captured from any Stop hook that
  returned `additional_context` (e.g. an `npm test` hook that pipes its
  result back).
* **Recent conversation excerpt** — when provided.

And a strict system prompt that demands one of:

```json
{"status": "VERIFIED", "summary": "all checks pass"}
```

or:

```json
{
  "status": "NEEDS_REWORK",
  "summary": "OAuth callback returns 500 — missing redirect URI",
  "issues": [
    {
      "step": 3,
      "description": "src/auth/oauth.ts:42 — redirect URI hardcoded as localhost; will break in staging",
      "suggested_fix": "Read from OAUTH_REDIRECT env var with a localhost fallback",
      "severity": "high"
    }
  ]
}
```

The parser is forgiving — it tolerates code fences, surrounding prose,
case-insensitive status strings (`needs-rework` works), missing
severity (defaults to `medium`), and alternate field names (`issue` /
`fix` for older models).

## Composing with Stop hooks

The most powerful pattern is Plan Mode → Stop hook → Verifier:

`~/.hermes/hooks.json`:

```json
{
  "Stop": [
    {"command": "pytest -q --tb=line", "timeout": 120}
  ]
}
```

When the agent declares done, the Stop hook runs the test suite.  If
tests pass, the verifier sees the output as evidence.  If tests fail,
the verifier marks the work `NEEDS_REWORK` with the failing test names
as concrete issues and the parent loop fixes them.

## Configuration

```yaml
verification:
  enabled: false         # default off; auto-on with /plan
  auto_when_plan: true   # run verifier whenever a plan artifact exists
  max_attempts: 2        # rework cycles before giving up
  model: null            # null = use the agent's main model
  provider: null         # null = use the agent's main provider
  max_tokens: 2000       # verifier output budget
```

The verifier can use a cheaper model than the executor.  For an agent
running on Opus, routing `verification.model` to Sonnet or Haiku is a
cost win without losing much rigor — the verifier is grading concrete
evidence, not generating it.

## /verify

One-off:

```bash
> /verify
  needs rework: pytest reports 2 failures in tests/auth/test_oauth.py (2 issue(s))

Verification failed. Address the issues below and try again, then emit
a new final response.

**Summary:** pytest reports 2 failures in tests/auth/test_oauth.py

**Issues:**

1. [high] (step 3) tests/auth/test_oauth.py::test_callback_redirect — AssertionError: expected staging URL
   suggested fix: Read OAUTH_REDIRECT from env, fall back to localhost
2. [medium] (step 4) tests/auth/test_oauth.py::test_session_persists — sessions table missing
   suggested fix: Add the sessions table migration to step 4

Once you have fixed the issues, run the plan's Verification commands
yourself and confirm they pass before declaring the task complete.
```

## When to leave it off

* **Conversational sessions** — chitchat doesn't need verification.
* **Tightly-scoped one-liners** — the verifier itself costs more than the
  task.
* **Latency-sensitive flows** — verification adds one round-trip per
  rework cycle.
