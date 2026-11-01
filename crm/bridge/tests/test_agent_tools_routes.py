"""The chat's Tools dialog reads: an agent's tools and the MCP servers.

Both answers live in the engine (the registry and the MCP pool are objects in
THAT process), so this router is a gate and a proxy and nothing else. The gate
is operator-only: a tool list and a server list describe what the appliance can
do, which is operator information. A rejected caller never reaches the engine.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENGINE_TOOLS = {
    "agent": "example-agent",
    "groups": {
        "core": ["read_file"],
        "plugins": {"acme-tools": ["acme_probe"]},
        "mcp": {"example-mcp": ["example_search"]},
        "opt_in": [],
    },
    "allowed": ["read_file", "acme_probe", "example_search"],
    "denied": [],
    "count": 3,
}

ENGINE_SERVERS = {
    "servers": [
        {
            "name": "example-mcp",
            "transport": "stdio",
            "running": True,
            "initialized": True,
            "protocol": "2025-06-18",
            "tool_count": 1,
        }
    ]
}


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.status = 200

    async def __call__(self, method, path, *, json=None, timeout=30):
        self.calls.append((method, path))
        if path.endswith("/mcp/servers"):
            return self.status, ENGINE_SERVERS
        return self.status, ENGINE_TOOLS


@pytest.fixture
def fake_engine():
    engine = FakeEngine()
    with patch("routers.agent_tools.engine_request", new=engine):
        yield engine


@pytest.mark.parametrize(
    "client_name",
    [
        "controls_client_as_viewer",
        "controls_client_as_service",
        "controls_client_as_other_tenant_owner",
    ],
)
@pytest.mark.parametrize("path", ["/api/agents/example-agent/tools", "/api/mcp/servers"])
def test_a_non_operator_is_refused_before_the_engine_is_called(
    request, client_name, path, fake_engine
):
    client = request.getfixturevalue(client_name)
    assert client.get(path).status_code == 403
    assert not fake_engine.calls, "a rejected caller reached the engine"


def test_it_proxies_the_agents_tools(controls_client_as_operator, fake_engine):
    response = controls_client_as_operator.get("/api/agents/example-agent/tools")

    assert response.status_code == 200
    assert response.json() == ENGINE_TOOLS
    assert fake_engine.calls == [("GET", "/api/admin/agents/example-agent/tools")]


def test_it_proxies_the_mcp_servers(controls_client_as_operator, fake_engine):
    response = controls_client_as_operator.get("/api/mcp/servers")

    assert response.status_code == 200
    assert response.json() == ENGINE_SERVERS
    assert fake_engine.calls == [("GET", "/api/admin/mcp/servers")]


@pytest.mark.parametrize("path", ["/api/agents/example-agent/tools", "/api/mcp/servers"])
def test_the_answer_is_never_cached(controls_client_as_operator, fake_engine, path):
    response = controls_client_as_operator.get(path)

    assert response.headers["cache-control"] == "no-store"


def test_the_engines_status_is_passed_through(controls_client_as_operator, fake_engine):
    fake_engine.status = 404
    assert controls_client_as_operator.get("/api/agents/example-agent/tools").status_code == 404

    fake_engine.status = 502
    assert controls_client_as_operator.get("/api/mcp/servers").status_code == 502


def test_an_agent_id_that_is_not_an_identifier_never_reaches_the_engine(
    controls_client_as_operator, fake_engine
):
    response = controls_client_as_operator.get("/api/agents/bad%20id/tools")

    assert response.status_code == 422
    assert not fake_engine.calls


def test_a_trailing_newline_in_the_id_is_refused_as_a_422(controls_client_as_operator, fake_engine):
    """``re.match`` with ``$`` admits ``abc\\n``, which the engine client then
    refuses as a malformed path -- a 500 where a 422 belongs."""
    response = controls_client_as_operator.get("/api/agents/abc%0A/tools")

    assert response.status_code == 422
    assert not fake_engine.calls


@pytest.mark.parametrize("engine_status", [401, 403])
@pytest.mark.parametrize("path", ["/api/agents/example-agent/tools", "/api/mcp/servers"])
def test_an_engine_refusing_the_bridge_is_a_502_not_the_callers_refusal(
    controls_client_as_operator, fake_engine, engine_status, path
):
    """An engine 401/403 is about the BRIDGE's token or scope. Passed through,
    the dialog would tell an operator their session ended, or that only an
    operator may look -- both false. The bridge's own gate still answers 403."""
    fake_engine.status = engine_status

    response = controls_client_as_operator.get(path)

    assert response.status_code == 502
    assert response.json()["error"].startswith("the engine refused the bridge's credentials")
    assert response.headers["cache-control"] == "no-store"
