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
from typing import TYPE_CHECKING

from robothor.workspace.errors import Unsupported
from robothor.workspace.protocols import Workspace

if TYPE_CHECKING:
    from robothor.workspace.protocols import CalendarProvider, MailProvider

__all__ = ["Workspace", "get_workspace"]


@lru_cache(maxsize=1)
def _google() -> Workspace:
    from robothor.workspace.google.adapter import GoogleCalendar, GoogleMail
    from robothor.workspace.types import GOOGLE_CAPABILITIES

    mail: MailProvider = GoogleMail()
    calendar: CalendarProvider = GoogleCalendar()
    return Workspace(
        provider="google", mail=mail, calendar=calendar, capabilities=GOOGLE_CAPABILITIES
    )


def get_workspace(tenant_id: str | None = None) -> Workspace:
    """The mail and calendar providers behind the ``gws_*`` tools.

    Chosen by the ``workspace_provider`` setting. Google (the default) is
    stateless, so one shared instance serves every tenant. ``microsoft365`` is
    DARK until its mail and calendar transports ship: selecting it refuses
    every mail and calendar call with :class:`Unsupported` rather than quietly
    serving Google -- an operator who chose Outlook must not get Gmail.
    ``tenant_id`` is for the Microsoft 365 credential lookup.
    """
    from robothor.settings import get_settings

    provider = get_settings().workspace.workspace_provider
    if provider == "google":
        return _google()
    raise Unsupported(
        f"{provider} mail and calendar are not available yet; "
        "set workspace_provider=google to use the gws tools",
        code="provider_not_available",
    )
