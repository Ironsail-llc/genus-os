"""``auth: {type: oauth_client_credentials, ...}`` on http MCP adapters.

Two halves: the YAML is parsed (and refused when it cannot be honoured), and
the http session mints a bearer token, sends it on every request, and recovers
once from a 401. The HTTP side is faked with ``httpx.MockTransport`` serving
both the token endpoint and the MCP endpoint.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from robothor.engine import adapters
from robothor.engine.adapters import _parse_adapter
from robothor.engine.mcp_client import (
    McpClientPool,
    McpHttpSession,
    McpServerConfig,
    register_adapter,
)
from robothor.engine.oauth_client_credentials import OAuthClientCredentials

TOKEN_URL = "https://auth.example.com/oauth/token"
MCP_URL = "https://mcp.example.com/_mcp"


@pytest.fixture(autouse=True)
def _no_core_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapters, "_core_tool_names", lambda: frozenset())


def _adapter_yaml(**auth_overrides: Any) -> dict[str, Any]:
    auth: dict[str, Any] = {
        "type": "oauth_client_credentials",
        "token_url": "${TOKEN_URL}",
        "client_id": "${CLIENT_ID}",
        "client_secret": "${CLIENT_SECRET}",
        "scope": "platform:read",
    }
    auth.update(auth_overrides)
    return {
        "name": "platform",
        "transport": "http",
        "url": MCP_URL,
        "headers": {"X-Static": "kept"},
        "auth": auth,
    }


@pytest.fixture
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_URL", TOKEN_URL)
    monkeypatch.setenv("CLIENT_ID", "client-123")
    monkeypatch.setenv("CLIENT_SECRET", "the-secret")


# ── Parsing ──────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("_env")
class TestParseAuth:
    def test_resolves_env_vars_like_headers(self):
        adapter = _parse_adapter(_adapter_yaml())

        assert adapter is not None
        assert adapter.auth == OAuthClientCredentials(
            token_url=TOKEN_URL,
            client_id="client-123",
            client_secret="the-secret",
            scope="platform:read",
        )
        assert adapter.headers == {"X-Static": "kept"}

    def test_no_auth_block_means_no_auth(self):
        data = _adapter_yaml()
        del data["auth"]
        adapter = _parse_adapter(data)
        assert adapter is not None
        assert adapter.auth is None

    def test_scope_is_optional(self):
        data = _adapter_yaml()
        del data["auth"]["scope"]
        adapter = _parse_adapter(data)
        assert adapter is not None and adapter.auth is not None
        assert adapter.auth.scope == ""

    def test_unknown_auth_type_refuses_the_adapter_with_reason(self, caplog):
        with caplog.at_level("WARNING", logger="robothor.engine.adapters"):
            adapter = _parse_adapter(_adapter_yaml(type="magic_link"))

        assert adapter is None
        assert any("platform" in r.message and "magic_link" in r.message for r in caplog.records)

    def test_non_mapping_auth_is_refused(self):
        data = _adapter_yaml()
        data["auth"] = "oauth_client_credentials"
        assert _parse_adapter(data) is None

    @pytest.mark.parametrize("missing", ["token_url", "client_id", "client_secret"])
    def test_missing_required_field_is_refused(self, missing, caplog):
        data = _adapter_yaml()
        del data["auth"][missing]
        with caplog.at_level("WARNING", logger="robothor.engine.adapters"):
            assert _parse_adapter(data) is None
        assert any(missing in r.message for r in caplog.records)

    def test_unset_env_var_for_secret_is_refused(self, monkeypatch, caplog):
        monkeypatch.delenv("CLIENT_SECRET")
        with caplog.at_level("WARNING", logger="robothor.engine.adapters"):
            assert _parse_adapter(_adapter_yaml()) is None
        assert any("client_secret" in r.message for r in caplog.records)

    def test_non_http_token_url_is_refused(self):
        assert _parse_adapter(_adapter_yaml(token_url="file:///etc/passwd")) is None

    def test_auth_on_a_stdio_adapter_is_refused(self):
        data = _adapter_yaml()
        data["transport"] = "stdio"
        data["command"] = ["node", "server.mjs"]
        assert _parse_adapter(data) is None

    def test_refusal_never_logs_the_secret(self, caplog):
        with caplog.at_level("WARNING", logger="robothor.engine.adapters"):
            _parse_adapter(_adapter_yaml(type="nope"))
        assert "the-secret" not in caplog.text

    def test_adapter_repr_hides_the_secret(self):
        adapter = _parse_adapter(_adapter_yaml())
        assert "the-secret" not in repr(adapter)

    def test_register_adapter_threads_auth_into_the_pool(self, monkeypatch):
        import robothor.engine.mcp_client as mcp_client_mod

        pool = McpClientPool()
        monkeypatch.setattr(mcp_client_mod, "_pool", pool)
        adapter = _parse_adapter(_adapter_yaml())
        register_adapter(adapter)

        config = pool._configs["platform"]
        assert config.auth == adapter.auth  # type: ignore[union-attr]
        assert config.headers == {"X-Static": "kept"}


# ── Session behaviour ────────────────────────────────────────────────────────


class _Server:
    """Token endpoint + MCP endpoint behind one MockTransport."""

    def __init__(self, mcp_statuses: list[int] | None = None) -> None:
        self.mints = 0
        self.mcp_requests: list[httpx.Request] = []
        self._mcp_statuses = list(mcp_statuses or [])

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            self.mints += 1
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["client_credentials"]
            return httpx.Response(
                200, json={"access_token": f"tok-{self.mints}", "expires_in": 3600}
            )
        self.mcp_requests.append(request)
        status = self._mcp_statuses.pop(0) if self._mcp_statuses else 200
        if status != 200:
            return httpx.Response(status, json={"error": "unauthorized"})
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]},
            },
        )


def _session(server: _Server) -> McpHttpSession:
    config = McpServerConfig(
        name="platform",
        url=MCP_URL,
        headers={"X-Static": "kept"},
        protocol="2026-07-28",
        auth=OAuthClientCredentials(
            token_url=TOKEN_URL, client_id="client-123", client_secret="the-secret"
        ),
    )
    return McpHttpSession(config, transport=httpx.MockTransport(server.handler))


class TestSessionAuth:
    async def test_sends_bearer_token_and_static_headers(self):
        server = _Server()
        session = _session(server)

        result = await session.call_tool("search_actions", {"query": "x"})

        assert result == {"ok": True}
        request = server.mcp_requests[0]
        assert request.headers["Authorization"] == "Bearer tok-1"
        assert request.headers["X-Static"] == "kept"

    async def test_token_is_cached_across_requests(self):
        server = _Server()
        session = _session(server)

        await session.call_tool("a", {})
        await session.call_tool("b", {})

        assert server.mints == 1
        assert [r.headers["Authorization"] for r in server.mcp_requests] == [
            "Bearer tok-1",
            "Bearer tok-1",
        ]

    async def test_single_401_forgets_remints_and_retries_once(self):
        server = _Server(mcp_statuses=[401, 200])
        session = _session(server)

        result = await session.call_tool("a", {})

        assert result == {"ok": True}
        assert server.mints == 2
        assert [r.headers["Authorization"] for r in server.mcp_requests] == [
            "Bearer tok-1",
            "Bearer tok-2",
        ]

    async def test_second_401_is_not_retried_again(self):
        server = _Server(mcp_statuses=[401, 401, 200])
        session = _session(server)

        result = await session.call_tool("a", {})

        assert "error" in result
        assert len(server.mcp_requests) == 2
        assert server.mints == 2

    async def test_token_endpoint_failure_is_reported_not_raised(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return httpx.Response(401, json={"error": "invalid_client"})
            raise AssertionError("MCP endpoint must not be called without a token")

        config = McpServerConfig(
            name="platform",
            url=MCP_URL,
            protocol="2026-07-28",
            auth=OAuthClientCredentials(
                token_url=TOKEN_URL, client_id="c", client_secret="the-secret"
            ),
        )
        session = McpHttpSession(config, transport=httpx.MockTransport(handler))

        result = await session.call_tool("a", {})

        assert "invalid_client" in result["error"]
        assert "the-secret" not in result["error"]

    async def test_no_auth_sends_no_authorization_header(self):
        server = _Server()
        config = McpServerConfig(name="plain", url=MCP_URL, protocol="2026-07-28")
        session = McpHttpSession(config, transport=httpx.MockTransport(server.handler))

        await session.call_tool("a", {})

        assert "Authorization" not in server.mcp_requests[0].headers
        assert server.mints == 0

    async def test_401_without_auth_is_not_retried(self):
        server = _Server(mcp_statuses=[401])
        config = McpServerConfig(name="plain", url=MCP_URL, protocol="2026-07-28")
        session = McpHttpSession(config, transport=httpx.MockTransport(server.handler))

        await session.call_tool("a", {})

        assert len(server.mcp_requests) == 1
