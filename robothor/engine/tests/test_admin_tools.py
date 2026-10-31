"""What an agent can reach, as the chat's Tools dialog reads it.

Two engine routes under ``/api/admin`` (so ``engine:control``, by prefix):

* ``GET /api/admin/agents/{id}/tools`` -- the names the registry would offer
  that agent, grouped by where each one comes from: core, a plugin
  distribution, an MCP server, or the opt-in families.
* ``GET /api/admin/mcp/servers`` -- the MCP pool's servers with their health
  and a tool count. ``command`` and ``url`` are how this process reaches a
  server; they are deployment detail and must never reach a browser.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.plugins.inventory import PluginStatus

SENTINEL = "SENTINEL-NEVER-SHOWN-9c1d"

CORE_TOOL = "read_file"
OPT_IN_TOOL = "claude_code_start"
PLUGIN_TOOL = "acme_probe"
STRAY_PLUGIN_TOOL = "mystery_tool"
MCP_TOOL = "example_search"
MCP_SERVER = "example-mcp"


def _make_app():
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
        return create_health_app(mock_config, runner=None, scheduler=None)


def _client():
    return TestClient(_make_app(), raise_server_exceptions=False)


class FakeRegistry:
    """The registry's answer for one agent, and where each name came from."""

    def __init__(self) -> None:
        self.configs: list[Any] = []
        self.routes = {MCP_TOOL: MCP_SERVER, "example_fetch": MCP_SERVER}

    def get_tool_names(self, config: Any) -> list[str]:
        self.configs.append(config)
        return [CORE_TOOL, OPT_IN_TOOL, PLUGIN_TOOL, STRAY_PLUGIN_TOOL, MCP_TOOL]

    def get_adapter_route(self, name: str) -> str | None:
        return self.routes.get(name)

    def adapter_tool_names(self) -> set[str]:
        return set(self.routes)


class FakePool:
    def list_servers(self) -> list[dict[str, Any]]:
        return [
            {
                "name": MCP_SERVER,
                "transport": "stdio",
                "command": f"/opt/{SENTINEL}/bin/server",
                "url": None,
                "running": True,
                "initialized": True,
                "protocol": "2025-06-18",
            },
            {
                "name": "remote-mcp",
                "transport": "http",
                "command": None,
                "url": f"https://{SENTINEL}.example.invalid/mcp",
                "running": True,
                "initialized": False,
                "protocol": "2025-06-18",
            },
        ]


PLUGINS = (
    PluginStatus(
        name="acme-tools",
        state="loaded",
        manifest={"contract_version": 1, "declared": {"handlers": [PLUGIN_TOOL]}},
    ),
    # A disabled distribution's declaration says nothing about a loaded tool.
    PluginStatus(
        name="other-tools",
        state="disabled",
        manifest={"contract_version": 1, "declared": {"handlers": [STRAY_PLUGIN_TOOL]}},
    ),
)


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    (tmp_path / "example-agent.yaml").write_text(
        "id: example-agent\n"
        "name: Example Agent\n"
        f"tools_allowed: [{CORE_TOOL}, {OPT_IN_TOOL}, {PLUGIN_TOOL}, {MCP_TOOL}]\n"
        "tools_denied: [exec]\n"
    )
    from robothor.engine import admin_tools

    monkeypatch.setattr(admin_tools, "_manifest_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def registry():
    fake = FakeRegistry()
    with (
        patch("robothor.engine.tools.registry.get_registry", return_value=fake),
        patch("robothor.plugins.inventory.inventory", return_value=PLUGINS),
        patch("robothor.engine.mcp_client.get_mcp_client_pool", return_value=FakePool()),
    ):
        yield fake


class TestAgentTools:
    def test_the_fixture_names_are_what_they_claim(self):
        from robothor.engine.tools.constants import OPT_IN_TOOLS
        from robothor.engine.tools.registry import builtin_schema_names

        builtins = builtin_schema_names()
        assert CORE_TOOL in builtins
        assert OPT_IN_TOOL in builtins and OPT_IN_TOOL in OPT_IN_TOOLS
        assert not {PLUGIN_TOOL, STRAY_PLUGIN_TOOL, MCP_TOOL} & builtins

    def test_it_groups_the_agents_tools_by_source(self, agents_dir, registry):
        response = _client().get("/api/admin/agents/example-agent/tools")

        assert response.status_code == 200
        body = response.json()
        assert body["agent"] == "example-agent"
        assert body["groups"] == {
            "core": [CORE_TOOL],
            "plugins": {"acme-tools": [PLUGIN_TOOL], "unattributed": [STRAY_PLUGIN_TOOL]},
            "mcp": {MCP_SERVER: [MCP_TOOL]},
            "opt_in": [OPT_IN_TOOL],
        }
        assert body["count"] == 5

    def test_it_asks_the_registry_about_the_agents_own_config(self, agents_dir, registry):
        _client().get("/api/admin/agents/example-agent/tools")

        (config,) = registry.configs
        assert config.id == "example-agent"

    def test_it_reports_the_manifests_allow_and_deny_lists(self, agents_dir, registry):
        body = _client().get("/api/admin/agents/example-agent/tools").json()

        assert body["allowed"] == [CORE_TOOL, OPT_IN_TOOL, PLUGIN_TOOL, MCP_TOOL]
        assert body["denied"] == ["exec"]

    def test_an_unknown_agent_is_a_404(self, agents_dir, registry):
        response = _client().get("/api/admin/agents/no-such-agent/tools")

        assert response.status_code == 404
        assert not registry.configs

    def test_the_response_names_no_path(self, agents_dir, registry):
        body = _client().get("/api/admin/agents/example-agent/tools").json()

        assert str(agents_dir) not in repr(body)
        assert ".yaml" not in repr(body)


class TestMcpServers:
    def test_it_lists_each_server_with_health_and_a_tool_count(self, registry):
        response = _client().get("/api/admin/mcp/servers")

        assert response.status_code == 200
        servers = {s["name"]: s for s in response.json()["servers"]}
        assert servers[MCP_SERVER] == {
            "name": MCP_SERVER,
            "transport": "stdio",
            "running": True,
            "initialized": True,
            "protocol": "2025-06-18",
            "tool_count": 2,
        }
        assert servers["remote-mcp"]["initialized"] is False
        assert servers["remote-mcp"]["tool_count"] == 0

    def test_command_and_url_never_leave_the_engine(self, registry):
        response = _client().get("/api/admin/mcp/servers")

        assert SENTINEL not in response.text
        for server in response.json()["servers"]:
            assert "command" not in server
            assert "url" not in server


class TestScope:
    """Both routes are under ``/api/admin``, so a read-scoped token is refused."""

    @pytest.fixture
    def _signed_tokens(self, monkeypatch):
        from robothor.auth import tokens

        monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
        monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long")
        tokens.reset_signing_key_cache()
        yield tokens
        tokens.reset_signing_key_cache()

    @pytest.mark.parametrize(
        "path", ["/api/admin/agents/example-agent/tools", "/api/admin/mcp/servers"]
    )
    def test_a_read_scoped_token_is_refused(self, _signed_tokens, registry, agents_dir, path):
        from robothor.engine.auth import ENGINE_AUDIENCE

        token = _signed_tokens.issue_service_token(
            "genus-bridge",
            "test-tenant",
            audience=ENGINE_AUDIENCE,
            scopes=("engine:read",),
            ttl_seconds=60,
        )

        response = _client().get(path, headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 403
