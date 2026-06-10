"""
``hermes superagent <task>`` — the one-command synthesis of the whole stack.

This is what makes the agent worth shipping: a single command that
exercises Plan Mode + parallel orchestration + verification + lessons
capture + promotion nudges + tamper-evident audit log + JSON-envelope
reporting.

Workflow
--------

1. Compose a *plan prompt* that asks the agent to produce a structured
   DAG of subtasks (read-only investigation, no mutations yet).
2. Run a planning oneshot.  The model returns YAML describing the DAG.
3. Validate the YAML; if invalid, surface the error.
4. Execute the DAG via the orchestrator (parallel, file-lock-arbitrated,
   verified).
5. Emit a JSON envelope reporting per-task results, total cost, total
   duration, lessons captured this run, and any promotion nudges that
   surfaced.

The whole stack from one shell command:

    $ hermes superagent "Add OAuth login to the dashboard"

In CI / cron:

    $ hermes superagent "Apply the @security-audit checklist to api/" \\
        --output-format json --auto-rework
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


PLAN_PROMPT_TEMPLATE = """\
You are the Superagent planner.  The user wants you to complete the
following task end-to-end:

    {task}

Produce a YAML pipeline of subtasks that the orchestrator will
execute in parallel.  Each subtask becomes one subagent invocation.

Rules:
* Decompose into 2-6 subtasks.  Independent subtasks run in parallel.
* Use `depends_on` to express ordering.  A subtask only starts when
  its deps SUCCEED.
* Declare `write_paths` for every file each subtask will modify; the
  orchestrator acquires exclusive locks so concurrent writers don't
  clobber.
* When relevant, use `subagent_type` to pick a specialist profile
  (e.g. `code-reviewer`, `test-writer`, `Explore`).
* Keep goals concrete and specific — each goal becomes a subagent's
  full instruction.

Respond with EXACTLY one YAML document.  No prose before or after.
No markdown code fences.

Example output shape:

    fanout: 4
    tasks:
      - id: server
        goal: |
          ...one or more sentences describing what this subtask does...
        subagent_type: code-reviewer
        write_paths:
          - api/server.py
      - id: tests
        goal: |
          ...
        subagent_type: test-writer
        depends_on: [server]
        write_paths:
          - tests/api/test_server.py
