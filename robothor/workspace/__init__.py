"""Provider-neutral workspace layer: mail and calendar over Google or Microsoft 365.

The ``gws_*`` tools keep their names and their guards; which backend serves
them is the ``workspace_provider`` setting (``google`` by default,
``microsoft365`` opt-in). This package holds the shared error vocabulary
(:mod:`robothor.workspace.errors`) and the per-provider transports
(:mod:`robothor.workspace.microsoft` for Microsoft Graph). See
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
    from robothor.workspace.protocols import CalendarProvider, MailProvider

__all__ = ["UnavailableMail", "Workspace", "get_workspace", "reset_workspace_cache"]


@lru_cache(maxsize=1)
def _google() -> Workspace:
    from robothor.workspace.google.adapter import GoogleCalendar, GoogleMail
    from robothor.workspace.types import GOOGLE_CAPABILITIES

    mail: MailProvider = GoogleMail()
    calendar: CalendarProvider = GoogleCalendar()
    return Workspace(
        provider="google", mail=mail, calendar=calendar, capabilities=GOOGLE_CAPABILITIES
    )


class UnavailableMail:
    """Mail on a provider whose mail transport has not shipped: every call refuses.

    ``available`` is False, so the ``gws_*`` handler refuses a mail tool before
    any guard runs, the same way a whole dark provider is refused.
    """

    available = False

    def __init__(self, provider: str) -> None:
        self.provider = provider

    def _refuse(self) -> Unsupported:
        return Unsupported(
            f"{self.provider} mail is not available yet; calendar tools work, mail "
            "tools need workspace_provider=google for now",
            code="provider_not_available",
        )

    async def search(self, query: Any, *, max_results: int) -> dict[str, Any]:
        raise self._refuse()

    async def get_message(self, message_id: str, *, fmt: str) -> dict[str, Any]:
        raise self._refuse()

    async def get_thread(self, thread_id: str, *, fmt: str) -> dict[str, Any]:
        raise self._refuse()

    async def send(self, raw: str, *, thread_id: str | None = None) -> dict[str, Any]:
        raise self._refuse()

    async def reply(self, raw: str, *, thread_id: str) -> dict[str, Any]:
        raise self._refuse()

    async def modify(
        self, message_id: str, *, add_labels: Any, remove_labels: Any
    ) -> dict[str, Any]:
        raise self._refuse()

    def shape_envelope(
        self, raw: dict[str, Any], *, max_header_chars: int | None = None
    ) -> dict[str, Any]:
        raise self._refuse()

    def shape_message(self, raw: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
        raise self._refuse()


@lru_cache(maxsize=32)
def _microsoft365(tenant_id: str, assistant_mailbox: str, owner_mailbox: str) -> Workspace:
    """One Microsoft 365 bundle per platform tenant and mailbox configuration.

    The Graph client is built on first use from the tenant's vault (and once
    per event loop), so constructing this never touches the network.
    """
    from robothor.workspace.microsoft.calendar import GraphCalendar
    from robothor.workspace.types import MICROSOFT365_CAPABILITIES

    async def graph() -> Any:
        # Looked up at call time: the seam tests replace.
        from robothor.workspace import microsoft

        return await microsoft.graph_client_from_vault(tenant_id)

    try:
        calendar: CalendarProvider = GraphCalendar(
            graph, assistant_mailbox=assistant_mailbox, owner_mailbox=owner_mailbox
        )
    except ValueError as exc:
        raise Unsupported(f"microsoft365 is misconfigured: {exc}", code="not_configured") from None
    mail: MailProvider = UnavailableMail("microsoft365")
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
    serves CALENDAR through Microsoft Graph for platform tenant ``tenant_id``
    (its app credential is in that tenant's vault); its mail is not built yet
    and refuses (:class:`UnavailableMail`) rather than quietly serving Google
    -- an operator who chose Outlook must not get Gmail. Without an assistant
    mailbox configured, every Microsoft 365 call is refused.
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
