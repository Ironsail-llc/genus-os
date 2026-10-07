"""Microsoft 365: is the app connected, can it reach its two mailboxes, and ONLY them?

Active only on an instance that uses Microsoft 365 (``workspace_provider =
microsoft365``) or has a Microsoft 365 credential in the vault -- connected
but not yet enabled is exactly when an operator runs the doctor. On every
other instance each check skips and says the instance is on Google.

* ``workspace.m365_connection`` (required) -- the settings and the vault
  credential are there, Entra issues a token, the assistant's inbox and the
  owner's calendar answer. One row per step; a failed step skips the rest.
* ``workspace.m365_scope`` (required) -- the canary mailbox is DENIED. An app
  that can read the canary was granted more than the two mailboxes (an
  unscoped admin consent, or a management scope that never applied), which
  means the assistant can read the whole company's mail. That is an error,
  never a warning.
* ``workspace.m365_canary_configured`` (recommended) -- with no canary the
  scope is unproven, and the operator should know that.
* ``workspace.m365_timezone`` (recommended) -- the owner's Exchange timezone
  agrees with the instance's ``ROBOTHOR_TIMEZONE``, or "9 am" means two
  different things to the calendar and to the agents.

Results name mailboxes (configuration, not data) and Graph status codes.
Never a token, a message, an event or a credential.

Every Graph call leaves the box, so under ``--offline`` (the bridge's
``GET /api/doctor``) only the local configuration step runs.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, Result, fail, info, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext
    from robothor.workspace.microsoft.graph import GraphClient

logger = logging.getLogger(__name__)

__all__ = ["CHECKS", "iana_for", "probe_for_enable"]

PROVIDER = "microsoft365"
CONNECT_COMMAND = "genus workspace connect microsoft365"

_INACTIVE = (
    "workspace_provider is google and no Microsoft 365 credential is stored; nothing to check"
)
_OFFLINE = "--offline: Microsoft Graph was not contacted"

#: Windows timezone ids Exchange stores, mapped to IANA (CLDR windowsZones,
#: territory 001). Not exhaustive; an id missing here is reported as "could
#: not compare", never as a mismatch.
WINDOWS_TO_IANA: dict[str, str] = {
    "Dateline Standard Time": "Etc/GMT+12",
    "UTC-11": "Etc/GMT+11",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Alaskan Standard Time": "America/Anchorage",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time (Mexico)": "America/Mazatlan",
    "Mountain Standard Time": "America/Denver",
    "Central America Standard Time": "America/Guatemala",
    "Central Standard Time": "America/Chicago",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina",
    "SA Pacific Standard Time": "America/Bogota",
    "Eastern Standard Time": "America/New_York",
    "Eastern Standard Time (Mexico)": "America/Cancun",
    "US Eastern Standard Time": "America/Indianapolis",
    "Venezuela Standard Time": "America/Caracas",
    "Atlantic Standard Time": "America/Halifax",
    "SA Western Standard Time": "America/La_Paz",
    "Pacific SA Standard Time": "America/Santiago",
    "Newfoundland Standard Time": "America/St_Johns",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Buenos_Aires",
    "SA Eastern Standard Time": "America/Cayenne",
    "Greenland Standard Time": "America/Godthab",
    "UTC-02": "Etc/GMT+2",
    "Azores Standard Time": "Atlantic/Azores",
    "Cape Verde Standard Time": "Atlantic/Cape_Verde",
    "UTC": "Etc/UTC",
    "Coordinated Universal Time": "Etc/UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Morocco Standard Time": "Africa/Casablanca",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "GTB Standard Time": "Europe/Bucharest",
    "E. Europe Standard Time": "Europe/Chisinau",
    "Egypt Standard Time": "Africa/Cairo",
    "FLE Standard Time": "Europe/Kiev",
    "Israel Standard Time": "Asia/Jerusalem",
    "South Africa Standard Time": "Africa/Johannesburg",
    "Jordan Standard Time": "Asia/Amman",
    "Arabic Standard Time": "Asia/Baghdad",
    "Turkey Standard Time": "Europe/Istanbul",
    "Arab Standard Time": "Asia/Riyadh",
    "Russian Standard Time": "Europe/Moscow",
    "E. Africa Standard Time": "Africa/Nairobi",
    "Iran Standard Time": "Asia/Tehran",
    "Arabian Standard Time": "Asia/Dubai",
    "Afghanistan Standard Time": "Asia/Kabul",
    "Pakistan Standard Time": "Asia/Karachi",
    "West Asia Standard Time": "Asia/Tashkent",
    "India Standard Time": "Asia/Calcutta",
    "Sri Lanka Standard Time": "Asia/Colombo",
    "Nepal Standard Time": "Asia/Katmandu",
    "Central Asia Standard Time": "Asia/Almaty",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "Myanmar Standard Time": "Asia/Rangoon",
    "SE Asia Standard Time": "Asia/Bangkok",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "Taipei Standard Time": "Asia/Taipei",
    "W. Australia Standard Time": "Australia/Perth",
    "Korea Standard Time": "Asia/Seoul",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin",
    "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Tasmania Standard Time": "Australia/Hobart",
    "West Pacific Standard Time": "Pacific/Port_Moresby",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Fiji Standard Time": "Pacific/Fiji",
    "Tonga Standard Time": "Pacific/Tongatapu",
}


def iana_for(zone: str) -> str | None:
    """An IANA id for an Exchange timezone (Windows or IANA spelling), or None."""
    from zoneinfo import ZoneInfo

    name = str(zone or "").strip()
    if not name:
        return None
    if name in WINDOWS_TO_IANA:
        return WINDOWS_TO_IANA[name]
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001 - an unknown name is an answer
        return None
    return name


def _same_rules(first: str, second: str) -> bool:
    """Do two IANA zones give the same UTC offset across this year?

    ``America/Detroit`` and ``America/New_York`` are different names for the
    same clock; comparing names would warn about nothing.
    """
    from zoneinfo import ZoneInfo

    a, b = ZoneInfo(first), ZoneInfo(second)
    year = datetime.datetime.now(datetime.UTC).year
    for month in (1, 3, 4, 7, 10, 11):
        instant = datetime.datetime(year, month, 15, 12, tzinfo=datetime.UTC)
        if instant.astimezone(a).utcoffset() != instant.astimezone(b).utcoffset():
            return False
    return True


# ── seams (replaced wholesale by the suite) ──────────────────────────────────


async def _graph(tenant_id: str) -> GraphClient:
    """A Graph client from the vault. Raises ``AuthError`` when not connected."""
    from robothor.workspace.microsoft import graph_client_from_vault

    return await graph_client_from_vault(tenant_id)


def _vault_connected(tenant_id: str) -> bool:
    """Is a Microsoft 365 directory id stored? Blocking; any failure is ``False``."""
    try:
        from robothor import vault
        from robothor.vault.naming import workspace_key

        return bool(vault.get(workspace_key(PROVIDER, "tenant_id"), tenant_id=tenant_id))
    except Exception as exc:  # noqa: BLE001 - an unreadable vault is "not connected"
        logger.debug("doctor: vault unreadable for the microsoft365 check: %s", type(exc).__name__)
        return False


# ── shared steps ─────────────────────────────────────────────────────────────


def _platform_tenant(ctx: DoctorContext) -> str:
    from robothor.constants import DEFAULT_TENANT

    return ctx.settings.database.tenant_id or DEFAULT_TENANT


async def _active(ctx: DoctorContext) -> bool:
    if ctx.settings.workspace.workspace_provider == PROVIDER:
        return True
    return bool(await ctx.run_blocking(_vault_connected, _platform_tenant(ctx)))


def _mailboxes(ctx: DoctorContext) -> tuple[str, str, str]:
    ws = ctx.settings.workspace
    return (
        (ws.m365_assistant_mailbox or "").strip().lower(),
        (ws.m365_owner_mailbox or "").strip().lower(),
        (ws.m365_scope_canary_mailbox or "").strip().lower(),
    )


def _user_path(mailbox: str, rest: str) -> str:
    from urllib.parse import quote

    return f"/users/{quote(mailbox, safe='@.+-_')}{rest}"


def _explain(exc: BaseException) -> str:
    """A WorkspaceError's own (already safe) message, or the exception's type."""
    from robothor.workspace.errors import WorkspaceError

    if isinstance(exc, WorkspaceError):
        return str(exc)
    return type(exc).__name__


async def _connect(ctx: DoctorContext) -> tuple[GraphClient | None, str]:
    """``(client, "")`` or ``(None, why not)``. Reads the vault, sends nothing."""
    try:
        return await _graph(_platform_tenant(ctx)), ""
    except Exception as exc:  # noqa: BLE001 - a check reports, it does not raise
        return None, _explain(exc)


async def _read(graph: GraphClient, path: str, params: dict[str, str] | None = None) -> Any:
    return await graph.get(path, params)


# ── workspace.m365_connection ────────────────────────────────────────────────


async def _connection(ctx: DoctorContext) -> list[Result]:
    """The app can sign in and reach both of its mailboxes.

    One row per step -- ``config`` (mailboxes set, credential in the vault),
    ``token`` (Entra issues one), ``assistant_inbox`` and ``owner_calendar``
    (Graph answers 200). A failed step skips the steps after it. Fix: run
    ``genus workspace connect microsoft365`` and follow its printed steps.
    """
    if not await _active(ctx):
        return [skip(_INACTIVE)]

    steps = ("config", "token", "assistant_inbox", "owner_calendar")

    def rest(reason: str, start: int) -> list[Result]:
        return [Result(status="skip", detail=reason, sub_id=s) for s in steps[start:]]

    assistant, owner, _canary = _mailboxes(ctx)
    missing = [
        name
        for name, value in (
            ("ROBOTHOR_M365_ASSISTANT_MAILBOX", assistant),
            ("ROBOTHOR_M365_OWNER_MAILBOX", owner),
        )
        if not value
    ]
    if missing:
        return [
            fail(
                f"{', '.join(missing)} not set; run `{CONNECT_COMMAND}`",
                sub_id="config",
            ),
            *rest("configuration incomplete", 1),
        ]

    graph, why = await _connect(ctx)
    if graph is None:
        return [
            fail(f"{why}; run `{CONNECT_COMMAND}`", sub_id="config"),
            *rest("configuration incomplete", 1),
        ]
    results = [
        Result(
            status="pass",
            detail="mailboxes set and the app credential is in the vault",
            sub_id="config",
        )
    ]
    try:
        if ctx.offline:
            return results + rest(_OFFLINE, 1)
        try:
            await graph.token_source.token()
        except Exception as exc:  # noqa: BLE001
            return results + [
                fail(
                    f"Entra did not issue a token: {_explain(exc)}. Check that the certificate "
                    "printed by the connect command is uploaded to the app registration",
                    sub_id="token",
                ),
                *rest("no token", 2),
            ]
        results.append(
            Result(status="pass", detail="Entra issued an app-only token", sub_id="token")
        )

        try:
            await _read(
                graph,
                _user_path(assistant, "/mailFolders/inbox/messages"),
                {"$top": "1", "$select": "id"},
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                fail(
                    f"the app cannot read the assistant inbox {assistant} ({_explain(exc)}): "
                    "assign Mail.ReadWrite and include the mailbox in the management scope",
                    sub_id="assistant_inbox",
                )
            )
        else:
            results.append(
                Result(
                    status="pass",
                    detail=f"the assistant inbox {assistant} is readable",
                    sub_id="assistant_inbox",
                )
            )

        try:
            await _read(graph, _user_path(owner, "/calendar"), {"$select": "id"})
        except Exception as exc:  # noqa: BLE001
            results.append(
                fail(
                    f"the app cannot read the owner calendar {owner} ({_explain(exc)}): "
                    "assign Calendars.ReadWrite and include the mailbox in the management scope",
                    sub_id="owner_calendar",
                )
            )
        else:
            results.append(
                Result(
                    status="pass",
                    detail=f"the owner calendar {owner} is readable",
                    sub_id="owner_calendar",
                )
            )
        return results
    finally:
        await graph.aclose()


# ── workspace.m365_scope ─────────────────────────────────────────────────────


async def _scope(ctx: DoctorContext) -> Result:
    """The app is DENIED the canary mailbox, so its grant is scoped.

    A readable canary means the app can read mailboxes beyond the assistant
    and the owner -- an unscoped admin consent, or a management scope that was
    never applied. Fix: scope it with RBAC for Applications and remove any
    tenant-wide Mail/Calendars consent in Entra.
    """
    from robothor.workspace.errors import NotFound, PermissionDenied

    if not await _active(ctx):
        return skip(_INACTIVE)
    assistant, owner, canary = _mailboxes(ctx)
    if not canary:
        return skip(
            "no canary mailbox configured, so the scope is unproven (see "
            "workspace.m365_canary_configured)"
        )
    if canary in (assistant, owner):
        return fail(
            f"the canary {canary} must be a third mailbox the app is NOT scoped to, not the "
            "assistant or the owner; set ROBOTHOR_M365_SCOPE_CANARY_MAILBOX to another mailbox"
        )
    if ctx.offline:
        return skip(_OFFLINE)
    graph, why = await _connect(ctx)
    if graph is None:
        return skip(f"not connected ({why}); see workspace.m365_connection")
    try:
        await _read(
            graph,
            _user_path(canary, "/mailFolders/inbox/messages"),
            {"$top": "1", "$select": "id"},
        )
    except PermissionDenied as exc:
        return ok(f"the canary {canary} is denied ({exc.status or 403}): the app is scoped")
    except NotFound:
        return fail(
            f"the canary mailbox {canary} was not found, so the scope is unproven: point "
            "ROBOTHOR_M365_SCOPE_CANARY_MAILBOX at a real mailbox outside the scope"
        )
    except Exception as exc:  # noqa: BLE001
        return fail(f"could not prove the scope: reading the canary failed ({_explain(exc)})")
    finally:
        await graph.aclose()
    return fail(
        f"app scope is not restricted: it can read mailboxes beyond assistant+owner (the "
        f"canary {canary} is readable). Remove any tenant-wide Mail/Calendars consent in "
        "Entra and scope the app with RBAC for Applications; see docs/workspace/microsoft365.md"
    )


async def _canary_configured(ctx: DoctorContext) -> Result:
    """A canary mailbox is configured; without one the app's scope is unproven."""
    if not await _active(ctx):
        return skip(_INACTIVE)
    _assistant, _owner, canary = _mailboxes(ctx)
    if not canary:
        return fail(
            "no canary mailbox: the app's scope is unproven. Set "
            "ROBOTHOR_M365_SCOPE_CANARY_MAILBOX to a mailbox the app must not read "
            f"(`{CONNECT_COMMAND} --canary-mailbox ...`)"
        )
    return ok(f"canary mailbox {canary} is configured")


