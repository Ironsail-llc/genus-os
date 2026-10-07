"""The Microsoft Graph transport: one async HTTP client with Graph's rules built in.

Everything above this module (mail, calendar, ingestion) speaks Graph through
:class:`GraphClient`, so the rules that keep the assistant from double-sending
or leaking live in one place:

**Reads retry, writes never do.** A ``GET`` that meets 429/503/504 waits for
``Retry-After`` (or a short backoff) and tries again, at most
:data:`MAX_READ_ATTEMPTS` times and :data:`MAX_READ_WAIT_SECONDS` of waiting in
total, then raises :class:`~robothor.workspace.errors.RateLimited`. A write
(``POST``/``PATCH``/``DELETE``) is sent once. If the connection dropped after
the request left, or Graph answered 5xx, the outcome is unknown -- the mail may
have gone -- and the caller gets :class:`~robothor.workspace.errors.UnknownEffect`
to reconcile by reading back. Retrying would be how an operator's contact gets
the same email twice. (Same rule as ``robothor/sales/providers.py``.)

**Paging stays on Graph.** ``@odata.nextLink`` is an absolute URL chosen by the
server; it is followed only when its origin (scheme, host, port) equals the
base URL's. A link anywhere else is refused before the bearer token is sent to
it.

**Stable ids, UTC times.** Every request carries
``Prefer: IdType="ImmutableId", outlook.timezone="UTC"``: immutable ids survive
a message moving folders (the id we recorded at send time stays valid for the
verification read-back), and UTC makes calendar times comparable with the rest
of the platform. Graph reads ``Prefer`` as one comma-separated list of
preferences (RFC 7240), so a caller's own preferences are *merged* into that
list -- same-named ones override the default in place -- rather than sent as a
second header or replacing it (:func:`merge_prefer`).

**Four at a time per mailbox.** Exchange throttles per mailbox (four concurrent
requests per app per mailbox), so requests addressed to ``/users/{mailbox}/...``
share an :class:`asyncio.Semaphore` of four keyed by the mailbox, case- and
percent-encoding-insensitive.

**Errors say what Graph said, and no more.** An error carries the HTTP status,
Graph's ``error.code`` and (except for auth failures) its ``error.message``,
redacted and truncated. Never the token, never a request or response body.
Each request gets a fresh ``client-request-id`` (logged at debug), the handle
Microsoft support asks for.
"""

from __future__ import annotations

import asyncio
import contextlib
import email.utils
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import httpx

from robothor.workspace.errors import (
    NotFound,
    PermissionDenied,
    PreconditionFailed,
    RateLimited,
    UnknownEffect,
    WorkspaceError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from robothor.workspace.microsoft.auth import TokenSource

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PREFER",
    "GRAPH_BASE_URL",
    "MAILBOX_CONCURRENCY",
    "MAX_READ_ATTEMPTS",
    "MAX_READ_WAIT_SECONDS",
    "GraphClient",
    "merge_prefer",
]

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
DEFAULT_PREFER = 'IdType="ImmutableId", outlook.timezone="UTC"'
READ_RETRY_STATUSES = frozenset({429, 503, 504})
MAX_READ_ATTEMPTS = 3
MAX_READ_WAIT_SECONDS = 60.0
MAILBOX_CONCURRENCY = 4
DEFAULT_MAX_ITEMS = 1000
TIMEOUT_SECONDS = 30.0

#: Failures that prove the request never left: a write that hit one of these
#: definitely did nothing, so it is a plain failure, not an unknown effect.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)

_MAILBOX_SEGMENT = re.compile(r"^/users/([^/]+)", re.IGNORECASE)
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")
_FORBIDDEN_PATH = re.compile(r"[\s\\#?]|://")
_MESSAGE_LIMIT = 300


def _split_prefer(value: str) -> list[str]:
    """Split a ``Prefer`` value on commas that are not inside quotes."""
    items: list[str] = []
    current: list[str] = []
    quoted = False
    for char in value:
        if char == '"':
            quoted = not quoted
        if char == "," and not quoted:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    items.append("".join(current).strip())
    return [item for item in items if item]


def _preference_name(item: str) -> str:
    return item.split("=", 1)[0].split(";", 1)[0].strip().lower()


def merge_prefer(base: str, extra: str | None) -> str:
    """One ``Prefer`` value: ``base``'s preferences, overridden/extended by ``extra``'s."""
    merged = _split_prefer(base)
    for item in _split_prefer(extra or ""):
        name = _preference_name(item)
        for index, existing in enumerate(merged):
            if _preference_name(existing) == name:
                merged[index] = item
                break
        else:
            merged.append(item)
    return ", ".join(merged)


