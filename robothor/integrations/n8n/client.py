"""A read-only client for the n8n public API.

Only four calls, all GETs: the workflow list, the failed-execution list, one
execution's error message, and ``/healthz``. ``projects`` and ``variables``
are licence-gated on many instances (403) and are never called.

The API key is a credential, so it is kept out of everything that can be
printed: it is sent per request rather than stored on the ``httpx`` client,
:class:`N8nClient`'s ``repr`` names only the base URL, and a failure raises
:class:`N8nError` carrying a CLASS and an HTTP status -- never a vendor body
and never an ``httpx`` exception, whose text and request can carry the
header. The ``httpx`` exception is suppressed (``raise ... from None``) so a
logged traceback cannot reach it either.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_EXECUTIONS",
    "MAX_EXECUTION_BYTES",
    "N8nClient",
    "N8nError",
    "checked_root",
    "instance_root",
]

#: The most failed executions one listing returns, newest first.
MAX_EXECUTIONS = 500
#: Workflows per page (the API's own maximum).
WORKFLOW_PAGE = 250
#: A cursor loop that never ends is a bug on one side or the other; stop.
MAX_PAGES = 100
#: An error message longer than this is cut: it is a hint, not a log.
MAX_ERROR_CHARS = 500
TIMEOUT_SECONDS = 15.0
#: The most of one execution's JSON read to find its error message. An
#: execution with large data is abandoned past this: no message, no memory spike.
MAX_EXECUTION_BYTES = 2 * 1024 * 1024

ErrorKind = Literal["auth", "rate_limit", "network", "unknown", "not_configured"]


class N8nError(Exception):
    """An n8n call failed. ``kind`` is the whole explanation; nothing else is kept."""

    def __init__(self, kind: ErrorKind, status: int | None = None, detail: str = "") -> None:
        self.kind: ErrorKind = kind
        self.status = status
        message = f"{kind} (HTTP {status})" if status is not None else kind
        super().__init__(f"{message}: {detail}" if detail else message)

    def __repr__(self) -> str:
        return f"N8nError(kind={self.kind!r}, status={self.status!r})"


def _classify(status: int) -> ErrorKind:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "network"
    return "unknown"


def instance_root(base_url: str) -> str:
    """The instance root: no trailing slash and no ``/api/v1`` suffix."""
    return (base_url or "").strip().rstrip("/").removesuffix("/api/v1")


def checked_root(base_url: str, *, allow_insecure_http: bool = False) -> str:
    """The instance root, or ``N8nError("not_configured")`` when it is unsafe to send a key to.

    An absolute ``https://`` URL with a host. ``http://`` only for a loopback host,
    or anywhere when ``allow_insecure_http`` (``ROBOTHOR_N8N_ALLOW_INSECURE_HTTP``).
    """
    from urllib.parse import urlsplit

    from robothor.auth.runtime import is_loopback_host

    root = instance_root(base_url)
    try:
        parts = urlsplit(root)
        host = parts.hostname or ""
    except ValueError:
        parts, host = None, ""
    scheme = parts.scheme.lower() if parts is not None else ""
    if scheme == "https" and host:
        return root
    if scheme == "http" and host and (allow_insecure_http or is_loopback_host(host)):
        return root
    raise N8nError(
        "not_configured",
        detail=(
            "ROBOTHOR_N8N_BASE_URL must be an absolute https:// URL with a host "
            "(http:// only for localhost, or with ROBOTHOR_N8N_ALLOW_INSECURE_HTTP)"
        ),
    )


def _when(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _workflow(raw: dict[str, Any]) -> dict[str, Any]:
    tags = [str(t.get("name") or "") for t in raw.get("tags") or [] if isinstance(t, dict)]
    return {
        "workflow_id": str(raw.get("id")),
        "name": str(raw.get("name") or ""),
        "active": bool(raw.get("active")),
        "tags": [t for t in tags if t],
        "remote_updated_at": _when(raw.get("updatedAt")),
    }


def _execution(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "execution_id": str(raw.get("id")),
        "workflow_id": str(raw.get("workflowId") or ""),
        "status": str(raw.get("status") or "error"),
        "mode": str(raw.get("mode") or ""),
        "started_at": _when(raw.get("startedAt")),
        "stopped_at": _when(raw.get("stoppedAt")),
    }


class N8nClient:
    """``async with N8nClient(base_url, api_key) as client: ...``"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT_SECONDS,
        allow_insecure_http: bool = False,
    ) -> None:
        self.base_url = checked_root(base_url, allow_insecure_http=allow_insecure_http)
        self.__key = api_key
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    def __repr__(self) -> str:
        return f"N8nClient(base_url={self.base_url!r})"

    __str__ = __repr__

    async def __aenter__(self) -> N8nClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._http.get(
                f"{self.base_url}{path}",
                params=params,
                headers={"X-N8N-API-KEY": self.__key, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            # The TYPE only: the text and the request can both carry the key.
            logger.info("n8n: %s failed (%s)", path, type(exc).__name__)
            raise N8nError("network") from None
        if response.status_code != 200:
            raise N8nError(_classify(response.status_code), response.status_code)
        try:
            return response.json()
        except ValueError:
            raise N8nError("unknown", response.status_code) from None

    async def _pages(self, path: str, params: dict[str, Any], cap: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            body = await self._get(path, query)
            data = body.get("data") if isinstance(body, dict) else None
            rows.extend(item for item in data or [] if isinstance(item, dict))
            cursor = body.get("nextCursor") if isinstance(body, dict) else None
            if not cursor or len(rows) >= cap:
                break
        return rows[:cap]

    async def healthz(self) -> bool:
        """True when the instance answers ``/healthz``; raises :class:`N8nError` otherwise."""
        await self._get("/healthz")
        return True

    async def list_workflows(self) -> list[dict[str, Any]]:
        """Every workflow, as ``workflow_id, name, active, tags, remote_updated_at``."""
        raw = await self._pages("/api/v1/workflows", {"limit": WORKFLOW_PAGE}, cap=10**6)
        return [_workflow(item) for item in raw]

    async def count_workflows(self, limit: int = 1) -> int:
        """How many workflows the first page of ``limit`` holds: an access probe."""
        body = await self._get("/api/v1/workflows", {"limit": limit})
        return len(body.get("data") or []) if isinstance(body, dict) else 0

    async def list_error_executions(
        self, limit: int = 100, workflow_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Failed executions, newest first, at most :data:`MAX_EXECUTIONS`."""
        params: dict[str, Any] = {"status": "error", "limit": limit, "includeData": "false"}
        if workflow_id:
            params["workflowId"] = workflow_id
        raw = await self._pages("/api/v1/executions", params, cap=MAX_EXECUTIONS)
        return [_execution(item) for item in raw]

    async def get_execution_error(self, execution_id: str) -> str | None:
        """The error message n8n recorded for one execution, or None.

        The body is streamed and abandoned past :data:`MAX_EXECUTION_BYTES`
        (declared or read): an execution's data can be large, and only one
        short string out of it is wanted.
        """
        if not str(execution_id).isdigit():
            return None
        body = await self._get_bounded(
            f"/api/v1/executions/{execution_id}", {"includeData": "true"}
        )
        if not isinstance(body, dict):
            return None
        data = body.get("data") or {}
        result = data.get("resultData") if isinstance(data, dict) else None
        error = result.get("error") if isinstance(result, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if not message:
            return None
        return str(message)[:MAX_ERROR_CHARS]

    async def _get_bounded(self, path: str, params: dict[str, Any]) -> Any:
        """Like :meth:`_get`, but None for a body over the byte budget."""
        import json

        try:
            async with self._http.stream(
                "GET",
                f"{self.base_url}{path}",
                params=params,
                headers={"X-N8N-API-KEY": self.__key, "Accept": "application/json"},
            ) as response:
                if response.status_code != 200:
                    raise N8nError(_classify(response.status_code), response.status_code)
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > MAX_EXECUTION_BYTES:
                    return None
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > MAX_EXECUTION_BYTES:
                        return None
        except httpx.HTTPError as exc:
            logger.info("n8n: %s failed (%s)", path, type(exc).__name__)
            raise N8nError("network") from None
        try:
            return json.loads(bytes(buffer))
        except ValueError:
            return None