# ── workspace.m365_timezone ──────────────────────────────────────────────────


async def _timezone(ctx: DoctorContext) -> Result:
    """The owner's Exchange timezone keeps the same clock as ROBOTHOR_TIMEZONE.

    Exchange usually stores a Windows zone name; it is mapped to IANA and
    compared by UTC offset across the year. A zone that cannot be mapped, or
    mailbox settings the app may not read, is reported and never failed.
    """
    from robothor.workspace.errors import PermissionDenied

    if not await _active(ctx):
        return skip(_INACTIVE)
    _assistant, owner, _canary = _mailboxes(ctx)
    if not owner:
        return skip("no owner mailbox configured; see workspace.m365_connection")
    if ctx.offline:
        return skip(_OFFLINE)
    graph, why = await _connect(ctx)
    if graph is None:
        return skip(f"not connected ({why}); see workspace.m365_connection")
    try:
        answer = await _read(graph, _user_path(owner, "/mailboxSettings/timeZone"))
    except PermissionDenied:
        return info(
            "the owner's mailbox timezone is not readable; assign MailboxSettings.Read "
            "(optional) to compare it with ROBOTHOR_TIMEZONE"
        )
    except Exception as exc:  # noqa: BLE001
        return info(f"the owner's mailbox timezone could not be read ({_explain(exc)})")
    finally:
        await graph.aclose()

    mailbox_zone = str((answer or {}).get("value") or "").strip()
    configured = str(ctx.settings.engine.timezone or "").strip()
    mapped = iana_for(mailbox_zone)
    if not mapped or not iana_for(configured):
        return info(
            f"could not compare the owner's mailbox timezone {mailbox_zone or '(unset)'!r} "
            f"with ROBOTHOR_TIMEZONE {configured!r}"
        )
    if _same_rules(mapped, configured):
        return ok(
            f"the owner's mailbox timezone {mailbox_zone} matches ROBOTHOR_TIMEZONE {configured}"
        )
    return fail(
        f"the owner's mailbox timezone is {mailbox_zone} ({mapped}) but ROBOTHOR_TIMEZONE is "
        f"{configured}: times the agents state and times the calendar shows will disagree. "
        "Change one of them"
    )


