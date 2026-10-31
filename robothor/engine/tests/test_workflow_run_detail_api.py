"""The engine's workflow run-detail route explains a run step by step.

``GET /api/workflows/runs/{run_id}`` returned each step's ``output_text`` and
nothing of what the step was given, so a failed run could not be explained
from the API. It now returns each step's tool name, input, output and error.
Those payloads carry whatever the tool saw, so the route needs the control
capability (owner/admin), is read only within the engine's tenant, and is
never cached.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

RUN_ID = str(uuid.uuid4())
TENANT = "test-tenant"

_RUN_COLS = [
    "id",
    "workflow_id",
    "status",
    "trigger_type",
    "trigger_detail",
    "steps_total",
    "steps_completed",
    "steps_failed",
    "steps_skipped",
    "duration_ms",
    "error_message",
    "context",
    "started_at",
    "completed_at",
]


def _run_row() -> tuple:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return (
        RUN_ID,
        "example-flow",
        "failed",
        "manual",
        None,
        2,
        1,
        1,
        0,
        1200,
        "step lookup failed",
        {},
        now,
        now,
    )


_STEPS = [
    {
        "step_id": "fetch",
        "step_type": "tool",
        "status": "completed",
        "tool_name": "search_records",
        "tool_input": {"query": "Alice"},
        "tool_output": {"results": [{"name": "Alice"}]},
        "error_message": None,
        "output_text": "{'results': [{'name': 'Alice'}]}",
    },
    {
        "step_id": "lookup",
        "step_type": "tool",
        "status": "failed",
        "tool_name": "get_record",
        "tool_input": {"id": "rec-1"},
        "tool_output": {"error": "not found"},
        "error_message": "not found",
        "output_text": "{'error': 'not found'}",
    },
]


class _Cursor:
    """Answers the run query, then the steps query, recording both."""

    def __init__(self, calls: list[tuple[str, tuple]], found: bool) -> None:
        self._calls = calls
        self._found = found
        self.description: list[tuple[str]] = []
        self._step_cols: list[str] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._calls.append((" ".join(sql.split()), params))
        if "FROM workflow_runs" in sql:
            self.description = [(c,) for c in _RUN_COLS]
        else:
            select = " ".join(sql.split()).split("SELECT ", 1)[1].split(" FROM ", 1)[0]
            self._step_cols = [c.strip() for c in select.split(",")]
            self.description = [(c,) for c in self._step_cols]

    def fetchone(self) -> tuple | None:
        return _run_row() if self._found else None

    def fetchall(self) -> list[tuple]:
        return [tuple(step.get(c) for c in self._step_cols) for step in _STEPS]


class _Conn:
    def __init__(self, calls: list[tuple[str, tuple]], found: bool) -> None:
        self._calls = calls
        self._found = found

    def cursor(self) -> _Cursor:
        return _Cursor(self._calls, self._found)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_app():
    mock_config = MagicMock()
    mock_config.tenant_id = TENANT
    mock_config.bot_token = ""
    mock_config.port = 18800

    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.db.connection.get_connection"),
    ):
        return create_health_app(mock_config, runner=None, workflow_engine=None)


def _get(client: TestClient, run_id: str = RUN_ID, *, found: bool = True, headers=None):
    calls: list[tuple[str, tuple]] = []
    with patch("robothor.db.connection.get_connection", lambda *a, **k: _Conn(calls, found)):
        response = client.get(f"/api/workflows/runs/{run_id}", headers=headers or {})
    return response, calls


@pytest.fixture
def client() -> TestClient:
    return TestClient(_make_app(), raise_server_exceptions=False)


class TestRunDetailBody:
    def test_steps_carry_tool_name_input_output_and_error(self, client) -> None:
        response, _ = _get(client)
        assert response.status_code == 200
        steps = {s["step_id"]: s for s in response.json()["steps"]}
        assert steps["fetch"]["tool_name"] == "search_records"
        assert steps["fetch"]["tool_input"] == {"query": "Alice"}
        assert steps["fetch"]["tool_output"] == {"results": [{"name": "Alice"}]}
        assert steps["fetch"]["error_message"] is None
        assert steps["lookup"]["tool_input"] == {"id": "rec-1"}
        assert steps["lookup"]["error_message"] == "not found"

    def test_response_is_not_cached(self, client) -> None:
        response, _ = _get(client)
        assert response.headers.get("cache-control") == "no-store"

    def test_run_is_read_within_the_engine_tenant(self, client) -> None:
        _, calls = _get(client)
        run_sql, run_params = next(c for c in calls if "FROM workflow_runs" in c[0])
        assert "tenant_id" in run_sql
        assert TENANT in run_params

    def test_unknown_run_is_404(self, client) -> None:
        response, _ = _get(client, found=False)
        assert response.status_code == 404
        assert response.headers.get("cache-control") == "no-store"

    def test_malformed_run_id_is_404_without_a_query(self, client) -> None:
        response, calls = _get(client, "not-a-uuid")
        assert response.status_code == 404
        assert calls == []


class TestRunDetailAuth:
    @staticmethod
    def _secure_client(monkeypatch) -> TestClient:
        monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
        monkeypatch.setenv("ROBOTHOR_ENGINE_HOST", "0.0.0.0")
        monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "engine-test-signing-key-32-bytes-minimum")
        from robothor.auth.tokens import reset_signing_key_cache

        reset_signing_key_cache()
        return TestClient(_make_app(), raise_server_exceptions=False)

    @staticmethod
    def _bearer(role: str) -> dict[str, Any]:
        from robothor.auth.tokens import issue_access_token

        token = issue_access_token("user-1", TENANT, role, audience="genus-bridge")
        return {"Authorization": f"Bearer {token}"}

    @pytest.mark.parametrize("role", ["viewer", "member", "auditor"])
    def test_non_admin_token_is_403(self, monkeypatch, role) -> None:
        client = self._secure_client(monkeypatch)
        response, calls = _get(client, headers=self._bearer(role))
        assert response.status_code == 403
        assert calls == []

    @pytest.mark.parametrize("role", ["owner", "admin"])
    def test_admin_token_reads_the_run(self, monkeypatch, role) -> None:
        client = self._secure_client(monkeypatch)
        response, _ = _get(client, headers=self._bearer(role))
        assert response.status_code == 200
        assert response.json()["steps"][0]["tool_name"] == "search_records"
