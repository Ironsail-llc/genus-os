"""Tests for the generic REST→MCP bridge.

The bridge wraps an HTTP/JSON API as MCP tools over stdio. It must:
  * be configured entirely from environment variables,
  * expose read-only tools by default (writes gated behind CONNECTOR_READONLY=0),
  * build correct requests with auth + Accept headers,
  * enforce an optional resource allowlist,
  * never leak response bodies into logs or error payloads (PHI safety),
  * cap oversized payloads,
  * speak BOTH newline-delimited JSON (Claude Code / standard MCP) and
    Content-Length framing (RoboThor's stdio client), auto-detected per client.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from robothor.connectors.rest_mcp_bridge import (
    ConnectorConfig,
    RestConnector,
    StdioMcpServer,
    dispatch_tool,
    tool_definitions,
)

ENTRYPOINT = {
    "@context": "/api/contexts/Entrypoint",
    "@id": "/api",
    "@type": "Entrypoint",
    "patient": "/api/patients",
    "provider": "/api/providers",
}


def make_connector(handler, **overrides):
    """Build a RestConnector backed by an httpx.MockTransport handler."""
    cfg_kwargs = {
        "base_url": "https://api.test",
        "token": "tok123",
        "tool_prefix": "imp",
        "readonly": True,
        "accept": "application/ld+json",
        "api_root": "/api",
        "max_chars": 6000,
    }
    cfg_kwargs.update(overrides)
    config = ConnectorConfig(**cfg_kwargs)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RestConnector(config, client), config


# ── Config ────────────────────────────────────────────────────────────────


class TestConnectorConfig:
    def test_from_env_reads_values(self):
        env = {
            "CONNECTOR_BASE_URL": "https://app.impetusone.com",
            "CONNECTOR_TOKEN": "secret",
            "CONNECTOR_TOOL_PREFIX": "impetus",
            "CONNECTOR_READONLY": "1",
            "CONNECTOR_ALLOWED_RESOURCES": "patients, providers",
        }
        cfg = ConnectorConfig.from_env(env)
        assert cfg.base_url == "https://app.impetusone.com"
        assert cfg.token == "secret"
        assert cfg.tool_prefix == "impetus"
        assert cfg.readonly is True
        assert cfg.allowed_resources == ("patients", "providers")

    def test_readonly_defaults_true(self):
        cfg = ConnectorConfig.from_env({"CONNECTOR_BASE_URL": "https://x"})
        assert cfg.readonly is True

    def test_readonly_zero_disables(self):
        cfg = ConnectorConfig.from_env(
            {"CONNECTOR_BASE_URL": "https://x", "CONNECTOR_READONLY": "0"}
        )
        assert cfg.readonly is False

    def test_headers_include_auth_and_accept(self):
        cfg = ConnectorConfig(base_url="https://x", token="tok", accept="application/ld+json")
        h = cfg.request_headers()
        assert h["Authorization"] == "Bearer tok"
        assert h["Accept"] == "application/ld+json"

    def test_custom_auth_header_scheme(self):
        cfg = ConnectorConfig(
            base_url="https://x", token="k", auth_header="X-API-Key", auth_scheme=""
        )
        h = cfg.request_headers()
        assert h["X-API-Key"] == "k"
        assert "Authorization" not in h


# ── Tool definitions ────────────────────────────────────────────────────────


class TestToolDefinitions:
    def test_readonly_excludes_write_tools(self):
        cfg = ConnectorConfig(base_url="https://x", tool_prefix="imp", readonly=True)
        names = {t["name"] for t in tool_definitions(cfg)}
        assert names == {"imp_list_resources", "imp_list", "imp_get", "imp_search"}

    def test_write_tools_present_when_not_readonly(self):
        cfg = ConnectorConfig(base_url="https://x", tool_prefix="imp", readonly=False)
        names = {t["name"] for t in tool_definitions(cfg)}
        assert {"imp_create", "imp_update", "imp_delete"} <= names

    def test_tools_have_valid_schema(self):
        cfg = ConnectorConfig(base_url="https://x", tool_prefix="imp")
        for t in tool_definitions(cfg):
            assert t["name"] and t["description"]
            assert t["inputSchema"]["type"] == "object"


# ── HTTP behavior ────────────────────────────────────────────────────────────


class TestRequests:
    @pytest.mark.asyncio
    async def test_list_resources_returns_usable_collection_names(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["auth"] = request.headers.get("Authorization")
            seen["accept"] = request.headers.get("Accept")
            return httpx.Response(200, json=ENTRYPOINT)

        conn, _ = make_connector(handler)
        result = await conn.list_resources()
        assert seen["path"] == "/api"
        assert seen["auth"] == "Bearer tok123"
        assert seen["accept"] == "application/ld+json"
        # entrypoint keys are singular ("patient"); usable collection names are the
        # plural path segments the model should pass as `resource`.
        assert "patients" in result["resources"]
        assert "providers" in result["resources"]

    @pytest.mark.asyncio
    async def test_singular_resource_name_resolves_to_plural_path(self):
        def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p == "/api":
                return httpx.Response(200, json=ENTRYPOINT)
            if p == "/api/patients":
                return httpx.Response(200, json={"hydra:member": [{"id": 1}]})
            return httpx.Response(404, json={"hydra:description": "No route"})

        conn, _ = make_connector(handler)
        # model passes the singular entrypoint key — bridge must self-heal to /api/patients
        result = await conn.list_items("patient", items_per_page=1)
        assert "error" not in result
        assert result["hydra:member"][0]["id"] == 1

    @pytest.mark.asyncio
    async def test_correct_plural_name_needs_no_entrypoint_lookup(self):
        hits = {"entry": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api":
                hits["entry"] += 1
                return httpx.Response(200, json=ENTRYPOINT)
            return httpx.Response(200, json={"hydra:member": []})

        conn, _ = make_connector(handler)
        await conn.list_items("patients")
        assert hits["entry"] == 0  # correct name → no extra entrypoint fetch

    @pytest.mark.asyncio
    async def test_list_items_builds_url_and_query(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"hydra:totalItems": 0, "hydra:member": []})

        conn, _ = make_connector(handler)
        await conn.list_items("patients", page=2, items_per_page=5, filters={"status": "active"})
        assert seen["path"] == "/api/patients"
        assert seen["params"]["page"] == "2"
        assert seen["params"]["itemsPerPage"] == "5"
        assert seen["params"]["status"] == "active"

    @pytest.mark.asyncio
    async def test_get_item_builds_path(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(200, json={"@id": "/api/patients/123"})

        conn, _ = make_connector(handler)
        await conn.get_item("patients", "123")
        assert seen["path"] == "/api/patients/123"

    @pytest.mark.asyncio
    async def test_allowlist_blocks_disallowed_resource(self):
        called = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(200, json={})

        conn, _ = make_connector(handler, allowed_resources=("patients",))
        result = await conn.list_items("providers")
        assert "error" in result
        assert called["n"] == 0  # no HTTP request made

    @pytest.mark.asyncio
    async def test_error_response_does_not_leak_body(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={
                    "@type": "hydra:Error",
                    "hydra:title": "Not Found",
                    "hydra:description": "patient 999",
                    "ssn": "078-05-1120",
                },
            )

        conn, _ = make_connector(handler)
        result = await conn.get_item("patients", "999")
        blob = json.dumps(result)
        assert result["status"] == 404
        assert "078-05-1120" not in blob  # arbitrary body fields never echoed

    @pytest.mark.asyncio
    async def test_oversized_payload_truncated(self):
        big = {"hydra:member": [{"note": "x" * 500} for _ in range(50)]}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=big)

        conn, _ = make_connector(handler, max_chars=200)
        result = await conn.list_items("patients")
        assert result.get("_truncated") is True

    @pytest.mark.asyncio
    async def test_request_log_omits_query_values(self, caplog):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"hydra:member": []})

        conn, _ = make_connector(handler)
        with caplog.at_level(logging.INFO, logger="robothor.connectors.rest_mcp_bridge"):
            await conn.list_items("patients", filters={"name": "John Doe"})
        assert "John Doe" not in caplog.text  # query values never logged
        assert "/api/patients" in caplog.text


# ── Login / token auto-refresh ───────────────────────────────────────────────


class TestLogin:
    def test_from_env_reads_login_fields(self):
        cfg = ConnectorConfig.from_env(
            {
                "CONNECTOR_BASE_URL": "https://x",
                "CONNECTOR_LOGIN_URL": "https://x/api/auth/login",
                "CONNECTOR_LOGIN_USERNAME": "u@x",
                "CONNECTOR_LOGIN_PASSWORD": "pw",
            }
        )
        assert cfg.login_url == "https://x/api/auth/login"
        assert cfg.login_username == "u@x"
        assert cfg.login_password == "pw"
        assert cfg.login_username_field == "email"  # default
        assert cfg.login_token_field == "token"  # default

    @pytest.mark.asyncio
    async def test_logs_in_when_no_static_token(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/login":
                seen["login"] = True
                return httpx.Response(200, json={"token": "fresh-tok", "expiresIn": 2592000})
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"hydra:member": []})

        conn, _ = make_connector(
            handler,
            token="",
            login_url="https://api.test/auth/login",
            login_username="u@x",
            login_password="pw",
        )
        await conn.list_items("patients")
        assert seen.get("login") is True
        assert seen["auth"] == "Bearer fresh-tok"

    @pytest.mark.asyncio
    async def test_refreshes_token_on_401(self):
        state = {"logins": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/login":
                state["logins"] += 1
                return httpx.Response(200, json={"token": f"tok{state['logins']}"})
            if request.headers.get("Authorization") == "Bearer stale":
                return httpx.Response(401, json={"hydra:description": "Invalid credentials"})
            return httpx.Response(200, json={"@id": "/api/patients/1"})

        conn, _ = make_connector(
            handler,
            token="stale",
            login_url="https://api.test/auth/login",
            login_username="u",
            login_password="p",
        )
        result = await conn.get_item("patients", "1")
        assert state["logins"] == 1  # re-logged-in exactly once
        assert result["@id"] == "/api/patients/1"  # retry with fresh token succeeded

    @pytest.mark.asyncio
    async def test_login_failure_surfaces_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/login":
                return httpx.Response(401, json={"error": "bad creds"})
            return httpx.Response(401, json={"hydra:description": "Invalid credentials"})

        conn, _ = make_connector(
            handler,
            token="",
            login_url="https://api.test/auth/login",
            login_username="u",
            login_password="bad",
        )
        result = await conn.list_items("patients")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_login_never_logs_credentials_or_token(self, caplog):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/login":
                return httpx.Response(200, json={"token": "supersecrettoken"})
            return httpx.Response(200, json={"hydra:member": []})

        conn, _ = make_connector(
            handler,
            token="",
            login_url="https://api.test/auth/login",
            login_username="secretuser@x",
            login_password="supersecretpw",
        )
        with caplog.at_level(logging.INFO, logger="robothor.connectors.rest_mcp_bridge"):
            await conn.list_items("patients")
        assert "supersecretpw" not in caplog.text
        assert "supersecrettoken" not in caplog.text


# ── Dispatch ────────────────────────────────────────────────────────────────


class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_routes_to_list_resources(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=ENTRYPOINT)

        conn, cfg = make_connector(handler)
        result = await dispatch_tool(conn, cfg, "imp_list_resources", {})
        assert "patients" in result["resources"]

    @pytest.mark.asyncio
    async def test_dispatch_unknown_tool_errors(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        conn, cfg = make_connector(handler)
        result = await dispatch_tool(conn, cfg, "imp_bogus", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_dispatch_write_blocked_when_readonly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        conn, cfg = make_connector(handler, readonly=True)
        result = await dispatch_tool(conn, cfg, "imp_create", {"resource": "patients", "data": {}})
        assert "error" in result


# ── stdio framing (dual-consumer) ────────────────────────────────────────────


def _server() -> StdioMcpServer:
    async def dispatch(name, args):
        return {"ok": name}

    return StdioMcpServer(
        name="test-bridge",
        version="1.0",
        tools_fn=lambda: [
            {"name": "t", "description": "d", "inputSchema": {"type": "object", "properties": {}}}
        ],
        dispatch_fn=dispatch,
    )


class _FakeWriter:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


def _reader(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


def _decode_lsp(raw: bytes) -> list[dict]:
    msgs, i = [], 0
    while i < len(raw):
        sep = raw.index(b"\r\n\r\n", i)
        header = raw[i:sep].decode()
        length = int(header.split(":")[1].strip())
        start = sep + 4
        msgs.append(json.loads(raw[start : start + length]))
        i = start + length
    return msgs


class TestStdioFraming:
    @pytest.mark.asyncio
    async def test_ndjson_roundtrip(self):
        reqs = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
            + b"\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
            + b"\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
            + b"\n"
        )
        writer = _FakeWriter()
        await _server().serve(_reader(reqs), writer)
        lines = [json.loads(line) for line in bytes(writer.buf).splitlines() if line.strip()]
        # initialize + tools/list responses only; notification produced no reply
        assert len(lines) == 2
        assert lines[0]["result"]["protocolVersion"] == "2024-11-05"
        assert lines[1]["result"]["tools"][0]["name"] == "t"

    @pytest.mark.asyncio
    async def test_content_length_roundtrip(self):
        def frame(obj):
            payload = json.dumps(obj).encode()
            return f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload

        reqs = frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + frame(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        writer = _FakeWriter()
        await _server().serve(_reader(reqs), writer)
        msgs = _decode_lsp(bytes(writer.buf))
        assert len(msgs) == 2
        assert msgs[0]["result"]["serverInfo"]["name"] == "test-bridge"
        assert msgs[1]["result"]["tools"][0]["name"] == "t"

    @pytest.mark.asyncio
    async def test_tools_call_returns_mcp_content(self):
        def frame(obj):
            payload = json.dumps(obj).encode()
            return f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload

        reqs = frame(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "t", "arguments": {}},
            }
        )
        writer = _FakeWriter()
        await _server().serve(_reader(reqs), writer)
        msg = _decode_lsp(bytes(writer.buf))[0]
        content = msg["result"]["content"]
        assert content[0]["type"] == "text"
        assert json.loads(content[0]["text"]) == {"ok": "t"}

    @pytest.mark.asyncio
    async def test_unknown_method_returns_error(self):
        reqs = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "frobnicate"}).encode() + b"\n"
        writer = _FakeWriter()
        await _server().serve(_reader(reqs), writer)
        msg = json.loads(bytes(writer.buf).splitlines()[0])
        assert msg["error"]["code"] == -32601


# ── MCP 2026-07-28 version tolerance (accept-but-don't-require initialize) ──


class TestVersionTolerantInitialize:
    @pytest.mark.asyncio
    async def test_echoes_known_stateless_protocol_version(self):
        reqs = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2026-07-28"},
                }
            ).encode()
            + b"\n"
        )
        writer = _FakeWriter()
        await _server().serve(_reader(reqs), writer)
        msg = json.loads(bytes(writer.buf).splitlines()[0])
        assert msg["result"]["protocolVersion"] == "2026-07-28"

    @pytest.mark.asyncio
    async def test_falls_back_to_legacy_for_unknown_protocol_version(self):
        reqs = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "1999-01-01"},
                }
            ).encode()
            + b"\n"
        )
        writer = _FakeWriter()
        await _server().serve(_reader(reqs), writer)
        msg = json.loads(bytes(writer.buf).splitlines()[0])
        assert msg["result"]["protocolVersion"] == "2024-11-05"

    @pytest.mark.asyncio
    async def test_stateless_client_skips_initialize_and_is_still_served(self):
        """A 2026-07-28 client never sends initialize at all — tools/list and
        tools/call must work with no prior handshake."""
        reqs = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
            + b"\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "t", "arguments": {}},
                }
            ).encode()
            + b"\n"
        )
        writer = _FakeWriter()
        await _server().serve(_reader(reqs), writer)
        lines = [json.loads(line) for line in bytes(writer.buf).splitlines() if line.strip()]
        assert len(lines) == 2
        assert lines[0]["result"]["tools"][0]["name"] == "t"
        assert json.loads(lines[1]["result"]["content"][0]["text"]) == {"ok": "t"}