"""


_FENCE_BLOCK_RE = re.compile(
    r"```(?:yaml|yml)?\s*\n(?P<body>.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


def _strip_fences(text: str) -> str:
    """Extract YAML content even when the model wraps it in fences.

    Tolerant of:
    * Plain text with no fences (returned unchanged).
    * Single leading-and-trailing ``` fence (original behaviour).
    * Prose before AND/OR after a fenced block — pulls the body out
      via regex.  Common model failure mode is 'Here is the plan:\\n
      ```yaml ... ```\\nLet me know.' which the prior implementation
      mis-parsed as YAML.
    """
    stripped = text.strip()
    # Fast path — no fences anywhere.
    if "```" not in stripped:
        return stripped
    # Try regex extraction first (handles prose + fence + prose).
    match = _FENCE_BLOCK_RE.search(stripped)
    if match is not None:
        return match.group("body").strip()
    # Fall back to the simple-strip path for fence-only output.
    if stripped.startswith("```"):
        nl = stripped.find("\n")
        if nl > 0:
            stripped = stripped[nl + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def _parse_plan(text: str) -> Dict[str, Any]:
    """Parse the planner's YAML response into a dict.  Raises RuntimeError."""
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML required for superagent") from exc
    stripped = _strip_fences(text)
    try:
        data = yaml.safe_load(stripped) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(
            f"planner response was not valid YAML: {exc}\n\n"
            f"Raw response (first 500 chars):\n{stripped[:500]}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            "planner response did not parse to a mapping "
            f"(got {type(data).__name__})"
        )
    return data


def _collect_lessons_before(lessons_dir: Path) -> set[Path]:
    if not lessons_dir.is_dir():
        return set()
    try:
        return set(lessons_dir.glob("*.md"))
    except OSError:
        return set()


def _new_lessons_since(lessons_dir: Path, baseline: set[Path]) -> List[Path]:
    if not lessons_dir.is_dir():
        return []
    try:
        current = set(lessons_dir.glob("*.md"))
    except OSError:
        return []
    return sorted(current - baseline)


def _promotion_candidates_summary() -> List[Dict[str, Any]]:
    try:
        from agent import skill_promotion as _sp
        candidates = _sp.filter_already_installed(_sp.find_skill_candidates())
        return [
            {
                "name": c.name,
                "kind": c.kind,
                "score": round(c.score, 2),
                "tags": list(c.shared_tags),
            }
            for c in candidates[:10]
        ]
    except Exception:
        return []


def run_superagent(
    task: str,
    *,
    output_format: str = "text",
    dry_run: bool = False,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> int:
    """Top-level entry called from ``hermes superagent <task>``.  Returns exit code."""
    if not task or not task.strip():
        sys.stderr.write("hermes superagent: <task> is required\n")
        return 2

    # Snapshot the lessons corpus so we can report what's new at the end.
    try:
        from agent.lessons import _lessons_dir
        lessons_dir = _lessons_dir()
        lessons_baseline = _collect_lessons_before(lessons_dir)
    except Exception:
        lessons_dir = None
        lessons_baseline = set()

    overall_start = time.monotonic()

    # ---------- Step 1: planning oneshot ----------
    plan_prompt = PLAN_PROMPT_TEMPLATE.format(task=task.strip())
    plan_text = ""
    plan_cost = 0.0
    plan_tokens = 0
    plan_model = ""
    try:
        from hermes_cli.oneshot import _run_agent_with_details
        plan_response, plan_dict = _run_agent_with_details(
            plan_prompt, model=model, provider=provider,
        )
        plan_text = plan_response or ""
        if isinstance(plan_dict, dict):
            plan_cost = float(plan_dict.get("estimated_cost_usd") or 0.0)
            plan_tokens = int(plan_dict.get("total_tokens") or 0)
            plan_model = str(plan_dict.get("model") or "")
    except Exception as exc:
        sys.stderr.write(f"hermes superagent: planner failed — {exc}\n")
        return 2

    try:
        plan = _parse_plan(plan_text)
    except RuntimeError as exc:
        sys.stderr.write(f"hermes superagent: {exc}\n")
        return 2

    fanout = plan.get("fanout") or 4
    raw_tasks = plan.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        sys.stderr.write("hermes superagent: planner produced no tasks\n")
        return 2

    if dry_run:
        envelope = {
            "type": "superagent_dry_run",
            "task": task.strip(),
            "plan_cost_usd": round(plan_cost, 6),
            "plan_tokens": plan_tokens,
            "plan_model": plan_model,
            "fanout": fanout,
            "task_count": len(raw_tasks),
            "tasks": raw_tasks,
        }
        if output_format == "json":
            sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        else:
            print(f"🧭 Planner produced {len(raw_tasks)} task(s) "
                  f"(fanout={fanout}, planner-cost=${plan_cost:.4f}):")
            for t in raw_tasks:
                deps = (", ".join(t.get("depends_on") or [])) or "—"
                kind = t.get("subagent_type") or "default"
                print(f"  {t.get('id','?'):<24}  deps=[{deps}]  type={kind}")
        return 0

    # ---------- Step 2: orchestrate ----------
    from tools.orchestrate_tool import orchestrate_tasks
    # Build a parent agent that actually carries credentials.  The
    # prior implementation constructed AIAgent(model, provider,
    # quiet_mode=True) with no api_key/base_url/api_mode/credential_pool
    # — delegate_task then pulled effective_api_key=None and child
    # subagents failed to authenticate when creds lived in
    # config.yaml rather than env vars.  Mirror oneshot.py's
    # resolve_runtime_provider path so credentials thread through.
    try:
        from run_agent import AIAgent
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider
        cfg = load_config()
        model_cfg = (cfg.get("model") or {})
        effective_model = (model
                           or model_cfg.get("default")
                           or model_cfg.get("model"))
        effective_provider = provider or model_cfg.get("provider")
        runtime = resolve_runtime_provider(
            requested=effective_provider,
            target_model=effective_model or None,
        )
        parent = AIAgent(
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            api_mode=runtime.get("api_mode"),
            model=effective_model,
            credential_pool=runtime.get("credential_pool"),
            quiet_mode=True,
            platform="cli",
        )
    except Exception as exc:
        sys.stderr.write(
            f"hermes superagent: could not build parent agent — {exc}\n"
        )
        return 2

    raw_result = orchestrate_tasks(
        tasks=raw_tasks, fanout=fanout, parent_agent=parent,
    )
    try:
        orchestration = json.loads(raw_result)
    except (ValueError, TypeError):
        sys.stderr.write(
            "hermes superagent: orchestrator returned non-JSON result\n"
        )
        return 1

    overall_duration = time.monotonic() - overall_start

    # ---------- Step 3: collect outcomes ----------
    new_lessons = (_new_lessons_since(lessons_dir, lessons_baseline)
                   if lessons_dir is not None else [])
    promotion_candidates = _promotion_candidates_summary()

    # ---------- Step 4: report ----------
    envelope = {
        "type": "superagent_result",
        "task": task.strip(),
        "succeeded": bool(orchestration.get("succeeded")),
        "total_duration_seconds": round(overall_duration, 2),
        "planner": {
            "cost_usd": round(plan_cost, 6),
            "tokens": plan_tokens,
            "model": plan_model,
        },
        "orchestration": orchestration,
        "lessons_captured": [str(p) for p in new_lessons],
        "promotion_candidates": promotion_candidates,
    }

    if output_format == "json":
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False, default=str) + "\n")
    else:
        glyph = "✓" if envelope["succeeded"] else "✗"
        print(f"{glyph} Superagent — {len(orchestration.get('results') or [])} "
              f"task(s) in {overall_duration:.1f}s")
        print(f"  planner: ${plan_cost:.4f}, "
              f"{plan_tokens:,} tokens, model={plan_model}")
        print(f"  orchestrator: ${0.0:.4f} (per-task cost not yet aggregated)")
        for r in orchestration.get("results") or []:
            state = r.get("state", "?")
            print(f"    {r.get('task_id','?'):<24}  {state:<10}  "
                  f"{r.get('duration_seconds',0):5.1f}s")
        if new_lessons:
            print(f"  📚 {len(new_lessons)} new lesson(s) captured "
                  f"from this run.")
        if promotion_candidates:
            print(f"  🌱 {len(promotion_candidates)} promotion candidate(s) "
                  f"available — run /promotions to review.")

    return 0 if envelope["succeeded"] else 1
