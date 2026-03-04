"""Tests for Agent RBAC capabilities module.

Phase 3: Validates capability checking, manifest loading,
tool/endpoint/stream access control, and backward compatibility.
"""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.expanduser("~/robothor/brain/memory_system"))
import capabilities


@pytest.fixture(autouse=True)
def reset_capabilities():
    """Reset cached manifest between tests."""
    capabilities.reset()
    yield
    capabilities.reset()


# ─── Manifest Loading ────────────────────────────────────────────────


class TestManifestLoading:
    def test_loads_real_manifest(self):
        """Load the actual agent_capabilities.json file."""
        manifest = capabilities.load_capabilities()
        assert "agents" in manifest
        assert "version" in manifest

    def test_all_agents_present(self):
        """All 12 agents in the manifest."""
        capabilities.load_capabilities()
        agents = capabilities.list_agents()
        expected = [
            "email-classifier",
            "calendar-monitor",
            "email-analyst",
            "email-responder",
            "supervisor",
            "vision-monitor",
            "conversation-inbox",
            "conversation-resolver",
            "crm-steward",
            "morning-briefing",
            "evening-winddown",
            "helm-user",
        ]
        for agent in expected:
            assert agent in agents, f"Missing agent: {agent}"

    def test_missing_file_returns_empty(self):
        """Missing manifest file returns empty dict."""
        manifest = capabilities.load_capabilities("/nonexistent/path.json")
        assert manifest == {}

    def test_malformed_json_returns_empty(self):
        """Invalid JSON returns empty dict."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            f.flush()
            manifest = capabilities.load_capabilities(f.name)
            assert manifest == {}
        os.unlink(f.name)

    def test_manifest_schema_valid(self):
        """Manifest has required structure."""
        manifest = capabilities.load_capabilities()
        for agent_id, agent in manifest.get("agents", {}).items():
            assert "tools" in agent, f"{agent_id} missing tools"
            assert "bridge_endpoints" in agent, f"{agent_id} missing bridge_endpoints"
            assert "streams_read" in agent, f"{agent_id} missing streams_read"
            assert isinstance(agent["tools"], list), f"{agent_id} tools not a list"


# ─── Tool Access ─────────────────────────────────────────────────────


class TestToolAccess:
    def test_allowed_tool(self):
        """Email classifier can use list_conversations."""
        capabilities.load_capabilities()
        assert capabilities.check_tool_access("email-classifier", "list_conversations") is True

    def test_denied_tool(self):
        """Email classifier cannot merge contacts."""
        capabilities.load_capabilities()
        assert capabilities.check_tool_access("email-classifier", "merge_contacts") is False

    def test_crm_steward_has_merge(self):
        """CRM steward can merge contacts."""
        capabilities.load_capabilities()
        assert capabilities.check_tool_access("crm-steward", "merge_contacts") is True

    def test_vision_monitor_no_crm_tools(self):
        """Vision monitor has no CRM tools."""
        capabilities.load_capabilities()
        assert capabilities.check_tool_access("vision-monitor", "create_person") is False
        assert capabilities.check_tool_access("vision-monitor", "list_people") is False

    def test_helm_user_has_broad_access(self):
        """Helm user can access most tools."""
        capabilities.load_capabilities()
        for tool in ["list_conversations", "create_person", "merge_contacts", "crm_health"]:
            assert capabilities.check_tool_access("helm-user", tool) is True

    def test_unknown_agent_gets_full_access(self):
        """Unknown agent ID gets default_policy (allow)."""
        capabilities.load_capabilities()
        assert capabilities.check_tool_access("unknown-agent", "merge_contacts") is True

    def test_get_agent_tools(self):
        """get_agent_tools returns tool list."""
        capabilities.load_capabilities()
        tools = capabilities.get_agent_tools("email-classifier")
        assert "list_conversations" in tools
        assert "log_interaction" in tools
        assert "merge_contacts" not in tools


# ─── Endpoint Access ─────────────────────────────────────────────────


class TestEndpointAccess:
    def test_allowed_endpoint(self):
        """Email classifier can GET /api/conversations."""
        capabilities.load_capabilities()
        assert (
            capabilities.check_endpoint_access("email-classifier", "GET", "/api/conversations")
            is True
        )

    def test_denied_endpoint(self):
        """Email classifier cannot POST /api/people/merge."""
        capabilities.load_capabilities()
        assert (
            capabilities.check_endpoint_access("email-classifier", "POST", "/api/people/merge")
            is False
        )

    def test_wildcard_endpoint(self):
        """CRM steward can PATCH /api/people/123 via wildcard."""
        capabilities.load_capabilities()
        assert (
            capabilities.check_endpoint_access("crm-steward", "PATCH", "/api/people/abc-123")
            is True
        )

    def test_helm_user_wildcard_all(self):
        """Helm user has GET/POST/PATCH /api/* access."""
        capabilities.load_capabilities()
        assert capabilities.check_endpoint_access("helm-user", "GET", "/api/anything") is True
        assert capabilities.check_endpoint_access("helm-user", "POST", "/api/anything") is True
        assert capabilities.check_endpoint_access("helm-user", "PATCH", "/api/anything") is True

    def test_unknown_agent_allowed(self):
        """Unknown agents get full access (backward compat)."""
        capabilities.load_capabilities()
        assert (
            capabilities.check_endpoint_access("unknown-agent", "POST", "/api/people/merge") is True
        )

    def test_health_endpoint_universal(self):
        """All agents can access GET /health."""
        capabilities.load_capabilities()
        for agent in capabilities.list_agents():
            assert capabilities.check_endpoint_access(agent, "GET", "/health") is True


# ─── Stream Access ───────────────────────────────────────────────────


class TestStreamAccess:
    def test_read_allowed(self):
        """Email classifier can read email stream."""
        capabilities.load_capabilities()
        assert capabilities.check_stream_access("email-classifier", "email", "read") is True

    def test_read_denied(self):
        """Email classifier cannot read vision stream."""
        capabilities.load_capabilities()
        assert capabilities.check_stream_access("email-classifier", "vision", "read") is False

    def test_write_denied(self):
        """Email classifier cannot write any stream."""
        capabilities.load_capabilities()
        assert capabilities.check_stream_access("email-classifier", "email", "write") is False

    def test_crm_steward_can_write_crm(self):
        """CRM steward can write to crm stream."""
        capabilities.load_capabilities()
        assert capabilities.check_stream_access("crm-steward", "crm", "write") is True

    def test_supervisor_reads_all(self):
        """Supervisor can read all streams."""
        capabilities.load_capabilities()
        for stream in ["email", "crm", "health", "agent", "calendar", "vision", "system"]:
            assert capabilities.check_stream_access("supervisor", stream, "read") is True

    def test_unknown_agent_stream_access(self):
        """Unknown agent gets default access."""
        capabilities.load_capabilities()
        assert capabilities.check_stream_access("unknown-agent", "email", "read") is True


# ─── Custom Manifest ────────────────────────────────────────────────


class TestCustomManifest:
    def test_deny_default_policy(self):
        """default_policy: deny blocks unknown agents."""
        manifest = {
            "version": "1.0.0",
            "default_policy": "deny",
            "agents": {
                "test-agent": {
                    "tools": ["search"],
                    "bridge_endpoints": ["GET /health"],
                    "streams_read": ["email"],
                    "streams_write": [],
                }
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(manifest, f)
            f.flush()
            capabilities.load_capabilities(f.name)

        # Known agent works
        assert capabilities.check_tool_access("test-agent", "search") is True
        # Unknown agent denied
        assert capabilities.check_tool_access("rogue-agent", "search") is False
        os.unlink(f.name)