def _retry_after(response: httpx.Response) -> float | None:
    raw = (response.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def _redact(text: str) -> str:
    try:
        from robothor.secrets.redaction import redact
    except Exception:  # noqa: BLE001 - redaction must never break error reporting
        return text
    return redact(text)


def _graph_error(response: httpx.Response) -> tuple[str | None, str]:
    """Graph's ``error.code`` and ``error.message``, sanitised. Nothing else from the body."""
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return None, ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None, ""
    raw_code = str(error.get("code") or "")
    code = raw_code if _SAFE_CODE.fullmatch(raw_code) else None
    message = " ".join(str(error.get("message") or "").split())[:_MESSAGE_LIMIT]
    return code, _redact(message)


class GraphClient:
    """Async Microsoft Graph client for one app registration."""

    def __init__(
        self,
        token_source: TokenSource,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = GRAPH_BASE_URL,
        timeout: float = TIMEOUT_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        mailbox_concurrency: int = MAILBOX_CONCURRENCY,
    ) -> None:
        base = httpx.URL(base_url.rstrip("/"))
        if base.scheme != "https" or not base.host:
            raise ValueError("graph base_url must be an absolute https URL")
        self.token_source = token_source
        self._base = base
        self._base_path = base.path.rstrip("/")
        self._transport = transport
        self._timeout = timeout
        self._sleep = sleep
        self._mailbox_concurrency = mailbox_concurrency
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._client: httpx.AsyncClient | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> GraphClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _http(self) -> httpx.AsyncClient:
        # Lazily: nothing opens a socket at construction or import time.
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout, follow_redirects=False
            )
        return self._client

    # ── public API ───────────────────────────────────────────────────────

    async def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """One GET, retried on throttling. Returns the JSON object."""
        return await self._read(self._resolve(path), params, headers)

    async def get_all(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        max_items: int = DEFAULT_MAX_ITEMS,
        headers: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Every item of a collection, following same-origin nextLinks, up to ``max_items``."""
        items: list[dict[str, Any]] = []
        url = self._resolve(path)
        page_params: Mapping[str, Any] | None = params
        while True:
            page = await self._read(url, page_params, headers)
            values = page.get("value")
            if not isinstance(values, list):
                raise WorkspaceError("graph collection response carried no value list")
            for value in values:
                items.append(value)
                if len(items) >= max_items:
                    return items
            link = page.get("@odata.nextLink")
            if not link:
                return items
            url = self._check_next_link(str(link))
            page_params = None  # the nextLink already carries the query

    async def post(
        self,
        path: str,
        json: Any = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self._write("POST", self._resolve(path), json, headers)

    async def patch(
        self,
        path: str,
        json: Any = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self._write("PATCH", self._resolve(path), json, headers)

    async def delete(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self._write("DELETE", self._resolve(path), None, headers)

    # ── URLs ─────────────────────────────────────────────────────────────

    def _resolve(self, path: str) -> httpx.URL:
        """``base_url + path`` for a path that cannot leave the base."""
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise WorkspaceError("graph path must be relative to the base URL and start with '/'")
        if _FORBIDDEN_PATH.search(path) or any(ch < " " for ch in path):
            raise WorkspaceError("graph path carries a forbidden character; pass query via params")
        if any(segment in {".", ".."} for segment in unquote(path).split("/")):
            raise WorkspaceError("graph path may not contain '.' or '..' segments")
        return httpx.URL(str(self._base) + path)

    def _check_next_link(self, link: str) -> httpx.URL:
        try:
            url = httpx.URL(link)
        except Exception:  # noqa: BLE001
            raise WorkspaceError("graph returned an unparseable nextLink; refused") from None
        same_origin = (
            url.scheme == self._base.scheme
            and url.host == self._base.host
            and url.port == self._base.port
            and not url.userinfo
        )
        if not same_origin:
            raise WorkspaceError("graph returned a nextLink to a foreign origin; refused")
        return url

    def _mailbox_of(self, url: httpx.URL) -> str | None:
        path = url.raw_path.decode("ascii", "replace").split("?", 1)[0]
        if self._base_path and path.startswith(self._base_path):
            path = path[len(self._base_path) :]
        match = _MAILBOX_SEGMENT.match(path)
        return unquote(match.group(1)).strip().lower() if match else None

    def _gate(self, url: httpx.URL) -> asyncio.Semaphore | contextlib.nullcontext[None]:
        mailbox = self._mailbox_of(url)
        if mailbox is None:
            return contextlib.nullcontext()
        semaphore = self._semaphores.get(mailbox)
        if semaphore is None:
            semaphore = self._semaphores[mailbox] = asyncio.Semaphore(self._mailbox_concurrency)
        return semaphore

    # ── sending ──────────────────────────────────────────────────────────

    async def _send(
        self,
        method: str,
        url: httpx.URL,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        json: Any = None,
    ) -> httpx.Response:
        token = await self.token_source.token()
        request_id = str(uuid.uuid4())
        extra_prefer = None
        outgoing: dict[str, str] = {}
        for name, value in (headers or {}).items():
            lowered = name.lower()
            if lowered == "prefer":
                extra_prefer = value
            elif lowered not in {"authorization", "client-request-id"}:
                outgoing[name] = value
        outgoing.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Prefer": merge_prefer(DEFAULT_PREFER, extra_prefer),
                "client-request-id": request_id,
                "return-client-request-id": "true",
            }
        )
        kwargs: dict[str, Any] = {"headers": outgoing}
        if params:
            kwargs["params"] = dict(params)
        if json is not None:
            kwargs["json"] = json
        logger.debug(
            "graph %s %s client-request-id=%s",
            method,
            _MAILBOX_SEGMENT.sub("/users/{mailbox}", url.path[len(self._base_path) :]),
            request_id,
        )
        async with self._gate(url):
            return await self._http().request(method, url, **kwargs)

    async def _read(
        self,
        url: httpx.URL,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        waited = 0.0
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._send("GET", url, params=params, headers=headers)
            except httpx.TransportError as exc:
                delay = float(2 ** (attempt - 1))
                if attempt >= MAX_READ_ATTEMPTS or waited + delay > MAX_READ_WAIT_SECONDS:
                    raise WorkspaceError(f"graph transport failure: {type(exc).__name__}") from None
                await self._sleep(delay)
                waited += delay
                continue
            if response.status_code in READ_RETRY_STATUSES:
                hinted = _retry_after(response)
                delay = hinted if hinted is not None else float(2 ** (attempt - 1))
                if attempt >= MAX_READ_ATTEMPTS or waited + delay > MAX_READ_WAIT_SECONDS:
                    code, _ = _graph_error(response)
                    raise RateLimited(
                        f"graph HTTP {response.status_code}: throttled after {attempt} attempt(s)",
                        retry_after=delay,
                        status=response.status_code,
                        code=code,
                    )
                await self._sleep(delay)
                waited += delay
                continue
            if not response.is_success:
                raise self._error(response)
            try:
                payload = response.json()
            except ValueError:
                raise WorkspaceError("graph answered with an unreadable body") from None
            if not isinstance(payload, dict):
                raise WorkspaceError("graph answered with an unexpected body shape")
            return payload

    async def _write(
        self,
        method: str,
        url: httpx.URL,
        json: Any,
        headers: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        try:
            response = await self._send(method, url, params=None, headers=headers, json=json)
        except _NOT_SENT as exc:
            raise WorkspaceError(f"graph {method} was not sent: {type(exc).__name__}") from None
        except httpx.TransportError as exc:
            raise UnknownEffect(
                f"graph {method} outcome unknown after {type(exc).__name__}; read back before retrying"
            ) from None
        status = response.status_code
        if status == 429:
            code, _ = _graph_error(response)
            raise RateLimited(
                f"graph {method} throttled (HTTP 429); not retried",
                retry_after=_retry_after(response),
                status=429,
                code=code,
            )
        if status >= 500:
            code, _ = _graph_error(response)
            raise UnknownEffect(
                f"graph {method} outcome unknown after HTTP {status}"
                + (f" {code}" if code else "")
                + "; read back before retrying",
                status=status,
                code=code,
            )
        if not response.is_success:
            raise self._error(response)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError:
            raise UnknownEffect(
                f"graph {method} succeeded (HTTP {status}) with an unreadable body",
                status=status,
            ) from None
        if not isinstance(payload, dict):
            raise UnknownEffect(
                f"graph {method} succeeded (HTTP {status}) with an unexpected body shape",
                status=status,
            )
        return payload

    def _error(self, response: httpx.Response) -> WorkspaceError:
        status = response.status_code
        code, message = _graph_error(response)
        if status in (401, 403):
            # The message on an auth failure can echo the token; the code is enough.
            forget = getattr(self.token_source, "forget", None)
            if status == 401 and callable(forget):
                forget()
            text = f"graph HTTP {status}" + (f" {code}" if code else "")
            return PermissionDenied(text, status=status, code=code)
        text = (
            f"graph HTTP {status}"
            + (f" {code}" if code else "")
            + (f": {message}" if message else "")
        )
        if status == 404:
            return NotFound(text, status=status, code=code)
        if status == 412:
            return PreconditionFailed(text, status=status, code=code)
        if status == 429:
            return RateLimited(text, retry_after=_retry_after(response), status=status, code=code)
        return WorkspaceError(text, status=status, code=code)
