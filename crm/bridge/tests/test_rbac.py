"""Tests for Bridge RBAC middleware.

Phase 3: Validates agent capability enforcement at the Bridge API layer.
"""

import sys
import os

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, "/home/philip/clawd/memory_system")

from bridge_service import app


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ─── No Header (Backward Compatibility) ─────────────────────────────

class TestNoAgentHeader:
    @pytest.mark.asyncio
    async def test_no_header_allows_health(self, client):
        """No X-Agent-Id header → full access (backward compat)."""
        resp = await client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_header_allows_people(self, client):
        """No header → can access any endpoint."""
        resp = await client.get("/api/people")
        assert resp.status_code == 200


# ─── Known Agent: Authorized ────────────────────────────────────────

class TestAuthorizedAgent:
    @pytest.mark.asyncio
    async def test_email_classifier_reads_conversations(self, client):
        """Email classifier can GET /api/conversations."""
        resp = await client.get(
            "/api/conversations",
            headers={"X-Agent-Id": "email-classifier"},
        )
        # Should not be 403 (may be 200 or other depending on service state)
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_crm_steward_reads_people(self, client):
        """CRM steward can GET /api/people."""
        resp = await client.get(
            "/api/people",
            headers={"X-Agent-Id": "crm-steward"},
        )
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_all_agents_access_health(self, client):
        """Every known agent can access /health."""
        for agent_id in ["email-classifier", "supervisor", "crm-steward",
                         "vision-monitor", "helm-user"]:
            resp = await client.get(
                "/health",
                headers={"X-Agent-Id": agent_id},
            )
            assert resp.status_code == 200, f"{agent_id} denied /health"


# ─── Known Agent: Denied ────────────────────────────────────────────

class TestDeniedAgent:
    @pytest.mark.asyncio
    async def test_vision_monitor_denied_people(self, client):
        """Vision monitor cannot access /api/people."""
        resp = await client.get(
            "/api/people",
            headers={"X-Agent-Id": "vision-monitor"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_email_classifier_denied_merge(self, client):
        """Email classifier cannot merge contacts."""
        resp = await client.post(
            "/api/people/merge",
            headers={"X-Agent-Id": "email-classifier"},
            json={"primaryId": "a", "secondaryId": "b"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_calendar_monitor_denied_vault(self, client):
        """Calendar monitor cannot access vault."""
        resp = await client.get(
            "/api/vault/list",
            headers={"X-Agent-Id": "calendar-monitor"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_denied_response_body(self, client):
        """403 response includes agent ID and path."""
        resp = await client.get(
            "/api/people",
            headers={"X-Agent-Id": "vision-monitor"},
        )
        assert resp.status_code == 403
        body = resp.json()
        assert "vision-monitor" in body["error"]
        assert "/api/people" in body["error"]


# ─── Unknown Agent ──────────────────────────────────────────────────

class TestUnknownAgent:
    @pytest.mark.asyncio
    async def test_unknown_agent_allowed(self, client):
        """Unknown agent ID gets default policy (allow)."""
        resp = await client.get(
            "/api/people",
            headers={"X-Agent-Id": "rogue-agent-xyz"},
        )
        # Default policy is "allow" — should not be 403
        assert resp.status_code != 403


# ─── Audit on Deny ─────────────────────────────────────────────────

class TestAuditOnDeny:
    @pytest.mark.asyncio
    async def test_denied_request_logged(self, client):
        """Denied requests should create auth.denied audit events."""
        from unittest.mock import patch

        with patch("bridge_service.audit") as mock_audit:
            resp = await client.get(
                "/api/people",
                headers={"X-Agent-Id": "vision-monitor"},
            )
            assert resp.status_code == 403
            mock_audit.log_event.assert_called_once()
            args = mock_audit.log_event.call_args
            assert args[0][0] == "auth.denied"
            assert "vision-monitor" in args[0][1]  # action string
            assert args[1]["actor"] == "vision-monitor"
            assert args[1]["details"]["path"] == "/api/people"
