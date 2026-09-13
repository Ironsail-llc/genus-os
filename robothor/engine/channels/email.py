"""Email as a place output goes — with the opt-out checked before it leaves.

Until now email existed on this platform only as an agent *tool*
(``gws_gmail_send``). A manifest saying ``delivery.channel: email`` resolved to
nothing and the run recorded ``failed:no_channel:email``, so the one surface
most instances already have was the one surface a scheduled agent could not
announce on.

The guard is the point
----------------------
``crm_people.do_not_contact`` (migration 113) is a legal obligation, not a
preference, and its column comment says so: *email pipelines MUST check this
before sending*. The tool path checks it. A delivery channel that did not would
be a second door out of the same building, and it would be the door nobody
audited.

So the check runs **before a transport object exists** — before the gws binary
is probed, before an SMTP socket is opened. The refusal is a receipt
(``failed:email_dnc``), the evidence is a row in ``agent_guardrail_events``, and
the two failure modes that would quietly disarm it are named rather than
collapsed:

* An opt-out list that could not be READ is ``failed:email_dnc_unreadable``,
  never ``failed:email_dnc``. "We could not check" is not "they opted out", and
  overloading one token would make a database outage look like a wave of
  unsubscribes in every dashboard that counts them.
* ``ROBOTHOR_DNC_MODE=observe`` is honoured exactly as
  :mod:`robothor.engine.tools.handlers.gws` honours it — the check still runs
  and still files its row, and the mail goes. One control, one lever. A second
  guard with no lever is the one an operator disables by editing code.

Two transports, and the fallback is for ABSENCE only
----------------------------------------------------
``gws`` first when the CLI is installed, SMTP when it is not. A gws *error* —
expired OAuth, a quota — is a receipt and not a reason to send the same message
from a different address: the from-address and the audit trail differ between
the two transports, so a silent swap after an auth failure delivers the
operator's briefing as somebody else.
``test_a_gws_error_does_not_re_send_the_same_mail_over_smtp`` is that rule.

What is proof, here
-------------------
Gmail returns the message id it filed under; SMTP's ``send_message`` returns the
recipients it *refused*, so an empty mapping is the acknowledgement and the
locally generated ``Message-ID`` is the id. One message per send, so
``expected`` is 1 — and a transport that accepted the call without returning an
id acknowledges nothing. "The call did not raise" is never evidence.

What is never logged
--------------------
The SMTP password, and nothing shaped like the AUTH line that carries it.
``smtplib`` puts the server's own reply text inside the exception, and a
rejected ``AUTH PLAIN <base64>`` has the password in that base64; every error
here goes through :func:`~robothor.secrets.redaction.redact` before it reaches a
log record, and ``set_debuglevel`` is never called.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine.channels.base import UNCONFIGURED_STEP, SendReceipt, receipt_from
from robothor.engine.channels.email_credentials import SMTP_PASSWORD_ENV, email_credentials
from robothor.secrets.redaction import redact

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence
    from email.message import EmailMessage

    from robothor.engine.models import AgentConfig, AgentRun
    from robothor.identity import IdentityContext

logger = logging.getLogger(__name__)

__all__ = ["STATUSES", "EmailChannel"]

#: Every ``delivery_status`` this channel can record. A CLOSED set, for the
#: reason ``slack.py`` states: a status a query cannot match exactly is not a
#: status, and an SMTP error's own text can carry a credential.
STATUS_NO_TARGET = "failed:email_no_target"
STATUS_UNEXPANDED = "failed:email_unexpanded_target"
STATUS_UNRESOLVED = "failed:email_unresolved_target"
STATUS_NO_RUN = "failed:email_no_run"
STATUS_DNC = "failed:email_dnc"
STATUS_DNC_UNREADABLE = "failed:email_dnc_unreadable"
STATUS_NO_TRANSPORT = "failed:email_no_transport"
STATUS_SEND = "failed:email_send"

STATUSES = frozenset(
    {
        STATUS_NO_TARGET,
        STATUS_UNEXPANDED,
        STATUS_UNRESOLVED,
        STATUS_NO_RUN,
        STATUS_DNC,
        STATUS_DNC_UNREADABLE,
        STATUS_NO_TRANSPORT,
        STATUS_SEND,
    }
)

#: The canonical 8-4-4-4-12 form a ``crm_people.id`` takes. Checked as a shape
#: rather than with ``uuid.UUID``, which also accepts the undashed hex run — and
#: a 32-character string with no ``@`` would then be read as a person id and
#: looked up, rather than reported as the malformed address it is.
_PERSON_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.IGNORECASE
)

#: Deliberately loose, and deliberately anchored. The job is to catch the
#: manifest defects — a Slack channel id, a name, a bare word — not to
#: re-implement RFC 5322, which no regex does. A local part, an ``@``, a
#: dotted domain, and no whitespace or separator that would make this two
#: recipients rather than one.
_ADDRESS_RE = re.compile(r"[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[A-Za-z]{2,}\Z")

#: Implicit TLS. STARTTLS is not offered on this port and calling it there
#: raises, so the channel opens an ``SMTP_SSL`` connection instead.
IMPLICIT_TLS_PORT = 465

#: Budgets. The gws one matches the tool path's; the SMTP one exists because a
#: socket with no timeout is how a delivery becomes a hung run.
GWS_TIMEOUT_S = 30
SMTP_TIMEOUT_S = 30.0

#: How long the one opt-out retry waits, matching the tool path's guard. A
#: dropped socket or a restarting Postgres is over well inside this; refusing on
#: the first blip turns routine database churn into a mail outage, which is how
#: an operator learns to distrust a guard and switch it off.
_DNC_RETRY_DELAY_SECONDS = 0.5

#: What an announced message is titled when nobody said. The agent's display
#: name when it has one: an inbox full of "Genus OS" tells the operator nothing
#: about which of their agents is talking.
DEFAULT_SUBJECT = "Genus OS"


def _describe(exc: BaseException) -> str:
    """One line about a failure, carrying no credential.

    ``smtplib`` raises with the server's own reply text attached, and a refused
    ``AUTH PLAIN <base64>`` has the password inside that base64. This string
    reaches both the journal and ``genus channel verify``'s output, which people
    paste into bug reports, so every return goes through ``redact``.
    """
    return redact(f"{type(exc).__name__}: {exc}")


@dataclass(frozen=True)
class _Transport:
    """Which transport this instance has, and what it needs to use it.

    ``kind`` empty means none — reported as ``failed:email_no_transport`` rather
    than as a send that silently did nothing.
    """

    kind: str = ""
    sender: str = ""
    host: str = ""
    port: int = 0
    starttls: bool = True
    user: str = ""
    password: str = ""
    password_source: str = "missing"


def _gws_available() -> bool:
    """Whether a ``gws`` CLI exists on this box. The seam the suite replaces."""
    from robothor.engine.tools.handlers.gws import gws_available

    return gws_available()


def _gws_send(args: list[str], timeout: int = GWS_TIMEOUT_S) -> dict[str, Any]:
    """Run one gws command. Imported lazily; the seam the suite replaces."""
    from robothor.engine.tools.handlers.gws import run_gws

    return run_gws(args, timeout)


def _build_smtp(host: str, port: int, *, timeout: float = SMTP_TIMEOUT_S) -> Any:
    """An unopened-until-now SMTP connection.

    Imported here rather than at module scope for the reason ``slack.py``
    imports its client lazily: the module must stay importable — and the channel
    must stay *resolvable* — on an instance that will never use this branch.
    """
    import smtplib

    if port == IMPLICIT_TLS_PORT:
        return smtplib.SMTP_SSL(host, port, timeout=timeout)
    return smtplib.SMTP(host, port, timeout=timeout)


def _address_of(target: str) -> str:
    """The bare address in ``target``, lower-cased, or ``""`` if it is not one.

    ``Alice <alice@example.com>`` is what an operator types and what a manifest
    copied out of a mail client carries, so it is parsed rather than refused —
    but what the opt-out list is checked against, and what the message is
    addressed to, is the bare address either way.
    """
    from email.utils import parseaddr

    _name, address = parseaddr(target)
    address = (address or "").strip()
    return address.lower() if _ADDRESS_RE.fullmatch(address) else ""


def _opt_out_lookup(address: str, tenant_id: str) -> set[str]:
    """Read the opt-out list, with ONE retry on a connection-level failure.

    The reader is ``crm.dal.do_not_contact_emails`` — the same one the gws tool
    path calls, so the primary address, the JSONB secondaries and the
    ``contact_identifiers`` rows are all covered by one query that already
    lower-cases both sides. Only the retry policy lives here, and it matches the
    tool path's: ``OperationalError`` only, because a ``ProgrammingError`` means
    the query is wrong and asking again gets the same answer.
    """
    import time

    from psycopg2 import OperationalError

    from robothor.crm.dal import do_not_contact_emails

    try:
        return do_not_contact_emails([address], tenant_id=tenant_id)
    except OperationalError as exc:
        logger.warning(
            "do_not_contact lookup hit a connection error (%s) — one retry in %ss",
            exc,
            _DNC_RETRY_DELAY_SECONDS,
        )
        time.sleep(_DNC_RETRY_DELAY_SECONDS)
        return do_not_contact_emails([address], tenant_id=tenant_id)


def _dnc_mode() -> str:
    """``enforce`` (default) or ``observe``, from the governed flag.

    Delegates to ``feature_flags.do_not_contact_mode`` rather than reading the
    environment, so this channel and the gws tool path answer to the same lever
    — visible in ``/api/controls``, flippable from the dashboard, and recorded
    in the flag audit log.
    """
    try:
        from robothor.engine.feature_flags import do_not_contact_mode

        return do_not_contact_mode()
    except Exception as exc:  # noqa: BLE001 — a flag store that cannot be read
        # must not fail OPEN. Anything unresolvable enforces.
        logger.warning("Could not resolve the do_not_contact mode, enforcing: %s", exc)
        return "enforce"


def _log_dnc_event(run: AgentRun | None, reason: str, *, action: str, mode: str) -> None:
    """File the decision in ``agent_guardrail_events``; never raise.

    The write needs a real run (``run_id`` is NOT NULL and references
    ``agent_runs``), so a call made outside one still refuses — it simply has
    nowhere to file the note.
    """
    run_id = str(getattr(run, "id", "") or "")
    if not run_id:
        return
    try:
        from robothor.engine.tracking import log_guardrail_event

        log_guardrail_event(
            run_id,
            "do_not_contact",
            action,
            tool_name="channel:email",
            reason=reason,
            mode=mode,
        )
    except Exception as exc:  # noqa: BLE001 — evidence must never break a run
        logger.error("could not record do_not_contact guardrail event: %s", exc)


def _message(address: str, subject: str, body: str, *, sender: str = "") -> EmailMessage:
    """One plain-text message.

    ``sender`` is set only on the SMTP path. Gmail sends as whichever account
    the gws CLI is authenticated to and rewrites a ``From`` it does not own, so
    asserting one there would be a header the platform cannot keep — the tool
    path omits it for the same reason. The ``Message-ID`` follows the same rule:
    Gmail assigns its own and returns it, and that returned id is what this
    channel reports.
    """
    from email.message import EmailMessage
    from email.utils import formatdate, make_msgid

    message = EmailMessage()
    message["To"] = address
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    if sender:
        message["From"] = sender
        domain = sender.partition("@")[2].strip()
        message["Message-ID"] = make_msgid(domain=domain or None)
    message.set_content(body)
    return message


class EmailChannel:
    """Outbound email, reachable as ``delivery.channel: email``.

    Registered whether or not this instance has a transport. An instance with
    neither the gws CLI nor an SMTP host gets ``failed:email_no_transport``,
    which names the missing piece; leaving the name unresolvable would record
    ``failed:no_channel:email`` and send the operator looking for a platform
    feature that is right here.

    ``target`` is either a ``crm_people`` id or an address. The id form is the
    one worth having: it resolves through the CRM, so the opt-out flag on that
    person's row is honoured even when the address on it has changed.
    """

    name = "email"

    #: There is no inbound half yet. Phase 2 is IMAP, and it needs the provider
    #: message ids this channel already records to thread a reply against.
    inbound_router: Any | None = None

    def __init__(self) -> None:
        #: The three seams the suite replaces. Production probes the filesystem
        #: for a gws binary, shells out to it, and builds an ``smtplib``
        #: connection — none of which a test may do.
        self.gws_probe: Callable[[], bool] = _gws_available
        self.gws_send: Callable[..., dict[str, Any]] = _gws_send
        self.smtp_factory: Callable[..., Any] = _build_smtp

    # ── configuration ────────────────────────────────────────────────────

    def _transport(self) -> _Transport:
        """Which transport this send would use. Opens nothing.

        gws first, SMTP only when the CLI is absent. Never the other way round
        and never both: see the module docstring.
        """
        try:
            if self.gws_probe():
                return _Transport(kind="gws")
        except Exception as exc:  # noqa: BLE001 — a probe is a report
            logger.warning("Could not probe for the gws CLI: %s", exc)
        return self._smtp_transport()

    @staticmethod
    def _smtp_transport() -> _Transport:
        """The SMTP transport this instance has configured, or an empty one.

        A host with no from-address is not a transport: SMTP has no equivalent
        of Gmail's "send as the authenticated account", so a message with no
        ``From`` is one the server rejects or rewrites. Half-configured is
        reported as absent here and named as a failure by ``genus doctor``,
        which is where an operator is looking for the reason.
        """
        try:
            from robothor.settings import get_settings

            channels = get_settings().channels
        except Exception as exc:  # noqa: BLE001 — unrelated bad config must not
            # break a delivery path whose own configuration may be fine.
            logger.warning("Could not resolve this instance's email settings: %s", exc)
            return _Transport()

        host = (channels.email_smtp_host or "").strip()
        sender = (channels.email_from or "").strip()
        if not host or not sender:
            return _Transport()

        found = email_credentials()
        return _Transport(
            kind="smtp",
            sender=sender,
            host=host,
            port=int(channels.email_smtp_port or 587),
            starttls=bool(channels.email_smtp_starttls),
            user=(channels.email_smtp_user or "").strip(),
            password=found.smtp_password or "",
            password_source=found.smtp_password_source,
        )

    # ── protocol ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """No-op: the transport opens inside :meth:`send`."""
        return

    async def stop(self) -> None:
        """No-op: see :meth:`start`."""
        return

    async def health(self) -> dict[str, Any]:
        """What the operator needs to see. No password, and no fragment of one.

        ``ok`` here means *a transport is configured*, not *a message would
        arrive*: ``genus channel list`` calls this for every channel, and a
        health panel that opened an authenticated SMTP session on every refresh
        would be a diagnostic with a side effect. Proving reach is
        :meth:`verify`'s job, and it is aimed by hand.
        """
        transport = self._transport()
        return {
            "channel": self.name,
            "configured": bool(transport.kind),
            "transport": transport.kind or None,
            "from": transport.sender or None,
            "ok": bool(transport.kind),
        }

    async def send(
        self,
        target: str,
        text: str,
        *,
        subject: str | None = None,
        config: AgentConfig | None = None,
        run: AgentRun | None = None,
        **kw: Any,
    ) -> SendReceipt:
        """Mail ``text`` to ``target``, and report only what a transport proved.

        The order is the contract. Guards run before the tenant is read, the
        tenant before the recipient is resolved, and the opt-out list before any
        transport is chosen — so a refused recipient is refused on an instance
        with no mail configured at all, and a manifest defect reads as a
        manifest defect rather than as a missing credential.
        """
        agent = getattr(config, "id", "?")
        clean = (target or "").strip()
        if not clean:
            logger.warning("No email delivery target for %s", agent)
            return SendReceipt(acknowledged=0, expected=1, status=STATUS_NO_TARGET, target=clean)
        if "${" in clean:
            # `failed:telegram_unexpanded_chat_id` exists because a manifest
            # shipped with an unexpanded variable once. Every surface gets it.
            logger.error("Unexpanded env var in the email delivery target for %s", agent)
            return SendReceipt(acknowledged=0, expected=1, status=STATUS_UNEXPANDED, target=clean)

        is_person = bool(_PERSON_ID_RE.fullmatch(clean))
        if not is_person and not _address_of(clean):
            logger.error(
                "Email delivery target for %s is neither a crm_people id nor an email address",
                agent,
            )
            return SendReceipt(acknowledged=0, expected=1, status=STATUS_UNRESOLVED, target=clean)

        # The opt-out list is per-tenant, and a guard may not guess whose list
        # it is reading: clearing a recipient against some other tenant's list
        # answers a question nobody asked while reporting itself as having
        # checked.
        tenant = (getattr(run, "tenant_id", "") or "").strip() if run is not None else ""
        if not tenant:
            logger.error(
                "Email delivery for %s carries no run, so there is no tenant and no "
                "opt-out list to read. Not sending.",
                agent,
            )
            return SendReceipt(acknowledged=0, expected=1, status=STATUS_NO_RUN, target=clean)

        address, flagged_on_row = await self._resolve(clean, tenant, is_person=is_person)
        if not address:
            logger.error("Email delivery target for %s resolved to no address", agent)
            return SendReceipt(acknowledged=0, expected=1, status=STATUS_UNRESOLVED, target=clean)

        refusal = await self._opt_out_refusal(address, tenant, run, flagged_on_row=flagged_on_row)
        if refusal is not None:
            return SendReceipt(acknowledged=0, expected=1, status=refusal, target=address)

        body = text
        line = subject or (getattr(config, "name", "") or "") or DEFAULT_SUBJECT
        transport = self._transport()
        if not transport.kind:
            logger.warning(
                "Agent %s announces by email but this instance has neither the gws "
                "CLI nor %s + ROBOTHOR_EMAIL_FROM",
                agent,
                "ROBOTHOR_EMAIL_SMTP_HOST",
            )
            return SendReceipt(
                acknowledged=0,
                expected=1,
                status=STATUS_NO_TRANSPORT,
                target=address,
                body=body,
            )
        if transport.kind == "gws":
            return await self._send_gws(address, line, body, agent)
        return await self._send_smtp(transport, address, line, body, agent)

    # ── recipient ────────────────────────────────────────────────────────

    async def _resolve(self, target: str, tenant: str, *, is_person: bool) -> tuple[str, bool]:
        """``(address, flagged_on_the_person_row)`` for ``target``.

        A person id answers both halves in one query: the row carries the
        primary address AND ``doNotContact``, so honouring the flag costs no
        second lookup and works even for a person with no ``contact_identifiers``
        row. An address target answers only the first, and the opt-out query
        covers it.
        """
        if not is_person:
            return _address_of(target), False

        from robothor.crm.dal import get_person

        try:
            person = await asyncio.to_thread(get_person, target, tenant)
        except Exception as exc:  # noqa: BLE001 — an unresolvable recipient is a
            # receipt, not an exception escaping into run finalization.
            logger.error("Could not resolve the email recipient %s: %s", target, _describe(exc))
            return "", False
        if not person:
            return "", False
        primary = str((person.get("emails") or {}).get("primaryEmail") or "").strip()
        return _address_of(primary), bool(person.get("doNotContact"))

    async def _opt_out_refusal(
        self,
        address: str,
        tenant: str,
        run: AgentRun | None,
        *,
        flagged_on_row: bool,
    ) -> str | None:
        """``None`` to proceed, or the status to record instead of sending.

        Nothing below builds a transport, opens a socket or resolves a
        credential. That is the property the tests assert with a call counter
        rather than with a status: a guard that refuses after the mail has left
        is not a guard.
        """
        from psycopg2.errors import UndefinedColumn, UndefinedTable

        try:
            blocked = await asyncio.to_thread(_opt_out_lookup, address, tenant)
        except (UndefinedColumn, UndefinedTable) as exc:
            # Deploy beat `robothor migrate`. Pre-113 nobody can have been
            # flagged, so there is no opt-out to honour and refusing would only
            # manufacture an outage. Scoped to THIS column by name, not to the
            # exception class: the same query also names crm_people.deleted_at,
            # tenant_id, additional_emails and the whole contact_identifiers
            # table, and a carve-out keyed on the type would turn any of those
            # going missing into a silent allow.
            if "do_not_contact" not in str(exc):
                return self._unreadable(address, exc, run)
            logger.error(
                "do_not_contact check skipped for the email channel — this schema "
                "predates migration 113 (%s). Run `robothor migrate`.",
                exc,
            )
            blocked = set()
        except Exception as exc:  # noqa: BLE001 — an unreadable list is a receipt
            return self._unreadable(address, exc, run)

        if not blocked and not flagged_on_row:
            return None

        reason = f"recipient flagged do_not_contact: {address}"
        if _dnc_mode() == "observe":
            logger.warning(
                "do_not_contact OBSERVE: the email channel would have refused %s — "
                "sending anyway because ROBOTHOR_DNC_MODE=observe.",
                address,
            )
            _log_dnc_event(run, reason, action="observed", mode="observe")
            return None

        logger.error(
            "The email channel refused a delivery: %s has opted out of contact "
            "(crm_people.do_not_contact).",
            address,
        )
        _log_dnc_event(run, reason, action="blocked", mode="enforce")
        return STATUS_DNC

    @staticmethod
    def _unreadable(address: str, exc: BaseException, run: AgentRun | None) -> str | None:
        """Refuse a send whose opt-out lookup could not be read.

        Its own status, never ``failed:email_dnc``: that token is a claim about
        a person, and there is no person here — only a list nobody could read.

        Deliberately files NO guardrail event. That write goes to the database
        the lookup just failed on, so it raises too, buying a second traceback
        stacked on the real one. The ERROR line is this branch's evidence.
        """
        logger.error(
            "The email channel could not read the opt-out list for a delivery to %s: "
            "%s. Not sending.",
            address,
            _describe(exc),
        )
        if _dnc_mode() == "observe":
            logger.warning(
                "do_not_contact OBSERVE: sending to %s anyway with the opt-out list "
                "unread, because ROBOTHOR_DNC_MODE=observe.",
                address,
            )
            return None
        return STATUS_DNC_UNREADABLE

    # ── transports ───────────────────────────────────────────────────────

    async def _send_gws(self, address: str, subject: str, body: str, agent: str) -> SendReceipt:
        """Send through the gws CLI and insist on the message id it files under."""
        message = _message(address, subject, body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        args = [
            "gmail",
            "users",
            "messages",
            "send",
            "--params",
            '{"userId":"me"}',
            "--json",
            json.dumps({"raw": raw}),
        ]
        try:
            result = await asyncio.to_thread(self.gws_send, args, GWS_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — a transport failure is a receipt
            logger.error(
                "The email channel's gws transport raised for %s: %s", agent, _describe(exc)
            )
            return SendReceipt(
                acknowledged=0, expected=1, status=STATUS_SEND, target=address, body=body
            )

        error = result.get("error") if isinstance(result, dict) else "gws returned no mapping"
        if error:
            logger.error(
                "The email channel's gws transport refused a send for %s: %s",
                agent,
                redact(str(error)),
            )
            return SendReceipt(
                acknowledged=0, expected=1, status=STATUS_SEND, target=address, body=body
            )

        message_id = str(result.get("id") or "")
        if not message_id:
            # The one thing that is never evidence: the call returning without
            # an id and without raising.
            logger.error(
                "gws accepted a message for %s but returned no message id — nothing "
                "proves it was sent",
                agent,
            )
            return SendReceipt(
                acknowledged=0, expected=1, status=STATUS_SEND, target=address, body=body
            )
        return receipt_from([{"id": message_id}], 1, target=address, body=body)

    async def _send_smtp(
        self, transport: _Transport, address: str, subject: str, body: str, agent: str
    ) -> SendReceipt:
        """Send over SMTP. ``send_message`` returns what it REFUSED."""
        message = _message(address, subject, body, sender=transport.sender)
        message_id = str(message["Message-ID"] or "")
        try:
            refused = await asyncio.to_thread(self._smtp_exchange, transport, message)
        except Exception as exc:  # noqa: BLE001 — a transport failure is a receipt
            logger.error(
                "The email channel could not send over SMTP for %s: %s", agent, _describe(exc)
            )
            return SendReceipt(
                acknowledged=0, expected=1, status=STATUS_SEND, target=address, body=body
            )
        if refused:
            # The recipient is instance data and the server's text is not this
            # module's to trust; the count is the finding.
            logger.error(
                "The SMTP server refused %d recipient(s) of a message for %s",
                len(refused),
                agent,
            )
            return SendReceipt(
                acknowledged=0, expected=1, status=STATUS_SEND, target=address, body=body
            )
        return receipt_from([{"id": message_id}], 1, target=address, body=body)

    def _smtp_exchange(self, transport: _Transport, message: EmailMessage) -> dict[str, Any]:
        """Connect, secure, authenticate, send. Blocking: always in a thread.

        ``set_debuglevel`` is never called: it prints the whole SMTP
        conversation, AUTH line included, to stderr.
        """
        client = self.smtp_factory(transport.host, transport.port, timeout=SMTP_TIMEOUT_S)
        try:
            if transport.starttls and transport.port != IMPLICIT_TLS_PORT:
                client.starttls()
            if transport.user and transport.password:
                client.login(transport.user, transport.password)
            refused = client.send_message(message)
        finally:
            with contextlib.suppress(Exception):
                client.quit()
        return dict(refused or {})

    # ── verify ───────────────────────────────────────────────────────────

    async def verify(self, target: str | None = None) -> list[tuple[str, bool, str]]:
        """Prove, step by step, that this instance can actually send email.

        Two steps. ``transport`` is the half an operator can get wrong without
        noticing — an SMTP password that was rotated, a gws CLI whose OAuth
        expired. ``send`` is the only one that proves reach, which is why it
        exists and is not implied by the first.

        There is no configured verify target and there will not be one. A
        verification mail carries no marker that distinguishes it from a real
        briefing, so a fallback address is a message to whoever the box happens
        to point at; ``--to`` is named every time.

        And ``send`` checks the opt-out list first. Only that: refusing every
        address the CRM knows would make verify unusable, since the operator's
        own address is usually a ``crm_people`` row. A flagged address is the
        one case where sending is the harm.
        """
        transport = self._transport()
        if not transport.kind:
            # ONE step, named `configuration`: that shape is how `genus channel
            # verify` tells "never set up" (exit 2) from "set up and broken"
            # (exit 1) without a second round trip to ask.
            return [
                (
                    UNCONFIGURED_STEP,
                    False,
                    "no email transport on this instance: the gws CLI is not installed "
                    "and ROBOTHOR_EMAIL_SMTP_HOST + ROBOTHOR_EMAIL_FROM are not both set",
                )
            ]
        steps = [await self._verify_transport(transport)]
        steps.append(await self._verify_send(transport, (target or "").strip()))
        return steps

    async def transport_probe(self, transport: _Transport | None = None) -> str | None:
        """``None`` when this instance's transport answers, else what is wrong.

        Shared by :meth:`verify` and the doctor's ``email.transport`` check so
        there is one probe and one wording, rather than a CLI that reports a
        rejected password and a Health panel that shows green for the same box.
        Sends nothing: it opens the session, secures it, authenticates and hangs
        up. ``gws`` has no equivalent round trip that is free — an OAuth check
        there is a real API call — so its presence on disk is what is reported,
        and ``verify``'s send step is what proves it works.

        Returns a sentence rather than raising: a diagnostic that raises on a
        broken box helps nobody.
        """
        resolved = transport if transport is not None else self._transport()
        if not resolved.kind:
            return (
                "no email transport on this instance: the gws CLI is not installed "
                "and ROBOTHOR_EMAIL_SMTP_HOST + ROBOTHOR_EMAIL_FROM are not both set"
            )
        if resolved.kind == "gws":
            return None
        if resolved.user and not resolved.password:
            return (
                f"{SMTP_PASSWORD_ENV} is set neither in the environment nor in this "
                "instance's vault, but an SMTP user is configured"
            )
        try:
            await asyncio.to_thread(self._smtp_probe, resolved)
        except Exception as exc:  # noqa: BLE001 — a probe is a report, not a raise
            return _describe(exc)
        return None

    async def _verify_transport(self, transport: _Transport) -> tuple[str, bool, str]:
        """The transport answers — without sending anything through it."""
        step = "transport"
        problem = await self.transport_probe(transport)
        if problem is not None:
            return (step, False, problem)
        if transport.kind == "gws":
            return (
                step,
                True,
                "the gws CLI is installed; mail goes out as the Google Workspace "
                "account it is authenticated to",
            )
        source = f" (credential from the {transport.password_source})" if transport.user else ""
        return (step, True, f"the SMTP server accepted a session{source}")

    def _smtp_probe(self, transport: _Transport) -> None:
        """Connect, STARTTLS, authenticate, hang up. Sends nothing."""
        client = self.smtp_factory(transport.host, transport.port, timeout=SMTP_TIMEOUT_S)
        try:
            if transport.starttls and transport.port != IMPLICIT_TLS_PORT:
                client.starttls()
            if transport.user and transport.password:
                client.login(transport.user, transport.password)
        finally:
            with contextlib.suppress(Exception):
                client.quit()

    async def _verify_send(self, transport: _Transport, target: str) -> tuple[str, bool, str]:
        """Send one real message, and insist on a message id."""
        step = "send"
        if not target:
            return (
                step,
                False,
                "no address to send to: pass one with --to. There is no fallback "
                "address — a verification mail is indistinguishable from a real one, "
                "so it is aimed by hand every time",
            )
        address = _address_of(target)
        if not address:
            return (step, False, f"{target!r} is not an email address")

        try:
            blocked = await asyncio.to_thread(_opt_out_lookup, address, DEFAULT_TENANT)
        except Exception as exc:  # noqa: BLE001
            return (
                step,
                False,
                "the do-not-contact list could not be read, so this address cannot be "
                f"cleared and nothing was sent ({_describe(exc)})",
            )
        if blocked:
            return (
                step,
                False,
                f"{address} has opted out of contact (crm_people.do_not_contact). "
                "Verify will not mail them — aim it at an address that has not.",
            )

        body = "Genus OS channel verification."
        if transport.kind == "gws":
            receipt = await self._send_gws(address, DEFAULT_SUBJECT, body, "verify")
        else:
            receipt = await self._send_smtp(transport, address, DEFAULT_SUBJECT, body, "verify")
        if not receipt.acknowledged or not receipt.platform_ids:
            return (
                step,
                False,
                "the transport accepted the call but returned no message id, so "
                "nothing proves a message was sent",
            )
        # The id identifies a conversation; "it sent, and the address was cleared
        # against the opt-out list" is the finding.
        return (
            step,
            True,
            "a message was sent and the transport returned its id; the address is "
            "not on this instance's opt-out list",
        )

    # ── C8 / C10 ─────────────────────────────────────────────────────────

    async def ask(
        self,
        question: str,
        options: Sequence[str] = (),
        *,
        timeout: float = 300.0,
        target: str = "",
        addressee: str = "",
    ) -> str | None:
        """Not implemented: email has no inbound half on this platform yet.

        Raising rather than returning ``None`` is the distinction
        :mod:`robothor.engine.channels.base` draws. ``None`` means *the person
        did not reply*, and an unanswered mail is exactly the case a phase-2
        IMAP reader would have to tell apart from *there is no way to ask here*.
        Callers catch this and fall through to a durable ``agent_questions``
        row, which the operator answers from the Helm.
        """
        raise NotImplementedError(
            "interactive ask is not implemented for the email channel: there is no "
            "inbound reader yet, so a question mailed out could never be answered. "
            "An email-triggered run records its question as an agent_questions row "
            "and is answered through the bridge's /api/approvals endpoint"
        )

    async def resolve_identity(
        self, native_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> IdentityContext | None:
        """Map an email address onto a Genus identity, or ``None``.

        One path and one cache: the generic resolver over
        ``user_channel_identities``, the same row any other channel pairs into.
        ``None`` means *nobody is bound to this address*, which is a fact rather
        than a failure — and the answer this channel will keep giving until
        there is an inbound half for a pairing to mean anything.
        """
        from robothor.identity.resolvers import resolve_identity

        return await asyncio.to_thread(resolve_identity, self.name, native_id, tenant_id)
