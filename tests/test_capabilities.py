"""Tests for robothor.events.capabilities — Agent RBAC checking."""

import json
import os
import tempfile

import pytest

from robothor.events.capabilities import (
    check_endpoint_access,
    check_stream_access,
    check_tool_access,
    get_agent_tools,
    get_default_policy,
    list_agents,
    load_capabilities,
    reset,
)


@pytest.fixture(autouse=True)
def reset_capabilities():
    """Reset cached manifest between tests."""
    reset()
    yield
    reset()


# Sample manifest for self-contained tests (no reliance on real agent_capabilities.json)
SAMPLE_MANIFEST = {
    "version": "1.0.0",
    "default_policy": "allow",
    "agents": {
        "email-agent": {
            "tools": ["list_conversations", "log_interaction", "search_memory"],
            "bridge_endpoints": [
                "GET /api/conversations*",
                "POST /api/conversations/*/messages",
                "GET /health",
            ],
            "streams_read": ["email"],
            "streams_write": [],
        },
        "crm-agent": {
            "tools": [
                "list_people",
                "create_person",
                "update_person",
                "merge_contacts",
                "list_conversations",
                "log_interaction",
            ],
            "bridge_endpoints": [
                "GET /api/*",
                "POST /api/*",
                "PATCH /api/*",
                "DELETE /api/*",
                "GET /health",
            ],
            "streams_read": ["crm", "email"],
            "streams_write": ["crm"],
        },
        "supervisor": {
            "tools": ["search_memory", "list_conversations", "crm_health"],
            "bridge_endpoints": ["GET /api/*", "GET /health"],
            "streams_read": ["email", "crm", "health", "agent", "calendar", "vision", "system"],
            "streams_write": ["agent"],
        },
    },
}


@pytest.fixture
def sample_manifest_file():
    """Create a temp manifest file with sample data."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(SAMPLE_MANIFEST, f)
        f.flush()
        yield f.name
    os.unlink(f.name)


# ─── Manifest Loading ────────────────────────────────────────────────


class TestManifestLoading:
    def test_loads_manifest(self, sample_manifest_file):
        manifest = load_capabilities(sample_manifest_file)
        assert "agents" in manifest
        assert "version" in manifest

    def test_all_agents_present(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        agents = list_agents()
        assert set(agents) == {"email-agent", "crm-agent", "supervisor"}

    def test_missing_file_returns_empty(self):
        manifest = load_capabilities("/nonexistent/path.json")
        assert manifest == {}

    def test_malformed_json_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            f.flush()
            manifest = load_capabilities(f.name)
            assert manifest == {}
        os.unlink(f.name)

    def test_manifest_schema_valid(self, sample_manifest_file):
        manifest = load_capabilities(sample_manifest_file)
        for agent_id, agent in manifest.get("agents", {}).items():
            assert "tools" in agent, f"{agent_id} missing tools"
            assert "bridge_endpoints" in agent, f"{agent_id} missing bridge_endpoints"
            assert "streams_read" in agent, f"{agent_id} missing streams_read"
            assert isinstance(agent["tools"], list), f"{agent_id} tools not a list"


# ─── Tool Access ─────────────────────────────────────────────────────


class TestToolAccess:
    def test_allowed_tool(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        assert check_tool_access("email-agent", "list_conversations") is True

    def test_denied_tool(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        assert check_tool_access("email-agent", "merge_contacts") is False

    def test_crm_agent_has_merge(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        assert check_tool_access("crm-agent", "merge_contacts") is True

    def test_unknown_agent_gets_full_access(self, sample_manifest_file):
        """Unknown agent ID gets default_policy (allow)."""
        load_capabilities(sample_manifest_file)
        assert check_tool_access("unknown-agent", "merge_contacts") is True

    def test_get_agent_tools(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        tools = get_agent_tools("email-agent")
        assert "list_conversations" in tools
        assert "log_interaction" in tools
        assert "merge_contacts" not in tools

    def test_get_agent_tools_unknown(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        tools = get_agent_tools("unknown-agent")
        assert tools == []


# ─── Endpoint Access ─────────────────────────────────────────────────


class TestEndpointAccess:
    def test_allowed_endpoint(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        assert check_endpoint_access("email-agent", "GET", "/api/conversations") is True

    def test_denied_endpoint(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        assert check_endpoint_access("email-agent", "POST", "/api/people/merge") is False

    def test_wildcard_endpoint(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        assert check_endpoint_access("crm-agent", "PATCH", "/api/people/abc-123") is True

    def test_unknown_agent_allowed(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        assert check_endpoint_access("unknown-agent", "POST", "/api/people/merge") is True

    def test_health_endpoint(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        # All agents in our manifest have GET /health
        for agent in list_agents():
            assert check_endpoint_access(agent, "GET", "/health") is True


# ─── Stream Access ───────────────────────────────────────────────────


class TestStreamAccess:
    def test_read_allowed(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        assert check_stream_access("email-agent", "email", "read") is True

    def test_read_denied(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        assert check_stream_access("email-agent", "vision", "read") is False

    def test_write_denied(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        assert check_stream_access("email-agent", "email", "write") is False

    def test_crm_agent_can_write_crm(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        assert check_stream_access("crm-agent", "crm", "write") is True

    def test_supervisor_reads_all(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        for stream in ["email", "crm", "health", "agent", "calendar", "vision", "system"]:
            assert check_stream_access("supervisor", stream, "read") is True

    def test_unknown_agent_stream_access(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        assert check_stream_access("unknown-agent", "email", "read") is True


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
            load_capabilities(f.name)

        assert check_tool_access("test-agent", "search") is True
        assert check_tool_access("rogue-agent", "search") is False
        os.unlink(f.name)

    def test_default_policy_value(self, sample_manifest_file):
        load_capabilities(sample_manifest_file)
        assert get_default_policy() == "allow"
