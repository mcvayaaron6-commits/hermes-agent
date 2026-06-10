# Ship Readiness — what's left to use the agent in production

**Status of the PR:** 39 commits, +13,500 LOC, ~400 new tests across the new modules, all green.

This document is the explicit answer to "what's needed to complete and start using the agent." It separates **DONE**, **DO BEFORE SHIPPING** (gates to release), and **NICE TO HAVE** (post-launch).

---

## ✅ DONE — what already works end-to-end

These have working code, unit tests, and at least one documented entry point. You can use them today.

| Capability | How |
|---|---|
| Plan / Act with persisted artifact | `/plan "<task>"` → `/exit-plan` |
| Verifier rework loop (interactive) | `verification.enabled: true` in config |
| Verifier rework loop (headless) | `hermes -z "<task>" --auto-rework --output-format json` |
| User-defined hooks (7 events) | `.hermes/hooks.json` + `/hooks trust` |
| Named subagent profiles | Drop `.md` into `.hermes/agents/` or `~/.hermes/agents/` |
| Bundled starter profiles | Auto-loaded — Explore, code-reviewer, test-writer |
| Tamper-evident audit log | `audit.enabled: true` + optional `HERMES_AUDIT_HMAC_KEY` |
| Lessons captured from rework cycles | Default-on when verification + plan are active |
| Auto-skill-promotion candidates | `/promotions` lists, `/promotions install <n>` adopts |
| Profile promotion from skill-usage | Wired in commit `7db52dd` — reads `tools/skill_usage.py` |
| Differential verification (quorum) | `verification.consensus.enabled: true` + `models:` list |
| Parallel DAG orchestration (model-callable) | `orchestrate_tasks` tool the agent can call |
| Parallel DAG orchestration (CLI) | `hermes orchestrate <file.yaml> [--dry-run]` |
| Parallel DAG orchestration (Python) | `examples/python_orchestration.py` |
| K8s liveness/readiness probes | `GET /health`, `GET /ready` on the dashboard |
| Hash-chained audit verification | `/audit verify` + programmatic `verify_chain()` |
| Bus events flow into audit log | Automatic when `audit.enabled: true` |

The end-to-end loop is functional:

```
/plan → investigate → /exit-plan → execute → Stop hook tests →
verifier (single OR quorum) → NEEDS_REWORK auto-queues fix → VERIFIED →
lesson captured → bus.publish(LessonCaptured) → audit chain extended →
≥3 lessons cluster → 🌱 nudge → /promotions install → skill in registry →
≥5 successful uses → profile promotion → durable specialist
```

---

## 🟠 DO BEFORE SHIPPING — hard gates to release

These block production deployment for most users. Estimated effort each.

### 1. `~/.hermes/config.yaml` defaults review (1 day)

Every new feature added a config block. Review and confirm:

- All defaults are **off** for opt-in features (verification, audit, consensus) — done
- All defaults are **sane** for always-on features (skill_promotion thresholds, plan_mode allowlist) — done
- The config schema documentation (`hermes config doc`) renders the new sections — **needs verifying**
- A `hermes config validate` exit code handles the new blocks — **needs verifying**

### 2. Operational runbook (2 days)

A real ops runbook is missing. Should cover:

- What to grep for in `agent.log` when verification keeps failing
- How to bisect a corrupted audit chain (`/audit verify` says broken at line N — what next)
- How to prune `~/.hermes/lessons/` when it bloats
- How to recover from `~/.hermes/checkpoints/` exceeding disk quota
- How to disable consensus mode mid-incident if the secondary model is down

### 3. Cost ceilings per session (3 days)

The autonomy loop is great for safety, but a runaway `--auto-rework` × 2 attempts × consensus 3-model could spend $5+ per turn on Opus-class models. Need:

- `verification.max_cost_usd` per attempt — currently uncapped
- `orchestrate_tasks.max_total_cost_usd` per DAG — currently uncapped
- Surface budget exhaustion as a structured tool error so the model can react

### 4. Cross-process bus + locks (1 week)

The bus is in-process. For multi-machine fleets — which is the natural next step once a single user wants 10 parallel agents — need:

- NATS or Redis adapter behind `Bus` protocol (no API change)
- Redis SETNX or filesystem flock + lease renewal behind `FileLockManager` protocol
- Distributed orchestrator coordinator (Raft-based or Postgres-advisory-lock)

The Python surfaces are already designed for this swap. Estimated 1 person-week per transport.

### 5. Authentication on the dashboard (3 days)

`/health` and `/ready` are public. Fine. But `GET /api/sessions`, `POST /api/config`, etc. need:

- Token-based auth header (configurable; refuses if `HERMES_DASHBOARD_TOKEN` unset)
- OPTIONS preflight handled for CORS
- Rate limiting per token

Currently anyone on the local network can edit your config via the dashboard.

### 6. Integration test for the full autonomy loop (3 days)

Unit tests cover every module. **No test verifies the full pipeline runs.** Need a `tests/e2e/test_superagent_loop.py` that:

1. Spins up a fixture git repo with a deliberately failing test
2. Mocks the LLM (recorded responses, not live)
3. Runs `/plan "fix the failing test"` → `/exit-plan`
4. Verifies: Stop hook runs, verifier reads output, rework fires, lesson captured, promotion candidate surfaces
5. Asserts audit chain verifies and contains all expected events

Without this, every refactor risks silent breakage across the loop.

---

## 🟢 NICE TO HAVE — post-launch differentiators

Ship the above, ship the agent. Add these to compound the moat further.

| Item | Effort | Why |
|---|---|---|
| Bus persistence + replay UI | 2 weeks | Time-travel debugging — replay an agent run against a new model. The bus events are already typed; just need a serialiser + scrubber UI. |
| React-Flow live topology view | 2 weeks | Subscribe a React component to the bus over WebSocket. Watch your DAG execute as a live graph. Screenshot-worthy onboarding moment. |
| Loom-for-agent-sessions | 2-3 weeks | `hermes session share <id>` → uploads sanitised JSON → returns URL that loads a React replay player. Network effects: engineering managers post-mortem failures by sharing links. |
| A2A protocol citizenship | 2-3 weeks | Hermes already speaks MCP, ACP, plus the new orchestrator. Adding Google's A2A makes it the only agent that's a citizen of all three ecosystems. |
| Property-based testing | 1 week | Hypothesis suite over the orchestrator DAG semantics — proves cycle detection, dependency ordering, and parallel-execution correctness against arbitrary graphs. |
| Distributed lessons corpus | 1 week | Mount `~/.hermes/lessons/` from a shared git repo or S3 bucket — team members' agents inherit each other's institutional knowledge. |
| `hermes superagent <task>` | 3 days | Single command that orchestrates Plan → DAG → Verify → Lessons. Demo-worthy "watch this one command do everything." |
| VSCode extension chat sidebar | 2-3 weeks | Hermes ships ACP server; build the matching VSCode extension that uses it. Direct competition with Cursor / Codeium / Copilot Chat. |
| Constitutional AI hook layer | 1 week | YAML constitution → built-in PreToolUse hook that gates every tool call. Sits naturally on top of the existing hook system. |
| Tool-call signing | 4 days | HMAC every tool call's args+result, same key as audit log. Cryptographic provenance for "what did this agent really do." |
| Multi-tenancy (org_id everywhere) | 6-10 weeks | The big enterprise gate — schema migration on hermes_state.py, context propagation, gateway adapter rewrites. Only attempt with a real customer driving it. |

---

## Suggested 30-day launch path

### Week 1 — gate work
1. Cost ceilings per session (Tier-1 #3)
2. Dashboard auth (Tier-1 #5)
3. Config schema validation tightening (Tier-1 #1)

### Week 2 — confidence work
4. End-to-end recorded-response integration test (Tier-1 #6)
5. Operational runbook (Tier-1 #2)
6. Property-based orchestrator tests (Nice-to-Have)

### Week 3 — demo work
7. `hermes superagent <task>` one-command synthesis (Nice-to-Have)
8. React-Flow live topology view (Nice-to-Have)
9. Loom-for-sessions MVP (Nice-to-Have)

### Week 4 — fleet work
10. NATS bus adapter (Tier-1 #4)
11. Redis lock adapter (Tier-1 #4)
12. Public launch + Show HN

---

## What this agent is now

A production-shape, single-machine autonomous agent with:

- Industry-leading feature parity (Claude Code Agent SDK + extras)
- A compounding-intelligence engine nobody else ships
- Parallel orchestration with file-lock arbitration
- Tamper-evident audit trail (cryptographic when keyed)
- Multi-model differential verification
- 400+ new unit tests, all green, zero regressions

**What it isn't yet:** a hosted multi-tenant SaaS. The single-user assumption is baked in everywhere from `~/.hermes/` to gateway routing. That's the founder review's Tier-1 enterprise gap — substantial work, only sensibly attempted with a real customer driving requirements.

**Recommendation:** ship single-user as an open-source flagship (after Tier-1 gates above), gather signal from real users, then build the multi-tenant SaaS layer with concrete customer feedback in hand.
