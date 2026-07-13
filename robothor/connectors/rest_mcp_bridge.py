"""Generic REST→MCP bridge.

Wraps any HTTP/JSON API (API Platform / Hydra, OpenAPI, plain REST) as a small
set of MCP tools served over stdio. One binary, configured entirely by
environment variables, works for **both** consumers of stdio MCP:

* **Claude Code** and the official MCP spec use newline-delimited JSON.
* The **RoboThor engine** stdio client (``robothor/engine/mcp_client.py``) uses
  Content-Length framing.

The server auto-detects which framing a client uses on its first message and
replies in kind, so the same process serves either.

Configuration (all via env)::

    CONNECTOR_BASE_URL          required, e.g. https://app.impetusone.com
    CONNECTOR_TOKEN             API token (kept out of logs)
    CONNECTOR_AUTH_HEADER       default "Authorization"
    CONNECTOR_AUTH_SCHEME       default "Bearer" ("" => raw token in header)
    CONNECTOR_TOOL_PREFIX       default "api" — namespaces tool names
    CONNECTOR_READONLY          default "1" — writes only registered when "0"
    CONNECTOR_ACCEPT            default "application/ld+json"
    CONNECTOR_API_ROOT          default "/api" — Hydra/collection entrypoint path
    CONNECTOR_ALLOWED_RESOURCES optional CSV allowlist of resource names
    CONNECTOR_MAX_CHARS         default 6000 — response payload cap
    CONNECTOR_TIMEOUT           default 30 (seconds)
    CONNECTOR_NAME              default "rest-connector" — server/label name
    CONNECTOR_LOG_FILE          optional file for the audit log (else stderr)
    CONNECTOR_LOG_LEVEL         default "WARNING"

PHI safety: request bodies and query-parameter *values* are never logged (only
``METHOD path -> status``); error payloads surface HTTP status + the API's own
title/description only, never arbitrary response fields; oversized payloads are
truncated. Run read-only and behind a resource allowlist for sensitive APIs.

Run standalone::

    python -m robothor.connectors.rest_mcp_bridge
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
# MCP 2026-07-28 (RC) drops the initialize handshake entirely (stateless core).
# We stay version-tolerant: accept initialize if a legacy client sends it
# (echoing back whichever known version it requested), but never require it —
# a stateless client that skips straight to tools/list works unchanged.
# "2026-07-28" is duplicated (not imported) from robothor.engine.mcp_client's
# PROTOCOL_STATELESS: this module runs standalone (see module docstring) and
# deliberately has no import dependency on the engine package.
KNOWN_PROTOCOL_VERSIONS = {PROTOCOL_VERSION, "2026-07-28"}
_FALSEY = {"", "0", "false", "no", "off"}


def _as_bool(value: str) -> bool:
    return value.strip().lower() not in _FALSEY


# ── Configuration ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConnectorConfig:
    """Everything the bridge needs, sourced from the environment."""

    base_url: str = ""
    token: str = ""
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    tool_prefix: str = "api"
    readonly: bool = True
    accept: str = "application/ld+json"
    api_root: str = "/api"
    allowed_resources: tuple[str, ...] = ()
    max_chars: int = 6000
    timeout: float = 30.0
    name: str = "rest-connector"
    log_file: str = ""
    log_level: str = "WARNING"
    # Optional username/password login → auto-refreshing bearer token. When set
    # (and no static token), the bridge logs in to mint a token and re-logs-in
    # on a 401, so short-lived tokens never silently break the integration.
    login_url: str = ""
    login_username: str = ""
    login_password: str = ""
    login_username_field: str = "email"
    login_password_field: str = "password"
    login_token_field: str = "token"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ConnectorConfig:
        e = env if env is not None else os.environ

        def get(key: str, default: str = "") -> str:
            return e.get(key, default)

        allowed = tuple(
            s.strip() for s in get("CONNECTOR_ALLOWED_RESOURCES").split(",") if s.strip()
        )
        return cls(
            base_url=get("CONNECTOR_BASE_URL").rstrip("/"),
            token=get("CONNECTOR_TOKEN"),
            auth_header=get("CONNECTOR_AUTH_HEADER", "Authorization"),
            auth_scheme=get("CONNECTOR_AUTH_SCHEME", "Bearer"),
            tool_prefix=get("CONNECTOR_TOOL_PREFIX", "api"),
            readonly=_as_bool(get("CONNECTOR_READONLY", "1")),
            accept=get("CONNECTOR_ACCEPT", "application/ld+json"),
            api_root="/" + get("CONNECTOR_API_ROOT", "/api").strip("/"),
            allowed_resources=allowed,
            max_chars=int(get("CONNECTOR_MAX_CHARS", "6000") or 6000),
            timeout=float(get("CONNECTOR_TIMEOUT", "30") or 30),
            name=get("CONNECTOR_NAME", "rest-connector"),
            log_file=get("CONNECTOR_LOG_FILE"),
            log_level=get("CONNECTOR_LOG_LEVEL", "WARNING"),
            login_url=get("CONNECTOR_LOGIN_URL"),
            login_username=get("CONNECTOR_LOGIN_USERNAME"),
            login_password=get("CONNECTOR_LOGIN_PASSWORD"),
            login_username_field=get("CONNECTOR_LOGIN_USERNAME_FIELD", "email"),
            login_password_field=get("CONNECTOR_LOGIN_PASSWORD_FIELD", "password"),
            login_token_field=get("CONNECTOR_LOGIN_TOKEN_FIELD", "token"),
        )

    def request_headers(self) -> dict[str, str]:
        headers = {"Accept": self.accept}
        if self.token:
            headers[self.auth_header] = (
                f"{self.auth_scheme} {self.token}" if self.auth_scheme else self.token
            )
        return headers


# ── REST connector ───────────────────────────────────────────────────────────


class RestConnector:
    """Thin async HTTP client that maps tool calls to REST requests."""

    def __init__(self, config: ConnectorConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self.client = client
        self._token = config.token  # mutable; refreshed via login if configured
        self._entry_cache: dict[str, Any] | None = None  # Hydra entrypoint, lazy

    # -- public operations --------------------------------------------------

    async def list_resources(self) -> Any:
        """Return the usable collection names (no record data).

        API Platform entrypoints map *singular* keys (``patient``) to *plural*
        paths (``/api/patients``); agents must pass the plural segment, so expose
        those directly rather than the raw entrypoint.
        """
        data = await self._entrypoint()
        segments = self._segments(data)
        if segments:
            return {
                "resources": segments,
                "_note": "Pass one of these names as the 'resource' argument.",
            }
        return data

    async def list_items(
        self,
        resource: str,
        page: int = 1,
        items_per_page: int = 30,
        filters: dict[str, Any] | None = None,
    ) -> Any:
        if err := self._check_resource(resource):
            return err
        params: dict[str, Any] = {"page": page, "itemsPerPage": items_per_page}
        if filters:
            params.update(filters)
        return await self._collection_request("GET", resource, params=params)

    async def get_item(self, resource: str, item_id: Any) -> Any:
        if err := self._check_resource(resource):
            return err
        if not item_id:
            return {"error": "id is required"}
        return await self._item_request("GET", resource, item_id)

    async def search(self, resource: str, filters: dict[str, Any] | None) -> Any:
        if err := self._check_resource(resource):
            return err
        return await self._collection_request("GET", resource, params=filters or {})

    async def create_item(self, resource: str, data: dict[str, Any]) -> Any:
        if err := self._check_resource(resource):
            return err
        return await self._collection_request(
            "POST", resource, json_body=data, content_type=self.config.accept
        )

    async def update_item(self, resource: str, item_id: Any, data: dict[str, Any]) -> Any:
        if err := self._check_resource(resource):
            return err
        if not item_id:
            return {"error": "id is required"}
        return await self._item_request(
            "PATCH", resource, item_id, json_body=data, content_type="application/merge-patch+json"
        )

    async def delete_item(self, resource: str, item_id: Any) -> Any:
        if err := self._check_resource(resource):
            return err
        if not item_id:
            return {"error": "id is required"}
        return await self._item_request("DELETE", resource, item_id)

    # -- internals ----------------------------------------------------------

    def _check_resource(self, resource: str) -> dict[str, Any] | None:
        if not resource:
            return {"error": "resource is required"}
        allowed = self.config.allowed_resources
        if allowed and resource not in allowed:
            return {"error": f"resource '{resource}' is not in the allowlist"}
        return None

    async def _collection_request(
        self,
        method: str,
        resource: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        content_type: str | None = None,
    ) -> Any:
        result = await self._request(
            method,
            self._collection_url(resource),
            params=params,
            json_body=json_body,
            content_type=content_type,
        )
        if self._is_404(result) and (seg := await self._mapped_segment(resource)) != resource:
            logger.info("resource '%s' 404 -> retrying as '%s'", resource, seg)
            result = await self._request(
                method,
                self._collection_url(seg),
                params=params,
                json_body=json_body,
                content_type=content_type,
            )
        return result

    async def _item_request(
        self,
        method: str,
        resource: str,
        item_id: Any,
        *,
        json_body: Any | None = None,
        content_type: str | None = None,
    ) -> Any:
        result = await self._request(
            method,
            self._item_url(resource, item_id),
            json_body=json_body,
            content_type=content_type,
        )
        if self._is_404(result) and (seg := await self._mapped_segment(resource)) != resource:
            logger.info("resource '%s' 404 -> retrying as '%s'", resource, seg)
            result = await self._request(
                method, self._item_url(seg, item_id), json_body=json_body, content_type=content_type
            )
        return result

    @staticmethod
    def _is_404(result: Any) -> bool:
        return isinstance(result, dict) and result.get("status") == 404

    async def _entrypoint(self) -> dict[str, Any]:
        if self._entry_cache is None:
            data = await self._request("GET", f"{self.config.base_url}{self.config.api_root}")
            self._entry_cache = data if isinstance(data, dict) else {}
        return self._entry_cache

    @staticmethod
    def _segments(data: Any) -> list[str]:
        if not isinstance(data, dict):
            return []
        out = {
            str(v).rstrip("/").rsplit("/", 1)[-1]
            for k, v in data.items()
            if not k.startswith("@") and isinstance(v, str) and v.startswith("/")
        }
        return sorted(out)

    async def _mapped_segment(self, resource: str) -> str:
        """Map a possibly-singular resource name to its plural path segment."""
        for key, value in (await self._entrypoint()).items():
            if key.startswith("@") or not isinstance(value, str) or not value.startswith("/"):
                continue
            segment = value.rstrip("/").rsplit("/", 1)[-1]
            if resource in (key, segment):
                return segment
        return resource

    def _collection_url(self, resource: str) -> str:
        return f"{self.config.base_url}{self.config.api_root}/{resource}"

    def _item_url(self, resource: str, item_id: Any) -> str:
        ident = str(item_id)
        if ident.startswith("/"):  # already a full IRI path
            return f"{self.config.base_url}{ident}"
        return f"{self._collection_url(resource)}/{ident}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        content_type: str | None = None,
    ) -> Any:
        if not self._token and self._login_enabled():
            await self._login()
        resp = await self._send(method, url, params, json_body, content_type)
        if resp is None:
            return {"error": "request failed", "detail": "network"}
        if resp.status_code == 401 and self._login_enabled():
            logger.info("%s %s -> 401; refreshing token via login", method, httpx.URL(url).path)
            if await self._login():
                resp = await self._send(method, url, params, json_body, content_type)
                if resp is None:
                    return {"error": "request failed", "detail": "network"}
        return self._shape(method, url, resp)

    async def _send(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        json_body: Any | None,
        content_type: str | None,
    ) -> httpx.Response | None:
        headers = self._auth_headers()
        if content_type:
            headers["Content-Type"] = content_type
        try:
            return await self.client.request(
                method, url, params=params, json=json_body, headers=headers
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "%s %s -> request error (%s)", method, httpx.URL(url).path, type(exc).__name__
            )
            return None

    def _shape(self, method: str, url: str, resp: httpx.Response) -> Any:
        path = httpx.URL(url).path  # path only — never log query values (PHI)
        status = resp.status_code
        if status >= 400:
            logger.info("%s %s -> %s", method, path, status)
            return self._error_payload(resp)
        if status == 204 or not resp.content:
            logger.info("%s %s -> %s", method, path, status)
            return {"ok": True, "status": status}
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            logger.info("%s %s -> %s (non-json)", method, path, status)
            return {"status": status, "text": resp.text[: self.config.max_chars]}

        count = len(data.get("hydra:member", [])) if isinstance(data, dict) else None
        suffix = f" ({count} items)" if count is not None else ""
        logger.info("%s %s -> %s%s", method, path, status, suffix)
        return self._cap(data)

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Accept": self.config.accept}
        if self._token:
            headers[self.config.auth_header] = (
                f"{self.config.auth_scheme} {self._token}"
                if self.config.auth_scheme
                else self._token
            )
        return headers

    def _login_enabled(self) -> bool:
        c = self.config
        return bool(c.login_url and c.login_username and c.login_password)

    async def _login(self) -> bool:
        """Exchange username/password for a fresh token. Never logs creds/token."""
        c = self.config
        body = {c.login_username_field: c.login_username, c.login_password_field: c.login_password}
        try:
            resp = await self.client.post(
                c.login_url,
                json=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            logger.warning("login -> request error (%s)", type(exc).__name__)
            return False
        if resp.status_code >= 400:
            logger.warning("login -> %s", resp.status_code)
            return False
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            logger.warning("login -> non-json response")
            return False
        token = data.get(c.login_token_field) if isinstance(data, dict) else None
        if not token:
            logger.warning("login -> response missing '%s' field", c.login_token_field)
            return False
        self._token = str(token)
        logger.info("login -> %s; token refreshed", resp.status_code)
        return True

    def _error_payload(self, resp: httpx.Response) -> dict[str, Any]:
        out: dict[str, Any] = {"error": f"HTTP {resp.status_code}", "status": resp.status_code}
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            return out
        if isinstance(body, dict):
            for key in ("hydra:title", "title"):
                if body.get(key):
                    out["title"] = str(body[key])[:200]
                    break
            for key in ("hydra:description", "description", "detail"):
                if body.get(key):
                    out["detail"] = str(body[key])[:200]
                    break
        return out

    def _cap(self, data: Any) -> Any:
        text = json.dumps(data, default=str)
        if len(text) <= self.config.max_chars:
            return data
        return {
            "_truncated": True,
            "_bytes": len(text),
            "_note": "Response exceeded CONNECTOR_MAX_CHARS; narrow with filters or pagination.",
            "preview": text[: self.config.max_chars],
        }


# ── Tool definitions & dispatch ──────────────────────────────────────────────

_RESOURCE_PROP = {
    "type": "string",
    "description": "Resource collection name (see <prefix>_list_resources), e.g. 'patients'.",
}


def tool_definitions(config: ConnectorConfig) -> list[dict[str, Any]]:
    """MCP tool schemas, namespaced by prefix; writes only when not read-only."""
    p = config.tool_prefix
    mode = "read-only" if config.readonly else "read/write"
    tools: list[dict[str, Any]] = [
        {
            "name": f"{p}_list_resources",
            "description": f"List the API's available resource collections ({mode} connector).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": f"{p}_list",
            "description": "List items in a resource collection (paginated, optional filters).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "resource": _RESOURCE_PROP,
                    "page": {"type": "integer", "default": 1},
                    "items_per_page": {"type": "integer", "default": 30},
                    "filters": {"type": "object", "description": "Query filters as key/value."},
                },
                "required": ["resource"],
            },
        },
        {
            "name": f"{p}_get",
            "description": "Fetch a single item by id (or full IRI path).",
            "inputSchema": {
                "type": "object",
                "properties": {"resource": _RESOURCE_PROP, "id": {"type": "string"}},
                "required": ["resource", "id"],
            },
        },
        {
            "name": f"{p}_search",
            "description": "Search a resource collection using query filters.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "resource": _RESOURCE_PROP,
                    "filters": {"type": "object", "description": "Query filters as key/value."},
                },
                "required": ["resource", "filters"],
            },
        },
    ]
    if not config.readonly:
        tools += [
            {
                "name": f"{p}_create",
                "description": "Create a new item in a resource collection (POST).",
                "inputSchema": {
                    "type": "object",
                    "properties": {"resource": _RESOURCE_PROP, "data": {"type": "object"}},
                    "required": ["resource", "data"],
                },
            },
            {
                "name": f"{p}_update",
                "description": "Update an item by id (PATCH merge).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource": _RESOURCE_PROP,
                        "id": {"type": "string"},
                        "data": {"type": "object"},
                    },
                    "required": ["resource", "id", "data"],
                },
            },
            {
                "name": f"{p}_delete",
                "description": "Delete an item by id (DELETE).",
                "inputSchema": {
                    "type": "object",
                    "properties": {"resource": _RESOURCE_PROP, "id": {"type": "string"}},
                    "required": ["resource", "id"],
                },
            },
        ]
    return tools


async def dispatch_tool(
    connector: RestConnector,
    config: ConnectorConfig,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    """Route an MCP tool call to the matching connector operation."""
    prefix = f"{config.tool_prefix}_"
    if not name.startswith(prefix):
        return {"error": f"Unknown tool: {name}"}
    op = name[len(prefix) :]
    args = arguments or {}

    if op == "list_resources":
        return await connector.list_resources()
    if op == "list":
        return await connector.list_items(
            args.get("resource", ""),
            page=args.get("page", 1),
            items_per_page=args.get("items_per_page", 30),
            filters=args.get("filters"),
        )
    if op == "get":
        return await connector.get_item(args.get("resource", ""), args.get("id"))
    if op == "search":
        return await connector.search(args.get("resource", ""), args.get("filters"))
    if op in ("create", "update", "delete"):
        if config.readonly:
            return {"error": "connector is read-only; write operations are disabled"}
        if op == "create":
            return await connector.create_item(args.get("resource", ""), args.get("data") or {})
        if op == "update":
            return await connector.update_item(
                args.get("resource", ""), args.get("id"), args.get("data") or {}
            )
        return await connector.delete_item(args.get("resource", ""), args.get("id"))
    return {"error": f"Unknown tool: {name}"}


# ── stdio MCP server (framing-aware) ─────────────────────────────────────────


class StdioMcpServer:
    """Minimal JSON-RPC 2.0 MCP server over stdio.

    Auto-detects the client's framing on the first message — Content-Length
    (RoboThor) vs newline-delimited JSON (Claude Code / spec) — and replies the
    same way for the life of the connection.
    """

    def __init__(
        self,
        name: str,
        version: str,
        tools_fn: Callable[[], list[dict[str, Any]]],
        dispatch_fn: Callable[[str, dict[str, Any]], Awaitable[Any]],
    ) -> None:
        self.name = name
        self.version = version
        self.tools_fn = tools_fn
        self.dispatch_fn = dispatch_fn
        self._framing: str | None = None  # "lsp" | "ndjson"

    async def serve(self, reader: asyncio.StreamReader, writer: Any) -> None:
        while True:
            try:
                msg = await self._read_message(reader)
            except asyncio.IncompleteReadError:
                break
            except (json.JSONDecodeError, ValueError):
                continue  # skip malformed frame, keep serving
            if msg is None:
                break
            response = await self._handle(msg)
            if response is not None:
                writer.write(self._encode(response))
                await writer.drain()

    async def _read_message(self, reader: asyncio.StreamReader) -> dict[str, Any] | None:
        if self._framing is None:
            line = await reader.readline()
            while line in (b"\r\n", b"\n"):
                line = await reader.readline()
            if not line:
                return None
            if line.strip().lower().startswith(b"content-length:"):
                self._framing = "lsp"
                length = await self._consume_headers(reader, _parse_length(line))
                return _loads(await reader.readexactly(length))
            self._framing = "ndjson"
            return _loads(line)

        if self._framing == "lsp":
            length = 0
            while True:
                header = await reader.readline()
                if not header:
                    return None
                if header in (b"\r\n", b"\n"):
                    break
                if header.strip().lower().startswith(b"content-length:"):
                    length = _parse_length(header)
            if length <= 0:
                return None
            return _loads(await reader.readexactly(length))

        # ndjson
        line = await reader.readline()
        if not line:
            return None
        if not line.strip():
            return await self._read_message(reader)
        return _loads(line)

    @staticmethod
    async def _consume_headers(reader: asyncio.StreamReader, length: int) -> int:
        while True:
            header = await reader.readline()
            if not header or header in (b"\r\n", b"\n"):
                break
            if header.strip().lower().startswith(b"content-length:"):
                length = _parse_length(header)
        return length

    def _encode(self, message: dict[str, Any]) -> bytes:
        payload = json.dumps(message).encode()
        if self._framing == "lsp":
            return f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
        return payload + b"\n"

    async def _handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        msg_id = msg.get("id")
        if method is None:
            return None
        if method == "initialize":
            requested = (msg.get("params") or {}).get("protocolVersion")
            version = requested if requested in KNOWN_PROTOCOL_VERSIONS else PROTOCOL_VERSION
            return self._result(
                msg_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": self.version},
                },
            )
        if msg_id is None:  # notification (e.g. notifications/initialized) — no reply
            return None
        if method == "ping":
            return self._result(msg_id, {})
        if method == "tools/list":
            return self._result(msg_id, {"tools": self.tools_fn()})
        if method == "tools/call":
            params = msg.get("params") or {}
            try:
                obj = await self.dispatch_fn(params.get("name", ""), params.get("arguments") or {})
            except Exception as exc:  # never crash the loop on a tool error
                logger.warning("tool dispatch failed: %s", type(exc).__name__)
                obj = {"error": f"tool failed: {type(exc).__name__}"}
            is_error = isinstance(obj, dict) and "error" in obj
            return self._result(
                msg_id,
                {
                    "content": [{"type": "text", "text": json.dumps(obj, default=str)}],
                    "isError": is_error,
                },
            )
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    @staticmethod
    def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _parse_length(header: bytes) -> int:
    return int(header.split(b":", 1)[1].strip())


def _loads(raw: bytes) -> dict[str, Any]:
    obj: dict[str, Any] = json.loads(raw)
    return obj


# ── Entry point ──────────────────────────────────────────────────────────────


def _setup_logging(config: ConnectorConfig) -> None:
    level = getattr(logging, config.log_level.upper(), logging.WARNING)
    handler: logging.Handler = (
        logging.FileHandler(config.log_file)
        if config.log_file
        else logging.StreamHandler(sys.stderr)
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    # Attach to the module logger object directly (not by name): when this
    # module is executed via ``python -m ...`` its ``__name__`` is "__main__",
    # so a name-based handler on "robothor.connectors" would never receive the
    # records this module emits. Never write protocol bytes to stdout here —
    # stdout is the JSON-RPC channel.
    logger.handlers = [handler]
    logger.setLevel(level)
    logger.propagate = False


class _StdoutWriter:
    """Synchronous stdout sink for the server.

    Writing straight to ``sys.stdout.buffer`` (rather than an asyncio write
    pipe) keeps the bridge working whether stdout is a pipe, a tty, or a
    redirected file — responses are small and infrequent, so blocking writes
    are fine.
    """

    def write(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    async def drain(self) -> None:  # interface parity with StreamWriter
        return None


async def _stdin_reader() -> asyncio.StreamReader:
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    return reader


async def run(config: ConnectorConfig | None = None) -> None:
    config = config or ConnectorConfig.from_env()
    _setup_logging(config)
    if not config.base_url:
        logger.error("CONNECTOR_BASE_URL is required; nothing to serve")
        return
    logger.info(
        "starting connector '%s' -> %s (readonly=%s)", config.name, config.base_url, config.readonly
    )
    client = httpx.AsyncClient(timeout=config.timeout, follow_redirects=False)
    connector = RestConnector(config, client)
    server = StdioMcpServer(
        name=config.name,
        version="1.0",
        tools_fn=lambda: tool_definitions(config),
        dispatch_fn=lambda name, args: dispatch_tool(connector, config, name, args),
    )
    try:
        reader = await _stdin_reader()
        await server.serve(reader, _StdoutWriter())
    finally:
        await client.aclose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
