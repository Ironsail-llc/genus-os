"""Three POST routes answered 422 to every caller, for a whole year of commits.

`Request` is imported INSIDE `create_health_app`. With
`from __future__ import annotations` at the top of health.py, every annotation
in that file is a string, and FastAPI resolves a handler's annotations against
its MODULE globals — where `Request` did not exist. An unresolvable annotation
is treated as a query parameter, so `request` became a required query field and
FastAPI rejected the request before the handler ever ran:

    {"detail":[{"type":"missing","loc":["query","request"],"msg":"Field required"}]}

`POST /api/agents/{id}/trigger` is the in-engine "fire now" mechanism the
benchmark-runner verification flow was built for; `POST /api/runs/{id}/resume`
is how a checkpointed run is restarted; `POST /api/workflows/{id}/execute` is
the manual workflow trigger. All three were unreachable.

Nothing caught it because every test of these routes called the handler
function directly, where the annotation is never resolved. These tests go
through the app on purpose: what is being pinned is that FastAPI can BUILD the
route, which is precisely the step a direct call skips.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.auth import tokens
from robothor.engine.config import EngineConfig

TENANT = "test-tenant"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "engine-auth-test-key-that-is-at-least-32-bytes")
    tokens.reset_signing_key_cache()

    config = EngineConfig(workspace=tmp_path, manifest_dir=tmp_path, tenant_id=TENANT)
    runner = MagicMock()
    runner.execute = AsyncMock()

    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.engine.chat.load_all_sessions", return_value={}),
    ):
        app = create_health_app(config, runner=runner)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens.issue_access_token('human-1', TENANT, 'admin')}"}


def test_trigger_reaches_its_handler(client, auth):
    response = client.post("/api/agents/nobody/trigger", headers=auth)

    assert response.status_code == 200, response.text
    assert response.json() == {"error": "Agent not found: nobody"}


def test_resume_reaches_its_handler(client, auth):
    with patch("robothor.engine.tracking.get_run", return_value=None):
        response = client.post("/api/runs/does-not-exist/resume", headers=auth)

    assert response.status_code == 200, response.text
    assert response.json() == {"error": "Run not found: does-not-exist"}


def test_workflow_execute_reaches_its_handler(client, auth):
    """No workflow engine was passed, so the handler's own first guard answers."""
    response = client.post("/api/workflows/anything/execute", headers=auth)

    assert response.status_code == 200, response.text
    assert response.json() == {"error": "Workflow engine not available"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/agents/nobody/trigger",
        "/api/runs/does-not-exist/resume",
        "/api/workflows/anything/execute",
    ],
)
def test_no_route_demands_request_as_a_query_parameter(client, auth, path):
    """The specific symptom, named so a regression is recognisable.

    A 422 here means FastAPI could not resolve `Request` again — the annotation
    moved back inside the factory, or `from __future__ import annotations`
    reached a new handler whose type is not a module global.
    """
    with patch("robothor.engine.tracking.get_run", return_value=None):
        response = client.post(path, headers=auth)

    assert response.status_code != 422, response.text


def test_health_still_imports_without_the_optional_api_extra():
    """The reason `Request` is imported in a try/except rather than plainly.

    `fastapi` lives in the optional `api` extra and `daemon.py` imports this
    module unconditionally, so a bare module-level import would break every
    base install. Probed in a subprocess with `fastapi` made unimportable,
    because asserting it here — in a process where fastapi is already in
    `sys.modules` — would prove nothing at all.
    """
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    program = (
        "import sys\n"
        "class Block:\n"
        "    def find_module(self, name, path=None):\n"
        "        return self if name == 'fastapi' or name.startswith('fastapi.') else None\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'fastapi' or name.startswith('fastapi.'):\n"
        "            raise ImportError('fastapi is not installed')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        # Prove the blocker blocks. Without this the probe passes whether or
        # not fastapi was actually hidden, which is no probe at all.
        "try:\n"
        "    import fastapi\n"
        "    raise AssertionError('the fastapi blocker did not block')\n"
        "except ImportError:\n"
        "    pass\n"
        "import typing\n"
        "import robothor.engine.health as h\n"
        "assert h.Request is typing.Any, h.Request\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={"PYTHONPATH": str(repo), "PATH": "/usr/bin:/bin", "HOME": str(repo)},
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "ok" in result.stdout
