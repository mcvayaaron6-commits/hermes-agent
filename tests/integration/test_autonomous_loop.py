"""End-to-end integration tests for the autonomous-agent loop.

These tests prove the engines (hooks, plan_mode, verification) actually
talk to the agent loop, using lightweight mocks for the LLM.  They sit
above the engine-level unit tests in tests/agent/ but below the full
LLM-driven smoke tests (which require credentials and are skipped in CI).

The mockable seam used here is ``AIAgent._call_verifier_llm``, which the
verification module routes through.  Replacing it with a recorded
response is enough to exercise the full Plan → Stop → Verify → Rework
plumbing without an API key.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Plan Mode gate — exercise the actual _invoke_tool path
# ---------------------------------------------------------------------------


class _StubAgent:
    """Minimal AIAgent stand-in for testing the _invoke_tool gate.

    We avoid spinning up a real AIAgent (which requires credentials,
    config, and ~30 imports) by stubbing the bits the gate touches.
    """

    def __init__(self, plan_mode):
        from agent.hooks import HookRegistry
        self._plan_mode = plan_mode
        self._hook_registry = HookRegistry.empty()
        self._hook_registry_loaded = True
        self._hook_load_report = None
        self.session_id = "test-session"

    def _fire_hook(self, event, **payload):
        from agent.hooks import HookOutcome
        return HookOutcome()

    def _get_hook_registry(self):
        return self._hook_registry


def _invoke(agent, name, args=None, dispatch=None):
    """Run the real _invoke_tool wrapper against a stub agent."""
    import run_agent
    if dispatch is not None:
        agent._invoke_tool_dispatch = dispatch
    else:
        def _default_dispatch(self, *a, **kw):
            return json.dumps({"ok": True, "tool": a[0]}, ensure_ascii=False)
        agent._invoke_tool_dispatch = _default_dispatch.__get__(agent)
    return run_agent.AIAgent._invoke_tool(
        agent, name, args or {}, "task-1",
    )


def test_plan_mode_gate_refuses_mutating_tool():
    from agent.plan_mode import PlanModeState
    plan_mode = PlanModeState()
    plan_mode.enter(task="x", plan_path=Path("/tmp/p.md"))
    agent = _StubAgent(plan_mode)
    result = _invoke(agent, "write_file", {"path": "x.txt", "content": "hi"})
    payload = json.loads(result)
    assert "error" in payload
    assert "Plan Mode" in payload["error"]
    assert "write_file" in payload["error"]


def test_plan_mode_gate_allows_readonly_tool():
    from agent.plan_mode import PlanModeState
    plan_mode = PlanModeState()
    plan_mode.enter(task="x", plan_path=Path("/tmp/p.md"))
    agent = _StubAgent(plan_mode)

    captured = {}

    def _dispatch(name, args, *a, **kw):
        captured["name"] = name
        return json.dumps({"ok": True, "tool": name}, ensure_ascii=False)

    result = _invoke(agent, "read_file", {"path": "x.txt"}, dispatch=_dispatch)
    assert captured["name"] == "read_file"
    payload = json.loads(result)
    assert payload["ok"] is True


def test_plan_mode_disabled_is_pass_through():
    from agent.plan_mode import PlanModeState
    plan_mode = PlanModeState()  # not entered
    agent = _StubAgent(plan_mode)
    result = _invoke(agent, "write_file", {"path": "x.txt", "content": "hi"})
    payload = json.loads(result)
    assert payload.get("ok") is True


# ---------------------------------------------------------------------------
# PreToolUse / PostToolUse hooks — exercise the real wrapper
# ---------------------------------------------------------------------------


class _HookedAgent(_StubAgent):
    """Stub agent with a programmable hook registry."""

    def __init__(self, plan_mode, hook_outcomes):
        super().__init__(plan_mode)
        self._hook_outcomes = hook_outcomes

    def _fire_hook(self, event, **payload):
        from agent.hooks import HookOutcome
        return self._hook_outcomes.get(event, HookOutcome())


def test_pretooluse_block_short_circuits():
    from agent.hooks import HookOutcome
    from agent.plan_mode import PlanModeState
    outcomes = {
        "PreToolUse": HookOutcome(blocked=True, block_reason="secret detected"),
    }
    agent = _HookedAgent(PlanModeState(), outcomes)
    result = _invoke(agent, "write_file", {"path": "x.env", "content": "API_KEY=..."})
    payload = json.loads(result)
    assert "error" in payload
    assert "secret detected" in payload["error"]


def test_pretooluse_transform_replaces_args():
    from agent.hooks import HookOutcome
    from agent.plan_mode import PlanModeState
    outcomes = {
        "PreToolUse": HookOutcome(transformed_args={"path": "rewritten.txt"}),
    }
    agent = _HookedAgent(PlanModeState(), outcomes)
    captured = {}

    def _dispatch(name, args, *a, **kw):
        captured["args"] = args
        return json.dumps({"ok": True}, ensure_ascii=False)

    _invoke(agent, "write_file", {"path": "orig.txt"}, dispatch=_dispatch)
    assert captured["args"] == {"path": "rewritten.txt"}


def test_posttooluse_errors_dont_change_result():
    from agent.hooks import HookOutcome
    from agent.plan_mode import PlanModeState
    outcomes = {
        "PostToolUse": HookOutcome(errors=["hook foo exited 3"]),
    }
    agent = _HookedAgent(PlanModeState(), outcomes)

    def _dispatch(name, args, *a, **kw):
        return json.dumps({"ok": True, "value": 42}, ensure_ascii=False)

    result = _invoke(agent, "read_file", {"path": "x"}, dispatch=_dispatch)
    payload = json.loads(result)
    assert payload == {"ok": True, "value": 42}


# ---------------------------------------------------------------------------
# Verifier dispatch — exercise the real _run_verifier via the mockable seam
# ---------------------------------------------------------------------------


class _VerifierAgent:
    """Tiny stand-in to exercise _run_verifier without spinning AIAgent."""

    def __init__(self, *, plan_mode=None, verifier_response="", config=None):
        self._plan_mode = plan_mode
        self.model = "stub-model"
        self.provider = "stub-provider"
        self.session_id = "test-session"
        self._verifier_response = verifier_response
        self._captured_messages = None
        self._captured_model = None
        self._captured_max_tokens = None
        self._config = config or {
            "enabled": True, "auto_when_plan": True,
            "max_attempts": 2, "model": None,
            "provider": None, "max_tokens": 2000,
        }

    def _verification_config(self):
        return self._config

    def _capture_git_diff(self):
        return None

    def _call_verifier_llm(self, messages, *, model, provider, max_tokens):
        self._captured_messages = messages
        self._captured_model = model
        self._captured_max_tokens = max_tokens
        return self._verifier_response


def _run_verifier(agent, **kwargs):
    import run_agent
    return run_agent.AIAgent._run_verifier(agent, **kwargs)


def test_run_verifier_disabled_returns_none():
    cfg = {"enabled": False, "auto_when_plan": False,
           "max_attempts": 2, "model": None, "provider": None, "max_tokens": 2000}
    agent = _VerifierAgent(config=cfg, verifier_response="should not be called")
    assert _run_verifier(agent, final_response="done") is None
    assert agent._captured_messages is None


def test_run_verifier_auto_enables_with_plan(tmp_path):
    from agent.plan_mode import PlanModeState
    plan_mode = PlanModeState()
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan: x\n## Verification\nrun pytest\n",
                         encoding="utf-8")
    plan_mode.enter(task="x", plan_path=plan_file)
    cfg = {"enabled": False, "auto_when_plan": True,
           "max_attempts": 2, "model": None, "provider": None, "max_tokens": 2000}
    agent = _VerifierAgent(
        plan_mode=plan_mode, config=cfg,
        verifier_response='{"status": "VERIFIED", "summary": "ok"}',
    )
    report = _run_verifier(agent, final_response="done")
    assert report is not None
    assert report.verified


def test_run_verifier_parses_needs_rework():
    cfg = {"enabled": True, "auto_when_plan": True,
           "max_attempts": 2, "model": None, "provider": None, "max_tokens": 2000}
    agent = _VerifierAgent(
        config=cfg,
        verifier_response=json.dumps({
            "status": "NEEDS_REWORK",
            "summary": "tests fail",
            "issues": [
                {"step": 3, "description": "test_oauth fails",
                 "suggested_fix": "fix redirect URI", "severity": "high"},
            ],
        }),
    )
    report = _run_verifier(agent, final_response="all done")
    assert report is not None
    assert report.needs_rework
    assert len(report.issues) == 1
    assert report.issues[0].step == 3


def test_run_verifier_handles_dispatch_failure():
    cfg = {"enabled": True, "auto_when_plan": True,
           "max_attempts": 2, "model": None, "provider": None, "max_tokens": 2000}
    agent = _VerifierAgent(config=cfg, verifier_response="")

    def _raise(*args, **kwargs):
        raise RuntimeError("backend down")

    agent._call_verifier_llm = _raise
    report = _run_verifier(agent, final_response="done")
    assert report is not None
    assert report.is_error
    assert "backend down" in report.summary


def test_run_verifier_passes_plan_text_when_present(tmp_path):
    from agent.plan_mode import PlanModeState
    plan_mode = PlanModeState()
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan: x\n## Steps\n1. [ ] do thing\n",
                         encoding="utf-8")
    plan_mode.enter(task="x", plan_path=plan_file)
    cfg = {"enabled": True, "auto_when_plan": True,
           "max_attempts": 2, "model": None, "provider": None, "max_tokens": 2000}
    agent = _VerifierAgent(
        plan_mode=plan_mode, config=cfg,
        verifier_response='{"status": "VERIFIED", "summary": "ok"}',
    )
    _run_verifier(agent, final_response="done")
    user_msg = agent._captured_messages[1]["content"]
    assert "# Plan: x" in user_msg
    assert "do thing" in user_msg


def test_run_verifier_uses_test_output_when_provided():
    cfg = {"enabled": True, "auto_when_plan": True,
           "max_attempts": 2, "model": None, "provider": None, "max_tokens": 2000}
    agent = _VerifierAgent(
        config=cfg,
        verifier_response='{"status": "VERIFIED", "summary": "ok"}',
    )
    _run_verifier(agent, final_response="done", test_output="3 failed, 5 passed")
    user_msg = agent._captured_messages[1]["content"]
    assert "3 failed" in user_msg


def test_run_verifier_routes_model_override():
    cfg = {"enabled": True, "auto_when_plan": True,
           "max_attempts": 2, "model": "haiku-overridden",
           "provider": "anthropic", "max_tokens": 1500}
    agent = _VerifierAgent(
        config=cfg,
        verifier_response='{"status": "VERIFIED", "summary": "ok"}',
    )
    _run_verifier(agent, final_response="done")
    assert agent._captured_model == "haiku-overridden"
    assert agent._captured_max_tokens == 1500