# ── the --enable gate ────────────────────────────────────────────────────────


async def probe_for_enable(ctx: DoctorContext) -> tuple[bool, list[str]]:
    """Run the required checks the way ``connect --enable`` needs them.

    Passes only when every connection step passes AND a configured canary is
    denied: enabling an unscoped (or unproven) app would hand the assistant
    the whole tenant's mail.
    """
    lines: list[str] = []
    passed = True
    for row in await _connection(ctx):
        lines.append(f"{row.status:4}  connection:{row.sub_id or '-'}  {row.detail}")
        passed = passed and row.status == "pass"
    scope = await _scope(ctx)
    lines.append(f"{scope.status:4}  scope  {scope.detail}")
    passed = passed and scope.status == "pass"
    return passed, lines


CHECKS: tuple[Check, ...] = (
    Check(
        id="workspace.m365_connection",
        title="Microsoft 365: credential, token, assistant inbox, owner calendar",
        category="workspace",
        severity="required",
        run=_connection,
    ),
    Check(
        id="workspace.m365_scope",
        title="Microsoft 365: the app cannot read the canary mailbox",
        category="workspace",
        severity="required",
        run=_scope,
    ),
    Check(
        id="workspace.m365_canary_configured",
        title="Microsoft 365: a canary mailbox proves the app's scope",
        category="workspace",
        severity="recommended",
        run=_canary_configured,
    ),
    Check(
        id="workspace.m365_timezone",
        title="Microsoft 365: the owner's mailbox timezone matches the instance",
        category="workspace",
        severity="recommended",
        run=_timezone,
    ),
)
