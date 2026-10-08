"""Provider-neutral workspace layer: mail and calendar over Google or Microsoft 365.

The ``gws_*`` tools keep their names and their guards; which backend serves
them is the ``workspace_provider`` setting (``google`` by default,
``microsoft365`` opt-in). This package holds the shared error vocabulary
(:mod:`robothor.workspace.errors`) and the per-provider transports
(:mod:`robothor.workspace.microsoft` for Microsoft Graph: mail in
:mod:`robothor.workspace.microsoft.mail`, calendar in
:mod:`robothor.workspace.microsoft.calendar`). See
``docs/workspace/microsoft365.md``.

:func:`get_workspace` returns the :class:`Workspace` bundle (mail + calendar
providers, :mod:`robothor.workspace.protocols`) the handlers call for
transport; the shapes are in :mod:`robothor.workspace.types`.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from robothor.workspace.errors import Unsupported
from robothor.workspace.protocols import Workspace

if TYPE_CHECKING:
    from robothor.workspace.microsoft.graph import GraphClient
    from robothor.workspace.protocols import CalendarProvider, MailProvider

__all__ = ["Workspace", "get_workspace", "reset_workspace_cache"]


@lru_cache(maxsize=1)
def _google() -> Workspace:
    from robothor.workspace.google.adapter import GoogleCalendar, GoogleMail
    from robothor.workspace.types import GOOGLE_CAPABILITIES

    mail: MailProvider = GoogleMail()
    calendar: CalendarProvider = GoogleCalendar()
    return Workspace(
        provider="google", mail=mail, calendar=calendar, capabilities=GOOGLE_CAPABILITIES
    )


@lru_cache(maxsize=32)
def _microsoft365(tenant_id: str, assistant_mailbox: str, owner_mailbox: str) -> Workspace:
    """One Microsoft 365 bundle per platform tenant and mailbox configuration.

    Mail and calendar share ONE Graph client, built on first use from the
    tenant's vault (and once per event loop: an httpx client is bound to the
    loop it was made on), so constructing this never touches the network.
    The owner mailbox is part of the cache key, so a settings change rebuilds.
    """
    from robothor.workspace.microsoft.calendar import GraphCalendar
    from robothor.workspace.microsoft.mail import GraphMail
    from robothor.workspace.types import MICROSOFT365_CAPABILITIES

    shared: dict[str, Any] = {}

    async def graph() -> GraphClient:
        loop = asyncio.get_running_loop()
        if shared.get("loop") is not loop:
            # Looked up at call time: the seam tests replace it, and the
            # credential is read from the vault only when first used.
            from robothor.workspace import microsoft

            client = await microsoft.graph_client_from_vault(tenant_id)
            if shared.get("loop") is not loop:
                shared["loop"], shared["client"] = loop, client
        client_for_loop: GraphClient = shared["client"]
        return client_for_loop

    try:
        calendar: CalendarProvider = GraphCalendar(
            graph, assistant_mailbox=assistant_mailbox, owner_mailbox=owner_mailbox
        )
        mail: MailProvider = GraphMail(assistant_mailbox, graph_factory=graph)
    except ValueError as exc:
        raise Unsupported(f"microsoft365 is misconfigured: {exc}", code="not_configured") from None
    return Workspace(
        provider="microsoft365",
        mail=mail,
        calendar=calendar,
        capabilities=MICROSOFT365_CAPABILITIES,
    )


def reset_workspace_cache() -> None:
    """Forget built providers (after a settings change, and between tests)."""
    _microsoft365.cache_clear()


def get_workspace(tenant_id: str | None = None) -> Workspace:
    """The mail and calendar providers behind the ``gws_*`` tools.

    Chosen by the ``workspace_provider`` setting. Google (the default) is
    stateless, so one shared instance serves every tenant. ``microsoft365``
    serves mail and calendar from the ``m365_assistant_mailbox`` through
    Microsoft Graph for platform tenant ``tenant_id`` (its app credential is
    in that tenant's vault). It never falls back to Google -- an operator who
    chose Outlook must not get Gmail: without an assistant mailbox configured,
    every Microsoft 365 call is refused as not configured.
    """
    from robothor.constants import DEFAULT_TENANT
    from robothor.settings import get_settings

    settings = get_settings().workspace
    provider = settings.workspace_provider
    if provider == "google":
        return _google()
    if provider == "microsoft365":
        assistant = settings.m365_assistant_mailbox.strip().lower()
        if not assistant:
            raise Unsupported(
                "microsoft365 needs m365_assistant_mailbox (ROBOTHOR_M365_ASSISTANT_MAILBOX) "
                "set to the assistant's Exchange Online mailbox",
                code="not_configured",
            )
        return _microsoft365(
            tenant_id or DEFAULT_TENANT, assistant, settings.m365_owner_mailbox.strip().lower()
        )
    raise Unsupported(
        f"{provider} mail and calendar are not available; "
        "set workspace_provider=google to use the gws tools",
        code="provider_not_available",
    )
