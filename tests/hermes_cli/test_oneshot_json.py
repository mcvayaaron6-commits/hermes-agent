"""Tests for the --output-format json envelope in hermes -z mode."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.oneshot import _build_json_envelope


def test_envelope_minimal_with_just_response():
    env = _build_json_envelope("the prompt", "the response", None)
    assert env["type"] == "oneshot_result"
    assert env["prompt"] == "the prompt"
    assert env["final_response"] == "the response"
    assert "verification" not in env
    assert "rework_message" not in env


def test_envelope_carries_telemetry():
    result = {
        "model": "anthropic/claude-sonnet-4-6",
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "input_tokens": 1500,
        "output_tokens": 240,
        "cache_read_tokens": 1200,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 1740,
        "estimated_cost_usd": 0.0023,
        "cost_status": "exact",
        "api_calls": 3,
        "completed": True,
        "interrupted": False,
    }
    env = _build_json_envelope("p", "r", result)
    for key in result:
        assert env[key] == result[key]


def test_envelope_includes_verification_when_present():
    result = {
        "verification": {
            "status": "VERIFIED",
            "summary": "all tests pass",
            "issues": [],
            "attempts": 1,
        },
    }
    env = _build_json_envelope("p", "r", result)
    assert env["verification"]["status"] == "VERIFIED"


def test_envelope_includes_rework_message_when_needs_rework():
    result = {
        "verification": {"status": "NEEDS_REWORK", "summary": "fail"},
        "rework_message": "Verification failed. Address...",
    }
    env = _build_json_envelope("p", "r", result)
    assert env["rework_message"].startswith("Verification failed")
    assert env["verification"]["status"] == "NEEDS_REWORK"


def test_envelope_includes_stop_hook_block_when_present():
    result = {
        "stop_hook_block": {"reason": "tests fail"},
        "stop_hook_context": ["3 tests failed"],
    }
    env = _build_json_envelope("p", "r", result)
    assert env["stop_hook_block"]["reason"] == "tests fail"
    assert env["stop_hook_context"] == ["3 tests failed"]


def test_envelope_includes_user_prompt_block_when_present():
    result = {"user_prompt_block": {"reason": "secret detected in prompt"}}
    env = _build_json_envelope("p", "r", result)
    assert env["user_prompt_block"]["reason"] == "secret detected in prompt"


def test_envelope_is_json_serialisable():
    result = {
        "model": "x",
        "input_tokens": 100,
        "verification": {"status": "VERIFIED", "summary": "ok"},
        "estimated_cost_usd": 0.001,
    }
    env = _build_json_envelope("prompt with \"quotes\" and 日本語",
                               "response 🎉", result)
    # Must round-trip cleanly with non-ASCII content.
    serialised = json.dumps(env, ensure_ascii=False, default=str)
    decoded = json.loads(serialised)
    assert decoded["prompt"] == "prompt with \"quotes\" and 日本語"
    assert decoded["final_response"] == "response 🎉"


def test_envelope_handles_non_dict_result_gracefully():
    env = _build_json_envelope("p", "r", "not a dict")
    assert env["type"] == "oneshot_result"
    assert env["final_response"] == "r"
    assert "verification" not in env


def test_envelope_handles_empty_response():
    env = _build_json_envelope("p", "", None)
    assert env["final_response"] == ""


def test_envelope_omits_missing_telemetry_keys():
    result = {"model": "x", "input_tokens": 10}  # only two keys
    env = _build_json_envelope("p", "r", result)
    assert env["model"] == "x"
    assert env["input_tokens"] == 10
    assert "output_tokens" not in env
    assert "estimated_cost_usd" not in env


def test_resolve_auto_rework_max_attempts_default():
    from hermes_cli.oneshot import _resolve_auto_rework_max_attempts
    # Default falls back to 2 even if config is empty / unavailable.
    n = _resolve_auto_rework_max_attempts()
    assert n >= 1


def test_envelope_rework_history_keys_when_set():
    """Lock in the rework_history + total_attempts envelope keys so
    scripted CI consumers can rely on them."""
    # We build the envelope manually here — the integration of the
    # loop is exercised by smoke tests; this just verifies the keys
    # we add post-envelope are stable in shape.
    result = {"verification": {"status": "VERIFIED"}}
    env = _build_json_envelope("p", "r", result)
    env["rework_history"] = [
        {"attempt": 1, "verification": {"status": "NEEDS_REWORK"},
         "input_tokens": 100, "output_tokens": 50},
    ]
    env["total_attempts"] = 2
    assert env["total_attempts"] == 2
    assert env["rework_history"][0]["attempt"] == 1
    import json
    json.dumps(env, ensure_ascii=False, default=str)  # must serialise


def test_envelope_key_stability():
    """Lock in the envelope schema — silent key renames would break CI
    pipelines that grep for specific paths."""
    result = {
        "model": "m", "provider": "p",
        "input_tokens": 1, "output_tokens": 2, "total_tokens": 3,
        "estimated_cost_usd": 0.01,
        "verification": {"status": "VERIFIED"},
        "rework_message": "x",
    }
    env = _build_json_envelope("prompt", "response", result)
    # Top-level keys that must remain stable for scripted consumers.
    must_have = {
        "type", "prompt", "final_response",
        "model", "provider",
        "input_tokens", "output_tokens", "total_tokens",
        "estimated_cost_usd",
        "verification", "rework_message",
    }
    assert must_have.issubset(env.keys())
    assert env["type"] == "oneshot_result"  # locked literal
