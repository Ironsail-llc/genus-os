"""Read-only Datadog clients that only ever send a tenant-scoped query.

:class:`DatadogClient` (sync, for bridge handlers) and
:class:`AsyncDatadogClient` (async, for the engine) take a
:class:`~.scope.DatadogScope` with every call and compose the query
themselves, so no caller can send an unscoped one. A refused query raises
:class:`~.scope.ScopeError` before any request is built.

The keys are credentials and are kept out of everything printable, as in
:mod:`robothor.integrations.n8n.client`: they are sent per request rather than
stored on an ``httpx`` client, held in a wrapper whose ``repr`` is a mask, and
a failure raises :class:`DatadogError` carrying a kind and an HTTP status --
never a URL, a header or a vendor body. The ``httpx`` exception is suppressed
(``raise ... from None``) because its text and request can carry the headers.

The host is built from ``ROBOTHOR_DATADOG_SITE`` and must be a known Datadog
site; anything else is ``not_configured``, so a key is never sent elsewhere.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, Literal

import httpx

from robothor.constants import DEFAULT_TENANT
from robothor.integrations.datadog import requests as api
from robothor.integrations.datadog.names import API_KEY_ENV, APP_KEY_ENV
from robothor.integrations.datadog.requests import Bucket, LogPage, LogRow
from robothor.integrations.datadog.scope import DatadogScope, compose

logger = logging.getLogger(__name__)

__all__ = [
    "API_KEY_ENV",
    "APP_KEY_ENV",
    "AsyncDatadogClient",
    "Bucket",
    "DatadogClient",
    "DatadogError",
    "ErrorKind",
    "LogPage",
    "LogRow",
    "async_client_from_settings",
    "client_from_settings",
    "resolve_keys",
]

TIMEOUT_SECONDS = 20.0

ErrorKind = Literal["auth", "rate_limit", "network", "bad_query", "unknown", "not_configured"]


class DatadogError(Exception):
    """A Datadog call failed. ``kind`` and ``status`` are the whole explanation."""

    def __init__(self, kind: ErrorKind, status: int | None = None, detail: str = "") -> None:
        self.kind: ErrorKind = kind
        self.status = status
        message = f"{kind} (HTTP {status})" if status is not None else kind
        super().__init__(f"{message}: {detail}" if detail else message)

    def __repr__(self) -> str:
        return f"DatadogError(kind={self.kind!r}, status={self.status!r})"


def _classify(status: int) -> ErrorKind:
    if status == 400:
        return "bad_query"
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "network"
    return "unknown"


class _Key:
    """A credential whose ``repr`` and ``str`` are a mask."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


def _body(response: httpx.Response) -> Any:
    if response.status_code != 200:
        raise DatadogError(_classify(response.status_code), response.status_code)
    try:
        return response.json()
    except ValueError:
        raise DatadogError("unknown", response.status_code) from None


def _search(
    scope: DatadogScope,
    query: str,
    start: str,
    end: str,
    limit: int,
    cursor: str | None,
    newest_first: bool,
) -> api.ApiRequest:
    """The log search for ``query`` inside ``scope``; raises ScopeError before any I/O."""
    return api.logs_search(
        compose(scope.logs, query),
        start=start,
        end=end,
        limit=limit,
        cursor=cursor,
        newest_first=newest_first,
    )


def _aggregate(
    scope: DatadogScope, query: str, start: str, end: str, group_by: Sequence[str], limit: int
) -> api.ApiRequest:
    """The log aggregate for ``query`` inside ``scope``; raises ScopeError before any I/O."""
    return api.logs_aggregate(
        compose(scope.logs, query), start=start, end=end, group_by=group_by, limit=limit
    )


