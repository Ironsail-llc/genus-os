"""Fixtures for the live Microsoft 365 smoke suite. See ``harness.py`` for the guards.

Nothing here runs unless the module asked for a live fixture AND
``harness.skip_reason()`` is ``None``: the first fixture every live test pulls
in (:func:`live_config`) skips otherwise, before a credential is read or a
socket is opened.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from robothor.workspace.tests.live.harness import (
    CapturingTransport,
    FencedTransport,
    LiveConfig,
    LiveGuardError,
    load_config,
    new_run_tag,
    require_allowed_tenant,
    require_allowed_token,
    skip_reason,
)

logger = logging.getLogger(__name__)

_CURRENT_TEST: contextvars.ContextVar[str] = contextvars.ContextVar("live_test", default="session")


def _refuse(exc: LiveGuardError) -> None:
    pytest.fail(str(exc), pytrace=False)


@pytest.fixture(scope="session")
def live_config() -> LiveConfig:
    reason = skip_reason()
    if reason is not None:
        pytest.skip(reason)
    try:
        return load_config()
    except LiveGuardError as exc:
        _refuse(exc)
        raise  # unreachable; pytest.fail raises


@pytest.fixture(scope="session")
def live_credentials(live_config: LiveConfig) -> Any:
    """The app credential, from the vault or from files. Allowlist-checked before any request."""
    from robothor.workspace.microsoft import load_credentials
    from robothor.workspace.microsoft.auth import ClientCredentials

    if live_config.uses_vault:
        creds = asyncio.run(load_credentials(live_config.platform_tenant))
    else:
        cert_pem = Path(live_config.cert_path).read_text()
        key_pem = Path(live_config.key_path).read_text() if live_config.key_path else None
        creds = ClientCredentials(
            tenant_id=live_config.tenant_id,
            client_id=live_config.client_id,
            certificate_pem=cert_pem,
            private_key_pem=key_pem,
            client_secret=None,
        ).validated()
    if not creds.certificate_pem:
        _refuse(LiveGuardError("refusing to run: the live suite proves certificate sign-in only"))
    try:
        require_allowed_tenant(creds.tenant_id)
    except LiveGuardError as exc:
        _refuse(exc)
    return creds


@pytest.fixture(scope="session")
def run_tag(live_config: LiveConfig, live_credentials: Any) -> Any:
    """This run's tag. At session end, whatever still carries it is deleted from both mailboxes."""
    tag = new_run_tag()
    yield tag
    asyncio.run(_sweep(live_config, live_credentials, tag))


@pytest.fixture(scope="session")
def capture_scrubber(live_config: LiveConfig, live_credentials: Any) -> Any:
    if not live_config.capture_dir:
        return None
    from robothor.workspace.microsoft.capture import Scrubber

    forbidden = [live_credentials.tenant_id, live_credentials.client_id]
    forbidden += sorted({m.partition("@")[2] for m in live_config.readable})
    return Scrubber(
        salt=secrets.token_bytes(32), mailboxes=live_config.placeholders(), forbidden=forbidden
    )


@pytest.fixture(autouse=True)
def _label_capture(request: pytest.FixtureRequest) -> Any:
    token = _CURRENT_TEST.set(request.node.name)
    yield
    _CURRENT_TEST.reset(token)


def build_transport(config: LiveConfig, scrubber: Any) -> FencedTransport:
    inner: httpx.AsyncBaseTransport = httpx.AsyncHTTPTransport(retries=0)
    if scrubber is not None:
        inner = CapturingTransport(inner, config.capture_dir, scrubber, label=_CURRENT_TEST.get)
    return FencedTransport(inner, config)


GraphFactory = Callable[..., Awaitable[Any]]


@pytest.fixture
async def graph_factory(
    live_config: LiveConfig, live_credentials: Any, capture_scrubber: Any
) -> AsyncIterator[GraphFactory]:
    """``await make(algorithm="RS256")`` -> a fenced :class:`GraphClient` whose token's tid is allowlisted."""
    from robothor.workspace.microsoft import graph_client_from_vault
    from robothor.workspace.microsoft.auth import ClientCredentialTokenSource
    from robothor.workspace.microsoft.graph import GraphClient

    clients: list[Any] = []

    async def make(*, algorithm: str = "RS256") -> Any:
        transport = build_transport(live_config, capture_scrubber)
        if live_config.uses_vault and algorithm == "RS256":
            # The production path, verbatim: the platform tenant's vault rows.
            client = await graph_client_from_vault(live_config.platform_tenant, transport=transport)
        else:

            async def loader() -> Any:
                return live_credentials

            source = ClientCredentialTokenSource.from_loader(
                loader, transport=transport, assertion_algorithm=algorithm
            )
            client = GraphClient(source, transport=transport)
        clients.append(client)
        try:
            require_allowed_token(await client.token_source.token())
        except LiveGuardError as exc:
            _refuse(exc)
        client.fence = transport  # type: ignore[attr-defined]
        return client

    yield make
    for client in clients:
        with contextlib.suppress(Exception):
            await client.aclose()


@pytest.fixture
async def graph(graph_factory: GraphFactory) -> Any:
    return await graph_factory()


@pytest.fixture
async def janitor(graph: Any) -> AsyncIterator[list[str]]:
    """Paths to DELETE after the test (messages and events it created). Best effort."""
    paths: list[str] = []
    yield paths
    for path in reversed(paths):
        try:
            await graph.delete(path)
        except Exception as exc:  # noqa: BLE001 - cleanup reports, it does not fail the test
            logger.warning("live cleanup: could not delete one item (%s)", type(exc).__name__)


async def _sweep(config: LiveConfig, creds: Any, tag: str) -> None:
    from robothor.workspace.microsoft.auth import ClientCredentialTokenSource
    from robothor.workspace.microsoft.graph import GraphClient

    transport = build_transport(config, None)

    async def loader() -> Any:
        return creds

    async with GraphClient(
        ClientCredentialTokenSource.from_loader(loader, transport=transport), transport=transport
    ) as graph:
        now = datetime.now(UTC)
        for mailbox in sorted(config.writable):
            base = f"/users/{mailbox}"
            with contextlib.suppress(Exception):
                for item in await graph.get_all(
                    f"{base}/messages",
                    {
                        "$filter": f"receivedDateTime ge {(now - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}",
                        "$select": "id,subject",
                        "$top": "100",
                    },
                    max_items=1000,
                ):
                    if tag in str(item.get("subject") or ""):
                        with contextlib.suppress(Exception):
                            await graph.delete(f"{base}/messages/{quote(str(item['id']), safe='')}")
            with contextlib.suppress(Exception):
                for item in await graph.get_all(
                    f"{base}/calendar/calendarView",
                    {
                        "startDateTime": (now - timedelta(days=3)).isoformat(),
                        "endDateTime": (now + timedelta(days=60)).isoformat(),
                        "$select": "id,subject,seriesMasterId",
                        "$top": "100",
                    },
                    max_items=1000,
                ):
                    if tag in str(item.get("subject") or ""):
                        target = item.get("seriesMasterId") or item["id"]
                        with contextlib.suppress(Exception):
                            await graph.delete(f"{base}/events/{quote(str(target), safe='')}")
