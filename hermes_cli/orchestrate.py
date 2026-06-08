"""
``hermes orchestrate <file.yaml>`` — run a parallel DAG of agent tasks from
a checked-in YAML pipeline.

The orchestrator (``agent/orchestrator.py``) gives you parallel DAG execution
with file-lock arbitration.  This CLI subcommand exposes it without writing
Python — operators check in a YAML pipeline alongside their code and run it
from CI / cron / by hand.

YAML format
-----------

::

    fanout: 4          # optional, default 4, max 16

    tasks:
      - id: lint
        goal: Run ruff on src/
        subagent_type: Explore

      - id: tests
        goal: Run pytest on tests/
        subagent_type: test-writer
        depends_on: [lint]
        write_paths:
          - reports/junit.xml

      - id: docs
        goal: Update docs/api.md to reflect current src/api.py
        depends_on: [lint]
        write_paths:
          - docs/api.md

      - id: ship
        goal: Open a PR with the results
        depends_on: [tests, docs]

Output
------

Plain text by default (one line per task with state + duration), or
``--output-format json`` for the structured envelope CI scripts can
parse with ``jq``.

Exit codes
----------

* 0  → all tasks SUCCEEDED
* 1  → at least one task FAILED or SKIPPED
* 2  → DAG validation failed (cycle, missing dep, bad YAML)
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required for hermes orchestrate.  pip install pyyaml"
        ) from exc
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"could not read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{path}: top-level YAML must be a mapping, got "
            f"{type(data).__name__}"
        )
    return data


def _build_specs(raw_tasks: Any) -> List[Any]:
    """Convert YAML task dicts into TaskSpec objects.  Raises on bad input."""
    from agent.orchestrator import TaskSpec
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise RuntimeError("'tasks' must be a non-empty list")
    specs: List[TaskSpec] = []
    for i, t in enumerate(raw_tasks):
        if not isinstance(t, dict):
            raise RuntimeError(
                f"task {i} must be a mapping, got {type(t).__name__}"
            )
        tid = (t.get("id") or "").strip()
        if not tid:
            raise RuntimeError(f"task {i} is missing 'id'")
        goal = (t.get("goal") or "").strip()
        if not goal:
            raise RuntimeError(f"task {tid!r} is missing 'goal'")
        specs.append(TaskSpec(
            id=tid,
            goal=goal,
            subagent_type=t.get("subagent_type") or None,
            toolsets=list(t["toolsets"]) if t.get("toolsets") else None,
            depends_on=list(t.get("depends_on") or []),
            write_paths=list(t.get("write_paths") or []),
            max_iterations=t.get("max_iterations"),
            requires_upstream_success=bool(
                t.get("requires_upstream_success", True)
            ),
        ))
    return specs


def _build_parent_agent():
    """Build a minimal AIAgent so the orchestrator's default executor
    has someone to delegate from.  Uses ``oneshot``-style auto-resolution
    of provider/model from config.
    """
    # Import locally to keep CLI startup cheap.
    from hermes_cli.oneshot import _run_agent  # noqa: F401 — sanity
    from run_agent import AIAgent
    # We don't actually run a conversation here — we just need a
    # constructed AIAgent for delegate_task() to thread credentials
    # through.  The executor will call delegate_task itself.
    from hermes_cli.config import load_config
    cfg = load_config()
    model_cfg = (cfg.get("model") or {})
    return AIAgent(
        model=model_cfg.get("default") or model_cfg.get("model"),
        provider=model_cfg.get("provider"),
        quiet_mode=True,
    )


def run_orchestrate(
    yaml_path: Path,
    *,
    output_format: str = "text",
    dry_run: bool = False,
) -> int:
    """Top-level entry called by hermes_cli.main when the user types
    ``hermes orchestrate <file>``.  Returns an exit code.
    """
    try:
        config = _load_yaml(yaml_path)
        specs = _build_specs(config.get("tasks"))
    except RuntimeError as exc:
        sys.stderr.write(f"hermes orchestrate: {exc}\n")
        return 2

    fanout = config.get("fanout") or 4
    try:
        fanout = max(1, min(int(fanout), 16))
    except (TypeError, ValueError):
        fanout = 4

    if dry_run:
        # Validate-only: show the planned DAG without dispatching.
        from agent.orchestrator import validate_dag
        errors = validate_dag(specs)
        if errors:
            sys.stderr.write(f"hermes orchestrate: invalid DAG:\n")
            for e in errors:
                sys.stderr.write(f"  - {e}\n")
            return 2
        if output_format == "json":
            sys.stdout.write(json.dumps({
                "dry_run": True,
                "fanout": fanout,
                "task_count": len(specs),
                "tasks": [
                    {"id": s.id, "depends_on": s.depends_on,
                     "subagent_type": s.subagent_type}
                    for s in specs
                ],
            }) + "\n")
        else:
            print(f"✓ DAG valid — {len(specs)} task(s), fanout={fanout}")
            for s in specs:
                deps = (", ".join(s.depends_on) if s.depends_on else "—")
                kind = s.subagent_type or "default"
                print(f"  {s.id:<24}  deps=[{deps}]  type={kind}")
        return 0

    # Build parent agent + run.
    try:
        parent = _build_parent_agent()
    except Exception as exc:
        sys.stderr.write(
            f"hermes orchestrate: could not build parent agent — {exc}\n"
        )
        return 2

    from agent.orchestrator import Orchestrator, build_default_executor, TaskState
    orch = Orchestrator(
        build_default_executor(parent_agent=parent),
        fanout=fanout,
    )
    try:
        agg = orch.run(specs)
    except ValueError as exc:
        sys.stderr.write(f"hermes orchestrate: {exc}\n")
        return 2
    except Exception as exc:
        sys.stderr.write(f"hermes orchestrate: runtime failure — {exc}\n")
        return 1

    if output_format == "json":
        sys.stdout.write(json.dumps({
            "orchestrator_id": agg.orchestrator_id,
            "succeeded": agg.succeeded,
            "total_duration_seconds": round(agg.total_duration_seconds, 3),
            "total_iterations": agg.total_iterations,
            "results": [
                {
                    "task_id": tid,
                    "state": r.state.value,
                    "output": (r.output or "")[:8_000],
                    "error": r.error,
                    "duration_seconds": round(r.duration_seconds, 3),
                }
                for tid, r in agg.results.items()
            ],
        }) + "\n")
    else:
        status_glyph = "✓" if agg.succeeded else "✗"
        print(f"{status_glyph} {len(agg.results)} task(s) in "
              f"{agg.total_duration_seconds:.1f}s  "
              f"(fanout={fanout})")
        for tid, r in agg.results.items():
            # Structural pattern matching — Python 3.10+ idiom for
            # enum→display dispatch.  More readable than dict.get(default)
            # when the cases meaningfully differ.
            match r.state:
                case TaskState.SUCCEEDED: glyph = "✓"
                case TaskState.FAILED:    glyph = "✗"
                case TaskState.SKIPPED:   glyph = "↳"
                case _:                   glyph = "?"
            err = f"  — {r.error}" if r.error else ""
            print(f"  {glyph} {tid:<24}  {r.state.value:<10}  "
                  f"{r.duration_seconds:5.1f}s{err}")
    return 0 if agg.succeeded else 1
