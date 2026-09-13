"""The engine half of answering an escalation from outside the chat.

An in-RAM escalation lives in the ENGINE process — in a dict, behind an
``asyncio.Event`` that a coroutine in that same process is parked on. The bridge
is a different process and cannot reach it, so a Helm that "approved" an
escalation by writing a row would be approving nothing at all. This route is the
only way that decision crosses the boundary, and it is under ``/api/admin``, so
``engine/auth.py`` requires ``engine:control`` by prefix.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.engine.permission_escalation import EscalationRequest, PermissionEscalationManager


def _client():
    mock_config = MagicMock()
    mock_config.tenant_id = "test-tenant"
    mock_config.bot_token = ""
    mock_config.port = 18800

    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_completion_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.db.connection.get_connection"),
    ):
        app = create_health_app(mock_config, runner=None)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def manager():
    import robothor.engine.permission_escalation as pe

    mgr = PermissionEscalationManager(bot=MagicMock(), chat_id="12345")
    mgr._pending["req-1"] = EscalationRequest(
        request_id="req-1",
        agent_id="assistant",
        run_id="run-1",
        tool_name="exec",
        tool_args={},
        guardrail_name="destructive_write",
        reason="",
        created_at=0.0,
    )
    previous = pe._escalation_manager
    pe._escalation_manager = mgr
    yield mgr
    pe._escalation_manager = previous


class TestResolveRoute:
    def test_approving_wakes_the_waiting_request(self, manager):
        response = _client().post("/api/admin/approvals/escalation/req-1", json={"approved": True})

        assert response.status_code == 200
        assert response.json() == {"settled": True, "approved": True}
        assert manager._pending["req-1"].approved is True
        assert manager._pending["req-1"].result.is_set()

    def test_denying_wakes_it_denied(self, manager):
        _client().post("/api/admin/approvals/escalation/req-1", json={"approved": False})

        assert manager._pending["req-1"].approved is False

    def test_remember_session_grants_the_rest_of_the_session(self, manager):
        _client().post(
            "/api/admin/approvals/escalation/req-1",
            json={"approved": True, "remember_session": True},
        )

        assert manager._session_grants == {"assistant:destructive_write": {"exec"}}

    def test_an_unknown_request_is_404_and_not_a_quiet_success(self, manager):
        """The prompt may have timed out minutes ago. Telling the Helm "done"
        would show an approval the agent never received."""
        response = _client().post("/api/admin/approvals/escalation/gone", json={"approved": True})

        assert response.status_code == 404
        assert response.json()["settled"] is False

    def test_a_second_decision_is_404_too(self, manager):
        client = _client()
        client.post("/api/admin/approvals/escalation/req-1", json={"approved": True})
        second = client.post("/api/admin/approvals/escalation/req-1", json={"approved": False})

        assert second.status_code == 404
        assert manager._pending["req-1"].approved is True

    def test_no_manager_at_all_is_503(self):
        import robothor.engine.permission_escalation as pe

        previous = pe._escalation_manager
        pe._escalation_manager = None
        try:
            response = _client().post(
                "/api/admin/approvals/escalation/req-1", json={"approved": True}
            )
        finally:
            pe._escalation_manager = previous

        assert response.status_code == 503

    def test_the_response_carries_no_tool_arguments(self, manager):
        """This body reaches a browser through the bridge. Tool args are the
        command line an agent wanted to run."""
        body = (
            _client().post("/api/admin/approvals/escalation/req-1", json={"approved": True}).json()
        )

        assert set(body) == {"settled", "approved"}
