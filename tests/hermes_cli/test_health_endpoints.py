"""Focused tests for the new /health and /ready Kubernetes probes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# We can't import the FastAPI app + use TestClient directly because of
# the baseline starlette testclient bug.  Instead we call the route
# handlers as plain async functions — they're decorated with @app.get
# but the underlying coroutine is still callable.


@pytest.fixture
def mocked_home(tmp_path, monkeypatch):
    """Redirect get_hermes_home so probes don't touch the real ~/.hermes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    return tmp_path / "home"


@pytest.mark.asyncio
async def test_health_returns_ok(mocked_home):
    from hermes_cli.web_server import get_health
    result = await get_health()
    assert result["status"] == "ok"
    assert result["service"] == "hermes-web"
    assert "version" in result


@pytest.mark.asyncio
async def test_ready_returns_ready_when_components_ok(mocked_home, monkeypatch):
    from hermes_cli.web_server import get_ready
    # Mock get_running_pid to return None (no gateway) — should still
    # report ready since gateway is non-critical.
    monkeypatch.setattr("hermes_cli.web_server.get_running_pid", lambda: None)
    result = await get_ready()
    # Result can be a dict (ready) or a JSONResponse (not_ready).
    if hasattr(result, "body"):
        body = json.loads(result.body)
    else:
        body = result
    assert body["status"] == "ready"
    components = body["components"]
    assert components["config"]["ok"]
    assert components["storage"]["ok"]


@pytest.mark.asyncio
async def test_ready_includes_audit_status_when_disabled(mocked_home, monkeypatch):
    from hermes_cli.web_server import get_ready
    monkeypatch.setattr("hermes_cli.web_server.get_running_pid", lambda: None)
    monkeypatch.setattr("agent.audit_log._audit_config", lambda: {"enabled": False})
    result = await get_ready()
    body = result if isinstance(result, dict) else json.loads(result.body)
    assert body["components"]["audit_log"]["enabled"] is False


@pytest.mark.asyncio
async def test_ready_503_when_storage_unwritable(monkeypatch, tmp_path):
    """If the hermes_home is unwritable, /ready returns 503."""
    from hermes_cli.web_server import get_ready
    # Point HERMES_HOME at a path we can't write to (a file, not dir).
    bad = tmp_path / "not-a-dir"
    bad.touch()  # exists as a regular file — can't be used as dir
    monkeypatch.setenv("HERMES_HOME", str(bad))
    monkeypatch.setattr("hermes_cli.web_server.get_running_pid", lambda: None)
    result = await get_ready()
    if hasattr(result, "status_code"):
        assert result.status_code == 503
        body = json.loads(result.body)
        assert body["status"] == "not_ready"
        assert body["components"]["storage"]["ok"] is False
    else:
        # Some FastAPI versions may auto-wrap differently; sanity check.
        assert result.get("status") == "not_ready"


@pytest.mark.asyncio
async def test_ready_reports_gateway_running_state(mocked_home, monkeypatch):
    from hermes_cli.web_server import get_ready
    monkeypatch.setattr("hermes_cli.web_server.get_running_pid", lambda: 12345)
    result = await get_ready()
    body = result if isinstance(result, dict) else json.loads(result.body)
    assert body["components"]["gateway"]["running"] is True
    assert body["components"]["gateway"]["pid"] == 12345


@pytest.mark.asyncio
async def test_ready_schema_keys_stable(mocked_home, monkeypatch):
    """Lock in the response shape so K8s YAMLs / monitoring don't break."""
    from hermes_cli.web_server import get_ready
    monkeypatch.setattr("hermes_cli.web_server.get_running_pid", lambda: None)
    result = await get_ready()
    body = result if isinstance(result, dict) else json.loads(result.body)
    # Top-level keys
    assert {"status", "service", "version", "components"}.issubset(body.keys())
    # Components shape
    assert "config" in body["components"]
    assert "storage" in body["components"]
    assert "audit_log" in body["components"]
    assert "gateway" in body["components"]


@pytest.mark.asyncio
async def test_health_independent_of_ready_failure(mocked_home, monkeypatch, tmp_path):
    """/health stays green even when /ready would be red (storage broken)."""
    from hermes_cli.web_server import get_health, get_ready
    bad = tmp_path / "not-a-dir"
    bad.touch()
    monkeypatch.setenv("HERMES_HOME", str(bad))
    monkeypatch.setattr("hermes_cli.web_server.get_running_pid", lambda: None)
    health = await get_health()
    assert health["status"] == "ok"
    # /ready should be 503 in this scenario; /health stays 200.
    ready = await get_ready()
    if hasattr(ready, "status_code"):
        assert ready.status_code == 503