class _Base:
    def __init__(
        self,
        api_key: str,
        app_key: str,
        *,
        site: str = "datadoghq.com",
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        base = api.api_base(site)
        if base is None:
            raise DatadogError("not_configured", detail="ROBOTHOR_DATADOG_SITE is not a known site")
        if not (api_key or "").strip() or not (app_key or "").strip():
            raise DatadogError(
                "not_configured", detail="the Datadog API or application key is empty"
            )
        self.site = base.removeprefix("https://api.")
        self._base = base
        self._keys = (_Key(api_key.strip()), _Key(app_key.strip()))
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"{type(self).__name__}(site={self.site!r})"

    __str__ = __repr__

    def _headers(self) -> dict[str, str]:
        api_key, app_key = self._keys
        return {
            "DD-API-KEY": api_key.value,
            "DD-APPLICATION-KEY": app_key.value,
            "Accept": "application/json",
        }

    @staticmethod
    def _failed(request: api.ApiRequest, exc: Exception) -> DatadogError:
        # The TYPE only: the text and the request can both carry the keys.
        logger.info("datadog: %s failed (%s)", request.path, type(exc).__name__)
        return DatadogError("network")


class DatadogClient(_Base):
    """Sync client; a short-lived ``httpx.Client`` per call."""

    def __init__(
        self,
        api_key: str,
        app_key: str,
        *,
        site: str = "datadoghq.com",
        transport: httpx.BaseTransport | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(api_key, app_key, site=site, timeout=timeout)
        self._transport = transport

    def _send(self, request: api.ApiRequest) -> Any:
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as http:
                response = http.request(
                    request.method,
                    f"{self._base}{request.path}",
                    json=request.body,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise self._failed(request, exc) from None
        return _body(response)

    def search_logs(
        self,
        scope: DatadogScope,
        query: str,
        *,
        start: str,
        end: str,
        limit: int = 50,
        cursor: str | None = None,
        newest_first: bool = True,
    ) -> LogPage:
        request = _search(scope, query, start, end, limit, cursor, newest_first)
        return api.parse_log_page(self._send(request))

    def aggregate_logs(
        self,
        scope: DatadogScope,
        query: str,
        *,
        start: str,
        end: str,
        group_by: Sequence[str],
        limit: int = 25,
    ) -> tuple[Bucket, ...]:
        request = _aggregate(scope, query, start, end, group_by, limit)
        return api.parse_buckets(self._send(request))

    def validate(self) -> None:
        """Raises :class:`DatadogError` unless Datadog accepts the API key."""
        self._send(api.validate_request())

    def validate_app_key(self) -> None:
        """Raises :class:`DatadogError` unless the application key may read logs."""
        self._send(api.app_key_probe_request())


class AsyncDatadogClient(_Base):
    """Async client over the shared pooled ``httpx.AsyncClient`` (``robothor.http_clients``)."""

    def __init__(
        self,
        api_key: str,
        app_key: str,
        *,
        site: str = "datadoghq.com",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(api_key, app_key, site=site, timeout=timeout)
        self._transport = transport

    async def _send(self, request: api.ApiRequest) -> Any:
        from robothor.http_clients import shared_async_client

        http = shared_async_client("datadog", transport=self._transport)
        try:
            response = await http.request(
                request.method,
                f"{self._base}{request.path}",
                json=request.body,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise self._failed(request, exc) from None
        return _body(response)

    async def search_logs(
        self,
        scope: DatadogScope,
        query: str,
        *,
        start: str,
        end: str,
        limit: int = 50,
        cursor: str | None = None,
        newest_first: bool = True,
    ) -> LogPage:
        request = _search(scope, query, start, end, limit, cursor, newest_first)
        return api.parse_log_page(await self._send(request))

    async def aggregate_logs(
        self,
        scope: DatadogScope,
        query: str,
        *,
        start: str,
        end: str,
        group_by: Sequence[str],
        limit: int = 25,
    ) -> tuple[Bucket, ...]:
        request = _aggregate(scope, query, start, end, group_by, limit)
        return api.parse_buckets(await self._send(request))

    async def validate(self) -> None:
        """Raises :class:`DatadogError` unless Datadog accepts the API key."""
        await self._send(api.validate_request())

    async def validate_app_key(self) -> None:
        """Raises :class:`DatadogError` unless the application key may read logs."""
        await self._send(api.app_key_probe_request())


# ── from settings ───────────────────────────────────────────────────────────


def _get_secret(name: str, tenant_id: str) -> str | None:
    from robothor.secrets import get_secret

    return get_secret(name, tenant_id=tenant_id)


SecretLookup = Callable[[str, str], "str | None"]


def resolve_keys(settings: Any = None, *, secret: SecretLookup | None = None) -> tuple[str, str]:
    """The (API, application) keys, vault-first, or empty strings. Blocking.

    Instance-wide on purpose: one read-only key serves every tenant, and the
    wall between tenants is the scope composed into each query, not the key.
    So the vault row read is always the ROOT tenant's, whichever tenant asks;
    a child tenant storing its own key changes nothing.
    """
    from robothor.settings import get_settings

    group = (settings or get_settings()).integrations
    lookup = secret or _get_secret
    api_key = lookup(API_KEY_ENV, DEFAULT_TENANT) or (group.datadog_api_key or "")
    app_key = lookup(APP_KEY_ENV, DEFAULT_TENANT) or (group.datadog_app_key or "")
    return api_key.strip(), app_key.strip()


def client_from_settings(
    settings: Any = None,
    *,
    secret: SecretLookup | None = None,
    transport: httpx.BaseTransport | None = None,
) -> DatadogClient:
    """A sync client for the configured account. Blocking (the vault read).

    Raises ``DatadogError("not_configured")`` when a key is missing or the
    site is not a known Datadog site.
    """
    from robothor.settings import get_settings

    settings = settings or get_settings()
    api_key, app_key = resolve_keys(settings, secret=secret)
    site = settings.integrations.datadog_site
    return DatadogClient(api_key, app_key, site=site, transport=transport)


def async_client_from_settings(
    settings: Any = None,
    *,
    secret: SecretLookup | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncDatadogClient:
    """An async client for the configured account.

    Blocking (the vault read): call it through :func:`asyncio.to_thread` from
    async code. Raises like :func:`client_from_settings`.
    """
    from robothor.settings import get_settings

    settings = settings or get_settings()
    api_key, app_key = resolve_keys(settings, secret=secret)
    site = settings.integrations.datadog_site
    return AsyncDatadogClient(api_key, app_key, site=site, transport=transport)
