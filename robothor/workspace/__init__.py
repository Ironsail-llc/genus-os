"""Provider-neutral workspace layer: mail and calendar over Google or Microsoft 365.

The ``gws_*`` tools keep their names and their guards; which backend serves
them is the ``workspace_provider`` setting (``google`` by default,
``microsoft365`` opt-in). This package holds the shared error vocabulary
(:mod:`robothor.workspace.errors`) and the per-provider transports
(:mod:`robothor.workspace.microsoft` for Microsoft Graph: mail in
:mod:`robothor.workspace.microsoft.mail`). See
``docs/workspace/microsoft365.md``.

:func:`get_workspace` returns the :class:`Workspace` bundle (mail + calendar
providers, :mod:`robothor.workspace.protocols`) the handlers call for
transport; the shapes are in :mod:`robothor.workspace.types`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

from robothor.workspace.errors import Unsupported
from robothor.workspace.protocols import Workspace

if TYPE_CHECKING:
    from robothor.workspace.microsoft.graph import GraphClient
    from robothor.workspace.protocols import CalendarProvider, MailProvider

__all__ = ["Workspace", "get_workspace", "reset_workspace_cache"]

#: Why the Microsoft 365 calendar refuses until its transport ships.
M365_CALENDAR_DARK = (
    "microsoft365 calendar is not available yet; the gws_calendar_* tools refuse "
    "under workspace_provider=microsoft365 (mail is available)"
)


@lru_cache(maxsize=1)
def _google() -> Workspace:
    from robothor.workspace.google.adapter import GoogleCalendar, GoogleMail
    from robothor.workspace.types import GOOGLE_CAPABILITIES

    mail: MailProvider = GoogleMail()
    calendar: CalendarProvider = GoogleCalendar()
    return Workspace(
        provider="google", mail=mail, calendar=calendar, capabilities=GOOGLE_CAPABILITIES
    )


class _DarkCalendar:
    """The Microsoft 365 calendar until it ships: every call refuses."""

    def __init__(self) -> None:
        from robothor.workspace.types import MICROSOFT365_CAPABILITIES

        self.capabilities = MICROSOFT365_CAPABILITIES

    def __getattr__(self, name: str) -> Any:
        raise Unsupported(M365_CALENDAR_DARK, code="provider_not_available")


@lru_cache(maxsize=16)
def _microsoft365(tenant_id: str, mailbox: str) -> Workspace:
    """One provider bundle per platform tenant and mailbox: it caches a Graph client."""
    from robothor.workspace.microsoft.mail import GraphMail
    from robothor.workspace.types import MICROSOFT365_CAPABILITIES

    async def graph() -> GraphClient:
        # Late-bound so the credential is read from the vault when first used.
        from robothor.workspace import microsoft

        return await microsoft.graph_client_from_vault(tenant_id)

    calendar: Any = _DarkCalendar()
    return Workspace(
        provider="microsoft365",
        mail=GraphMail(mailbox, graph_factory=graph),
        calendar=calendar,
        capabilities=MICROSOFT365_CAPABILITIES,
        unavailable={"calendar": M365_CALENDAR_DARK},
    )


def reset_workspace_cache() -> None:
    """Forget cached providers (after a settings or credential change; tests)."""
    _microsoft365.cache_clear()


def get_workspace(tenant_id: str | None = None) -> Workspace:
    """The mail and calendar providers behind the ``gws_*`` tools.

    Chosen by the ``workspace_provider`` setting. Google (the default) is
    stateless, so one shared instance serves every tenant. ``microsoft365``
    serves mail from the ``m365_assistant_mailbox`` through Microsoft Graph,
    authenticated from platform tenant ``tenant_id``'s vault; its calendar is
    DARK until that transport ships (``Workspace.unavailable``), and refuses
    rather than quietly serving Google -- an operator who chose Outlook must
    not get Gmail.
    """
    from robothor.constants import DEFAULT_TENANT
    from robothor.settings import get_settings

    settings = get_settings().workspace
    provider = settings.workspace_provider
    if provider == "google":
        return _google()
    if provider == "microsoft365":
        mailbox = (settings.m365_assistant_mailbox or "").strip()
        if not mailbox:
            raise Unsupported(
                "microsoft365 mail needs the assistant's mailbox; set m365_assistant_mailbox "
                "(ROBOTHOR_M365_ASSISTANT_MAILBOX)",
                code="not_configured",
            )
        return _microsoft365(tenant_id or DEFAULT_TENANT, mailbox)
    raise Unsupported(f"unknown workspace provider {provider!r}", code="provider_not_available")
