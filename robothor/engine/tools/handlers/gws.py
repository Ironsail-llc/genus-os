"""Google Workspace (gws CLI) tool handlers."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.engine.tools.constants import MAX_TOOL_OUTPUT_CHARS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

# ── What has to fit in a tool result ──────────────────────────────────
#
# `MAX_TOOL_OUTPUT_CHARS` is the cap applied when a tool result is PERSISTED
# with the run step (see robothor/engine/tools/constants.py for where it does
# and does not apply). Past it the stored JSON is cut head-and-tail with a
# marker in the middle. `GMAIL_BODY_MAX_CHARS` is derived from the cap so that
# raising the cap raises it; the rest are picked, and `_fit_one_message` is
# what makes a picked number safe — it measures the real result and sheds
# fields until it fits.

#: Room for the headers, labels, snippet and JSON punctuation around a body.
#:
#: Picked, not derived — the docstring above used to claim otherwise. It is a
#: measured-generous guess at what a normal envelope costs, and the reason it
#: does not have to be exact is `_fit_one_message`, which measures the real
#: thing and sheds fields until it fits.
_GMAIL_ENVELOPE_CHARS = 1500

#: Default cap on ONE message's decoded body. A body longer than this is cut
#: here, by the handler, which can say `body_truncated` and keep the beginning
#: intact — rather than by the engine, which cuts blind and lands its hole
#: wherever the character count falls.
GMAIL_BODY_MAX_CHARS = MAX_TOOL_OUTPUT_CHARS - _GMAIL_ENVELOPE_CHARS

#: The floor a per-message budget never goes below in a long thread, so that
#: "the messages in order" does not degrade into a list of empty strings.
GMAIL_THREAD_MIN_BODY_CHARS = 300

#: The longest a single header may be in a SEARCH result. A `To:` addressed to
#: a distribution list is routinely a few KB on its own — one realistic header
#: measured at 2,268 characters, 1.5x the entire envelope allowance — and one
#: such message used to starve every other result out of the answer. Generous
#: enough to keep a real recipient list readable, small enough that it cannot
#: eat the budget.
GMAIL_SEARCH_HEADER_MAX_CHARS = 200

#: How many messages one search describes, however many the query matched.
#: Each result carries its headers now, so a hundred of them would be a
#: hundred results with a hole in the middle. Fewer, whole, beats more, cut.
GMAIL_SEARCH_MAX_RESULTS = 25

#: Metadata fetches issued at once when describing a search's results. The CLI
#: spawns a process per call; this is a small enough number to be polite to
#: Google and large enough that ten results do not cost ten round trips.
GMAIL_METADATA_CONCURRENCY = 5


def _resolve_robothor_email() -> str:
    """Resolve the bot's own email for duplicate-reply detection.

    ROBOTHOR_AI_EMAIL is the bot's sending address (e.g. bot@example.com).
    This must NOT use owner_config/ROBOTHOR_OWNER_EMAIL — that is the operator's
    address, and stripping it from reply recipients causes "No recipients
    found in thread" errors on every thread the operator is part of.

    Callers MUST truthiness-check before substring matching —
    `"" in <anything>` is True, which silently drops every reply.
    """
    # ROBOTHOR_AI_EMAIL is the bot's email — always prefer it
    ai_email = os.environ.get("ROBOTHOR_AI_EMAIL", "").lower().strip()
    if ai_email:
        return ai_email
    # Fallback: try owner_config only if AI email not set (legacy installs)
    try:
        from robothor.owner_config import load_owner_config

        cfg = load_owner_config()
        if cfg is not None and cfg.email:
            return cfg.email.lower().strip()
    except Exception:
        pass
    return ""


ROBOTHOR_EMAIL = _resolve_robothor_email()
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")

HANDLERS: dict[str, Any] = {}


# ── Contact 360 write-through helpers ────────────────────────────────────────
# After a successful gws Gmail / Calendar write, mirror it into the Contact
# 360 fabric (message + message_participant + timeline_activity, or
# calendar_event + participants). Best-effort — failure does not propagate.


def _resolve_person_by_email(email: str) -> str | None:
    """Look up person_id for an email address. Checks contact_identifiers
    across any channel (production data has operator emails on channel='api'
    and channel='email' mixed), and falls back to crm_people.email /
    additional_emails so the operator and any other person who only has
    their address on the person row still resolves."""
    try:
        from robothor.db.connection import get_connection

        addr = email.lower().strip()
        if not addr:
            return None
        with get_connection() as conn:
            cur = conn.cursor()
            # 1. contact_identifiers — any channel where identifier matches.
            cur.execute(
                """
                SELECT ci.person_id
                  FROM contact_identifiers ci
                  JOIN crm_people p ON p.id = ci.person_id
                 WHERE lower(ci.identifier) = %s
                   AND p.deleted_at IS NULL
                 LIMIT 1
                """,
                (addr,),
            )
            row = cur.fetchone()
            if row:
                pid = row[0] if not isinstance(row, dict) else row.get("person_id")
                if pid:
                    return str(pid)
            # 2. crm_people direct match (primary email + additional_emails JSONB).
            cur.execute(
                """
                SELECT id
                  FROM crm_people
                 WHERE deleted_at IS NULL
                   AND (lower(email) = %s
                        OR additional_emails::text ILIKE %s)
                 LIMIT 1
                """,
                (addr, f'%"{addr}"%'),
            )
            row = cur.fetchone()
            if row:
                pid = row[0] if not isinstance(row, dict) else row.get("id")
                return str(pid) if pid else None
            return None
    except Exception as e:  # noqa: BLE001
        logger.debug("email person resolve failed: %s", e)
        return None


def _record_sent_email(
    *,
    result: dict[str, Any],
    to: str,
    cc: str,
    subject: str,
    body: str,
    tenant_id: str | None = None,
) -> None:
    """Write message_thread + message + message_participant + timeline_activity
    for a gmail send response. Called only on success (no 'error' key).
    """
    try:
        from robothor.constants import DEFAULT_TENANT
        from robothor.db.connection import get_connection

        if not isinstance(result, dict) or "error" in result:
            return
        gmail_id = result.get("id")
        thread_id_ext = result.get("threadId") or gmail_id
        if not gmail_id or not thread_id_ext:
            return
        tenant_id = tenant_id or DEFAULT_TENANT

        recipients = [addr for addr in _EMAIL_RE.findall(to or "") if addr]
        cc_recipients = [addr for addr in _EMAIL_RE.findall(cc or "") if addr]
        if not recipients and not cc_recipients:
            return

        with get_connection() as conn:
            cur = conn.cursor()

            # 1. message_thread upsert.
            cur.execute(
                """
                INSERT INTO message_thread
                    (tenant_id, channel, external_thread_id, subject,
                     last_message_at, message_count)
                VALUES (%s, 'email', %s, %s, NOW(), 1)
                ON CONFLICT (tenant_id, channel, external_thread_id)
                DO UPDATE SET last_message_at = EXCLUDED.last_message_at,
                              message_count   = message_thread.message_count + 1,
                              updated_at      = NOW()
                RETURNING id
                """,
                (tenant_id, str(thread_id_ext), subject or None),
            )
            row = cur.fetchone()
            thread_row_id = row[0] if not isinstance(row, dict) else row["id"]

            # 2. message insert.
            snippet = (body or "")[:200]
            cur.execute(
                """
                INSERT INTO message
                    (tenant_id, thread_id, channel, direction,
                     external_message_id, subject, body_text, snippet, occurred_at)
                VALUES (%s, %s, 'email', 'outbound', %s, %s, %s, %s, NOW())
                ON CONFLICT (tenant_id, channel, external_message_id)
                DO UPDATE SET thread_id = EXCLUDED.thread_id
                RETURNING id, (xmax = 0) AS inserted
                """,
                (
                    tenant_id,
                    thread_row_id,
                    str(gmail_id),
                    subject or None,
                    body or None,
                    snippet or None,
                ),
            )
            r = cur.fetchone()
            if isinstance(r, dict):
                message_id, inserted = r["id"], r["inserted"]
            else:
                message_id, inserted = r[0], r[1]
            if not inserted:
                return  # already linked — don't duplicate participants/timeline

            # 3. participants per role. Sender is the operator (ROBOTHOR_EMAIL).
            for role, addrs in (("to", recipients), ("cc", cc_recipients)):
                for addr in addrs:
                    pid = _resolve_person_by_email(addr)
                    cur.execute(
                        """
                        INSERT INTO message_participant
                            (tenant_id, message_id, role, person_id, handle)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (tenant_id, message_id, role, pid, addr),
                    )
                    # 4. timeline_activity for resolved recipients.
                    if pid:
                        cur.execute(
                            """
                            INSERT INTO timeline_activity
                                (tenant_id, person_id, occurred_at, activity_type,
                                 source_table, source_id, channel, direction,
                                 title, snippet)
                            VALUES (%s, %s, NOW(), 'email', 'message', %s,
                                    'email', 'outbound', %s, %s)
                            ON CONFLICT (tenant_id, source_table, source_id) DO NOTHING
                            """,
                            (
                                tenant_id,
                                pid,
                                str(message_id),
                                subject or None,
                                snippet or None,
                            ),
                        )

            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("gws email write-through failed: %s", e)


def _record_calendar_event(
    *,
    result: dict[str, Any],
    tenant_id: str | None = None,
) -> None:
    """Upsert calendar_event + calendar_event_participant rows from a gws
    calendar-create response. Emits timeline_activity per resolved attendee."""
    try:
        from robothor.constants import DEFAULT_TENANT
        from robothor.db.connection import get_connection

        if not isinstance(result, dict) or "error" in result:
            return
        google_id = result.get("id")
        if not google_id:
            return
        tenant_id = tenant_id or DEFAULT_TENANT

        summary = result.get("summary")
        status = result.get("status")
        html_link = result.get("htmlLink")
        description = result.get("description")
        location = result.get("location")
        start_at = (result.get("start") or {}).get("dateTime") or (result.get("start") or {}).get(
            "date"
        )
        end_at = (result.get("end") or {}).get("dateTime") or (result.get("end") or {}).get("date")
        attendees = result.get("attendees") or []
        organizer_email = (result.get("organizer") or {}).get("email")

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO calendar_event
                    (tenant_id, google_event_id, title, description, location,
                     start_at, end_at, organizer_email, hangout_link, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, google_event_id)
                DO UPDATE SET title            = EXCLUDED.title,
                              description      = EXCLUDED.description,
                              location         = EXCLUDED.location,
                              start_at         = EXCLUDED.start_at,
                              end_at           = EXCLUDED.end_at,
                              organizer_email  = EXCLUDED.organizer_email,
                              hangout_link     = EXCLUDED.hangout_link,
                              status           = EXCLUDED.status,
                              updated_at       = NOW()
                RETURNING id
                """,
                (
                    tenant_id,
                    str(google_id),
                    summary,
                    description,
                    location,
                    start_at,
                    end_at,
                    organizer_email,
                    html_link,
                    status,
                ),
            )
            row = cur.fetchone()
            event_id = row[0] if not isinstance(row, dict) else row["id"]

            # Purge previous participants for this event and re-insert (keeps
            # response_status fresh on repeat calls).
            cur.execute("DELETE FROM calendar_event_participant WHERE event_id = %s", (event_id,))

            for att in attendees:
                email = (att.get("email") or "").lower().strip()
                if not email:
                    continue
                pid = _resolve_person_by_email(email)
                is_organizer = bool(att.get("organizer"))
                cur.execute(
                    """
                    INSERT INTO calendar_event_participant
                        (tenant_id, event_id, person_id, role, email,
                         display_name, response_status, organizer)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        event_id,
                        pid,
                        "organizer" if is_organizer else "attendee",
                        email,
                        att.get("displayName"),
                        att.get("responseStatus"),
                        is_organizer,
                    ),
                )
                if pid:
                    cur.execute(
                        """
                        INSERT INTO timeline_activity
                            (tenant_id, person_id, occurred_at, activity_type,
                             source_table, source_id, channel, title, snippet)
                        VALUES (%s, %s, %s, 'calendar_event', 'calendar_event', %s,
                                'calendar', %s, %s)
                        ON CONFLICT (tenant_id, source_table, source_id) DO NOTHING
                        """,
                        (
                            tenant_id,
                            pid,
                            start_at,
                            str(event_id),
                            summary,
                            (description or summary or "")[:200]
                            if (description or summary)
                            else None,
                        ),
                    )

            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("gws calendar write-through failed: %s", e)


def _resolve_owner_email() -> str:
    """Operator email from ``.robothor/owner.yaml``, env-var fallback."""
    try:
        from robothor.owner_config import load_owner_config

        cfg = load_owner_config()
        if cfg is not None and cfg.email:
            return cfg.email.lower()
    except Exception:
        logger.debug("owner_config unavailable; using env fallback", exc_info=True)
    return os.environ.get("ROBOTHOR_OWNER_EMAIL", "").strip().lower()


def _send_updates() -> str:
    """Who Google emails about an event this agent touched.

    Delegates to the governed flag
    (``robothor.engine.feature_flags.calendar_send_updates``) rather than
    reading the environment here, so "stop emailing my attendees" is an
    operator control on the dashboard instead of a private edit on a box.
    """
    from robothor.engine.feature_flags import calendar_send_updates

    try:
        return calendar_send_updates()
    except Exception:  # noqa: BLE001 - a flag problem must not lose the invitation
        logger.debug("calendar_send_updates unreadable; sending to all", exc_info=True)
        return "all"


#: The only two values ``calendar`` may take.
_CALENDAR_CHOICES = ("operator", "own")


def _operator_calendar_address() -> str:
    """The operator's address, or ``""`` when it is not usable as a calendar id.

    ``owner_config`` coerces with ``str(data.get("email", ""))``, so a malformed
    ``owner.yaml`` produces a truthy non-address:

        email: null   -> "none"        email: 12345  -> "12345"
        email: no-at  -> "no-at"       email: [a@b]  -> "['a@b']"

    Each of those was handed to Google as a ``calendarId`` while the result
    reported ``kind: "operator"`` — the tool writing somewhere that does not
    exist and saying it went to the operator, which is the precise lie class
    this whole change exists to eliminate. And ``email:`` with nothing after it
    is a shape this very branch produced four times in its own manifests.

    An address with no ``@`` is not an address. Returning ``""`` takes the
    existing, already-tested degradation: ``primary``, ``kind: "own"``, and the
    warning the resolver already emits — honest rather than confidently wrong.
    Full owner-config validation is the proper home for the type checking; this
    is the guard at the point of use.
    """
    email = _resolve_owner_email()
    # A FULL match, not "contains an @": `str(['a@example.com'])` contains one and is
    # a stringified list, not an address.
    if not _EMAIL_RE.fullmatch(email):
        if email:
            logger.warning(
                "the configured operator address is not an email address "
                "(~/.robothor/owner.yaml); treating the operator calendar as unknown"
            )
        return ""
    return email


class _InvalidCalendarError(ValueError):
    """``calendar`` was given something that is not one of the two choices.

    Anything unrecognised used to fall through to the operator branch, so
    ``calendar="primary"`` — the single most likely wrong value a model can
    emit, the word every Google Calendar document uses and this tool's own
    default until the previous commit — meant the EXACT OPPOSITE of what it
    says. An agent meaning "keep this on my own scratch calendar" wrote a real
    event to a human's real calendar and mailed them an invitation.

    An unrecognised value is an error, not a silent write to a person's
    calendar.
    """

    def __init__(self, given: str) -> None:
        super().__init__(given)
        self.given = given

    def as_result(self) -> dict[str, Any]:
        return {
            "error": (
                f"calendar={self.given!r} is not valid. Use calendar='operator' for the "
                "operator's own calendar (the default), or calendar='own' for YOUR "
                "calendar, which the operator never sees. For a third, shared calendar "
                "pass its address as calendar_id — note that 'primary' as a calendar_id "
                "means YOUR account's calendar, not the operator's."
            ),
            "hint": "invalid_params",
        }


def _resolve_calendar(args: dict[str, Any]) -> tuple[str, str]:
    """``(calendar id, kind)`` for a calendar tool call. Kind is the honest half.

    THE defect of 2026-09-16: ``calendar_id`` defaulted to ``"primary"``, and
    ``primary`` is the calendar of whichever account the ``gws`` CLI is signed
    in as — the ASSISTANT's own Google account, not the operator's. An itinerary
    was planned, inserted, and reported as "on your calendar". It was on the
    bot's. The operator was added as an attendee, which made the event look
    right in the API response, and no invitation was sent, so nothing ever
    reached him.

    So the default is the OPERATOR's calendar, and reaching the assistant's own
    requires saying ``calendar="own"`` out loud. A tool whose default quietly
    does the wrong thing is worse than one that refuses.

    The operator's address comes from ``owner.yaml`` through
    ``robothor.owner_config`` — never a literal, which would be instance data in
    platform code. With no owner configured there is no operator calendar to
    write to, so it degrades to ``primary`` and SAYS ``own``: the caller is told
    which calendar it got, and can say so.
    """
    explicit = str(args.get("calendar_id") or "").strip()
    owner_email = _operator_calendar_address()
    if explicit:
        if explicit.lower() == "primary":
            return explicit, "own"
        if owner_email and explicit.lower() == owner_email:
            return explicit, "operator"
        return explicit, "other"

    raw = args.get("calendar")
    choice = "operator" if raw is None or raw == "" else str(raw).strip().lower()
    if choice not in _CALENDAR_CHOICES:
        raise _InvalidCalendarError(choice)
    if choice == "own":
        return "primary", "own"
    if owner_email:
        return owner_email, "operator"
    logger.warning(
        "calendar=%r requested but no operator identity is configured "
        "(~/.robothor/owner.yaml); falling back to this account's own calendar",
        choice,
    )
    return "primary", "own"


def _recipients_for(args: dict[str, Any], calendar_kind: str) -> list[str]:
    """Every address the insert will invite, in order, deduplicated.

    The operator is auto-added ONLY when the event is going somewhere they
    would not otherwise see it. On their own calendar they are the organiser:
    adding them made them an attendee of their own itinerary, so Google asked
    them to RSVP to it and mailed them once per leg — ten emails for a ten-leg
    trip. That auto-add existed because the default used to be this account's
    `primary` calendar, where being an attendee was the only way the operator
    learned the event existed. The default is now theirs.

    One function, because the do-not-contact screen and the insert have to
    agree about who gets mail. They did not: the screen ran on the caller's
    list and the auto-add happened afterwards, so an operator on the opt-out
    list was refused as an explicit attendee and mailed as an implicit one.
    """
    recipients = [str(e).strip() for e in (args.get("attendees") or []) if e]
    owner_email = _operator_calendar_address()
    if (
        owner_email
        and calendar_kind != "operator"
        and not any(e.lower() == owner_email for e in recipients)
    ):
        recipients.append(owner_email)
    return recipients


def _calendar_block(calendar_id: str, kind: str) -> dict[str, str]:
    """The ``calendar`` field every calendar result carries.

    The assistant reported "it's on your calendar" because the API said the
    insert succeeded, and nothing in the result said whose calendar that was.
    Now it does, in every answer, so a truthful report is the easy one.
    """
    return {"kind": kind, "id": calendar_id}


def _normalize_summary(s: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for loose title matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip().lower()


def _summaries_match(a: str, b: str) -> bool:
    """Do these two titles name the same appointment?

    **Never a bare substring test**, which is what it was. When the
    attendee-less dedup branch was added, that weakness stopped being dormant:
    two genuinely different appointments at the same instant, one title
    containing the other, produced ``status: "deduped"`` and NO event —

        create 'Call'  against existing 'Call with the bank'  -> deduped
        create '1:1'   against existing '1:1 with Bob'        -> deduped
        create 'a'     against existing 'Anything at all'     -> deduped

    That is the original incident's failure class — an event the operator asked
    for does not exist — reached from the other direction.

    **Equality after normalisation, and nothing else.** The first fix replaced
    the bare substring test with a prefix rule gated on a 10-character minimum,
    kept for the case where attendees corroborate. That minimum is lower than
    real calendar titles: "Weekly team" is eleven characters and swallowed
    "Weekly team retro"; "Quarterly planning session" swallowed the same
    meeting "… with Legal". Any two events within a fortnight sharing a
    10-character prefix and one attendee collapsed into one, so a recurring 1:1
    booked a week at a time was silently not created from week two — the
    original failure class again, through the door the first fix left open.

    Normalisation is what the prefix rule was reaching for and is enough on its
    own: it strips punctuation and collapses whitespace, so "Hotel — Lisbon"
    and "hotel lisbon" are the same title while "Weekly team retro" is not
    "Weekly team". What identifies the same occurrence of a series is the
    START, which both branches of ``_find_duplicate_event`` now check.
    """
    na, nb = _normalize_summary(a), _normalize_summary(b)
    return bool(na) and na == nb


def _attendee_set(event_like: Any) -> set[str]:
    """Extract attendee emails (lowercased) from a create-payload list or a gws event dict."""
    out: set[str] = set()
    if isinstance(event_like, list):
        for entry in event_like:
            if isinstance(entry, str):
                out.add(entry.strip().lower())
            elif isinstance(entry, dict) and entry.get("email"):
                out.add(str(entry["email"]).strip().lower())
    elif isinstance(event_like, dict):
        for entry in event_like.get("attendees", []) or []:
            if isinstance(entry, dict) and entry.get("email"):
                out.add(str(entry["email"]).strip().lower())
    return out


def _attendees_overlap(proposed: set[str], existing: set[str], owner_email: str) -> bool:
    """Overlap rule for dedup.

    Ignore the operator's own email on both sides (it's auto-added, not a signal).
    Match if the smaller side has at least half its attendees in the other side,
    OR absolute overlap is at least 2.

    When BOTH sides are empty there is no attendee signal either way, and the
    answer is "yes, as far as attendees are concerned" — the title and the start
    time decide. Returning False there made dedup inert for exactly the events
    that caused the 2026-09-16 incident: a flight, a hotel, a trip leg, none of
    which has attendees. Only ONE side being empty is still a mismatch: an event
    with guests and an event without are not the same event.
    """
    a = {e for e in proposed if e and e != owner_email}
    b = {e for e in existing if e and e != owner_email}
    if not a and not b:
        return True
    if not a or not b:
        return False
    inter = a & b
    if len(inter) >= 2:
        return True
    smaller = min(len(a), len(b))
    return smaller > 0 and (len(inter) / smaller) >= 0.5


def _same_start(proposed: str, event: dict[str, Any]) -> bool:
    """Do these two start at the same moment? Tolerant of format and offset."""
    from datetime import datetime

    existing = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get(
        "date", ""
    )
    if not existing or not proposed:
        return False
    try:
        a = datetime.fromisoformat(proposed)
        b = datetime.fromisoformat(str(existing))
    except ValueError:
        return str(existing)[:16] == str(proposed)[:16]
    if (a.tzinfo is None) != (b.tzinfo is None):
        return a.replace(tzinfo=None) == b.replace(tzinfo=None)
    return a == b


def _find_duplicate_event(
    summary: str,
    start: str,
    attendees: list[str],
    calendar_id: str,
    owner_email: str,
    window_days: int = 14,
) -> dict[str, Any] | None:
    """Return an existing event dict if one in the ±window overlaps this proposal, else None.

    Matches on an equal (normalised) summary AND the same start time AND, when
    either side has attendees, an attendee overlap per ``_attendees_overlap``.

    The start check used to be on the attendee-less branch only, so with
    attendees the rule was title-plus-overlap and nothing else: "same title,
    seven days later, same attendee" deduped, and a recurring 1:1 booked weekly
    was not created from week two. Two events are the same event when they are
    at the same moment; a series occurrence is distinguished by exactly the
    field that was not being read.

    Silent on any list failure: dedup is best-effort, and an event the operator
    asked for is worth more than a duplicate they did not.
    """
    import json as _json
    from datetime import datetime, timedelta

    try:
        base = datetime.fromisoformat(start)
    except ValueError:
        logger.debug("gws_calendar_create dedup: unparseable start=%s — skipping check", start)
        return None

    time_min = (base - timedelta(days=window_days)).isoformat()
    time_max = (base + timedelta(days=window_days)).isoformat()

    cal_params = {
        "calendarId": calendar_id,
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 250,
    }
    listed = _run_gws(["calendar", "events", "list", "--params", _json.dumps(cal_params)])
    if not isinstance(listed, dict) or "error" in listed:
        logger.debug("gws_calendar_create dedup: list failed — skipping (result=%s)", listed)
        return None

    proposed_attendees = _attendee_set(attendees)
    for event in listed.get("items", []) or []:
        if not isinstance(event, dict):
            continue
        if event.get("status") == "cancelled":
            continue
        existing_attendees = _attendee_set(event)
        if not _summaries_match(summary, event.get("summary", "") or ""):
            continue
        # On BOTH branches. "Flight to Lisbon" twice in a fortnight is two
        # flights unless they leave at the same moment, and "Weekly 1:1" with
        # the same person next Tuesday is next Tuesday's 1:1.
        if not _same_start(start, event):
            continue
        if not _attendees_overlap(proposed_attendees, existing_attendees, owner_email):
            continue
        return event
    return None


_GWS_BINARY: str | None = None

#: The real Rust binary the npm installer extracts, bypassing the Node.js
#: wrapper. Named here rather than inline so the availability probe below and
#: the resolver below that cannot drift apart about what "installed" means.
_GWS_REAL_BINARY = "/usr/lib/node_modules/@googleworkspace/cli/node_modules/.bin_real/gws"


def gws_available() -> bool:
    """Whether a gws CLI exists on this box at all.

    Deliberately UNCACHED, and deliberately not ``_resolve_gws_binary()``: that
    function's last resort is the bare string ``"gws"``, which it then stores in
    ``_GWS_BINARY`` for the life of the process. Asking it "is gws installed?"
    would therefore answer "yes" on a box with no gws — and poison the cache so
    that every later tool call spawned a command that does not exist.

    Used by the email channel to decide between its two transports, so a wrong
    answer here is a briefing sent from the wrong address or not at all.
    """
    import shutil

    if Path(_GWS_REAL_BINARY).is_file() and os.access(_GWS_REAL_BINARY, os.X_OK):
        return True
    return shutil.which("gws") is not None


def run_gws(args: list[str], timeout: int = 30) -> dict[str, Any]:
    """Run one gws CLI command. The public name for :func:`_run_gws`.

    Same call, same return shape — parsed JSON, ``{"output": …}`` for anything
    that is not JSON, ``{"error": …}`` for a non-zero exit, a timeout or a
    missing binary. It exists so a caller outside this module (the email
    channel) does not have to reach for an underscore-prefixed name, and it
    adds nothing: a second way to run gws would be a second set of timeouts and
    a second way to read a failure.
    """
    return _run_gws(args, timeout)


# ── Turning a Gmail API message into something an agent can read ──────
#
# The Gmail API returns a MIME tree with every body base64url-encoded, which is
# the correct wire format and an unreadable tool result. Everything below
# converts one of those into flat fields, once, in the handler — the place that
# knows the cap it has to fit inside.


class _HtmlToText(HTMLParser):
    """The smallest HTML-to-text converter that does not lie.

    Stdlib only, on purpose: an HTML-only email is a routine thing (an invoice,
    a calendar invite, anything sent by a marketing system) and pulling a
    parser dependency into the engine for it would be a supply-chain decision
    made by a mail format. It drops ``script`` and ``style`` contents — those
    are not the email, and handing a model the contents of a ``<script>`` tag
    out of untrusted mail is an injection surface — turns block-level tags into
    newlines, and unescapes entities (``convert_charrefs`` does that for us).

    **Suppression is a STACK of open element names, not a depth counter.**

    The counter only ever came down on a matching ``handle_endtag``, so any
    suppressing element that was never explicitly closed pinned it above zero
    for the rest of the document and the body came back empty. Three ways that
    happens in ordinary mail, none of them exotic:

    * ``</head>`` and ``</title>`` are **omissible in HTML5** and real mail
      generators omit them. Valid HTML5 with no ``</head>`` returned "".
    * a generator self-closes ``<style/>``, ``<script/>`` or ``<head/>``
      (XHTML habits survive in mail templates).
    * the mail is simply malformed, which is the normal case here.

    The first round of this fix exempted the void elements, which was one
    trigger of the same bug rather than the bug. A stack fixes the class: a
    region is closed by its own end tag, by an **implicit close** — a start tag
    that cannot legally appear inside it, which is how HTML5 says ``<body>``
    ends an unclosed ``<head>`` — or by the end of the document, at which point
    whatever is still open has no content left to suppress anyway.

    ``body_text: ""`` with ``body_chars: 0`` and ``body_truncated: false`` is
    indistinguishable from a genuinely blank message, so the agent reports real
    mail as empty. That is the one failure this converter exists to prevent.
    """

    #: Elements with no end tag (HTML5's void elements). ``HTMLParser`` reports
    #: them through ``handle_starttag`` alone unless they are written
    #: self-closing, so none of them may ever open a region.
    _VOID = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    #: Elements whose CONTENT is not the email.
    _DROP = frozenset({"script", "style", "head", "title"})

    _BREAK = frozenset(
        {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}
    )

    #: Flow content: a start tag that cannot appear inside ``head`` or ``title``,
    #: so seeing one means the unclosed element ended. ``body`` is the canonical
    #: case — HTML5 says an omitted ``</head>`` is implied by it — but mail
    #: generators also drop straight into a ``<table>`` or a ``<div>``.
    _FLOW = frozenset(
        {
            "body",
            "div",
            "p",
            "table",
            "tbody",
            "thead",
            "tr",
            "td",
            "th",
            "span",
            "a",
            "ul",
            "ol",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "blockquote",
            "center",
            "font",
            "article",
            "section",
            "main",
            "header",
            "footer",
        }
    )

    #: What each suppressing element may legally contain. Anything outside it
    #: implicitly closes the element. ``script`` and ``style`` contain only
    #: character data, so nothing closes them but their own end tag — a start
    #: tag inside one is script text, not markup.
    _MAY_CONTAIN: dict[str, frozenset[str]] = {
        "head": frozenset({"title", "meta", "link", "style", "script", "base", "noscript"}),
        "title": frozenset(),
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._open: list[str] = []

    @property
    def _suppressed(self) -> bool:
        return bool(self._open)

    def _implicitly_close(self, tag: str) -> None:
        """Pop any open region that ``tag`` cannot legally appear inside.

        ``<body>`` after an unclosed ``<head>``, or any flow content after an
        unclosed ``<title>``. Character-data elements (``script``, ``style``)
        are never closed this way: their contents are text, not tags.
        """
        while self._open:
            top = self._open[-1]
            allowed = self._MAY_CONTAIN.get(top)
            if allowed is None:
                return  # script/style — only its own end tag closes it
            if tag in allowed:
                return
            if tag in self._FLOW or tag == "body":
                self._open.pop()
                continue
            return

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        self._implicitly_close(tag)
        if tag in self._VOID:
            # Never opens a region. `br` is also in _BREAK and still breaks.
            if tag in self._BREAK and not self._suppressed:
                self._chunks.append("\n")
            return
        if tag in self._DROP:
            self._open.append(tag)
            return
        if tag in self._BREAK and not self._suppressed:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        """``<style/>``, ``<head/>``, ``<meta … />``.

        A self-closing tag opens and closes in one token, so it must never push
        a region. Delegating to ``handle_starttag`` — which is what the previous
        version did, under a docstring claiming this exact property — pushed
        ``style``/``script``/``head`` and lost the rest of the document.
        """
        self._implicitly_close(tag)
        if tag in self._BREAK and not self._suppressed:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._VOID:
            return
        if tag in self._DROP:
            # Pop to and including this tag if it is open at all. An end tag
            # for something never opened (`</style>` alone) is ignored.
            if tag in self._open:
                while self._open and self._open.pop() != tag:
                    pass
            return
        if tag == "head":
            self._open.clear()
            return
        if tag in self._BREAK and not self._suppressed:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks).replace("\xa0", " ")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in joined.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


#: Elements whose contents ``HTMLParser`` hands over as one blob of character
#: data rather than parsing (``CDATA_CONTENT_ELEMENTS``, plus ``title``, which
#: behaves the same way in practice). Inside one of these the parser stops
#: seeing tags entirely, so an element that is never closed swallows the whole
#: rest of the document — no ``handle_starttag`` for ``<body>`` ever arrives,
#: and no amount of bookkeeping in the handler can recover.
#:
#: They are therefore removed BEFORE parsing, closed or not.
_NON_CONTENT = "script|style|title|xmp|iframe|noembed|noframes"

#: A properly closed one.
_CLOSED_REGION_RE = re.compile(rf"<({_NON_CONTENT})\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)

#: A self-closed one: ``<style/>``. It has no contents to remove, only a tag.
_SELF_CLOSED_RE = re.compile(rf"<(?:{_NON_CONTENT}|head)\b[^>]*/\s*>", re.IGNORECASE)

#: An opener with no matching close. It runs to whatever ends the head — or to
#: the end of the document, which is the honest reading of "the author never
#: closed it": everything after is inside the element as far as any parser is
#: concerned, so the only question is where a HUMAN would say it stopped.
_UNCLOSED_REGION_RE = re.compile(
    rf"<(?:{_NON_CONTENT})\b[^>]*>.*?(?=</head\s*>|<body\b|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _strip_non_content_regions(html: str) -> str:
    """Remove script/style/title regions, whether or not they are closed.

    This is what makes an unclosed ``<style>`` survivable. ``HTMLParser`` puts
    those elements into character-data mode, so ``<html><head><style>p{x}</head>
    <body><p>REAL PROSE</p>`` delivers the entire rest of the document as one
    `handle_data` call with `style` still open, and the body comes back empty.
    Every one of the seven triggers in the re-review is this shape or the
    omitted-``</head>`` shape; the handler's stack fixes the second, and only a
    pre-pass can fix the first.
    """
    html = _CLOSED_REGION_RE.sub(" ", html)
    html = _SELF_CLOSED_RE.sub(" ", html)
    return _UNCLOSED_REGION_RE.sub(" ", html)


def _html_to_text(html: str) -> str:
    """Readable text from an HTML email body. Never raises on bad markup."""
    parser = _HtmlToText()
    try:
        parser.feed(_strip_non_content_regions(html))
        parser.close()
    except Exception as exc:  # noqa: BLE001 - malformed mail is the normal case
        logger.debug("gws: HTML body could not be parsed (%s); falling back", exc)
        return re.sub(r"<[^>]+>", " ", html).strip()
    return parser.text()


def _decode_b64(data: str) -> str:
    """Decode one base64url MIME part. Never raises: a body that will not
    decode is still a message with headers worth returning."""
    if not data:
        return ""
    try:
        padded = data + "=" * (-len(data) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:  # noqa: BLE001 - a mangled part is not a crash
        logger.debug("gws: body part would not base64-decode (%s)", exc)
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _walk_parts(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every MIME part of a message, depth first, the payload itself included."""
    yield payload
    for part in payload.get("parts") or ():
        if isinstance(part, dict):
            yield from _walk_parts(part)


def _extract_body(payload: dict[str, Any]) -> str:
    """The message's text: ``text/plain`` if it has one, else its HTML as text.

    Preferring plain text is not an aesthetic choice — the HTML alternative of
    the same message is the same words wrapped in three times the characters,
    and the budget is 2,500 of them.
    """
    plain: list[str] = []
    html: list[str] = []
    for part in _walk_parts(payload):
        if part.get("filename"):
            continue  # an attachment, not the message
        mime = str(part.get("mimeType", ""))
        data = str((part.get("body") or {}).get("data", ""))
        if not data:
            continue
        if mime.startswith("text/plain"):
            plain.append(_decode_b64(data))
        elif mime.startswith("text/html"):
            html.append(_decode_b64(data))
    if plain:
        return "\n".join(t for t in plain if t).strip()
    if html:
        return _html_to_text("\n".join(html))
    return ""


def _extract_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Attachments by name, type and size. Never their bytes: one PDF would be
    the whole result, and the agent has `analyze_pdf` for the contents."""
    found: list[dict[str, Any]] = []
    for part in _walk_parts(payload):
        filename = str(part.get("filename") or "")
        if not filename:
            continue
        body = part.get("body") or {}
        found.append(
            {
                "filename": filename,
                "mime_type": str(part.get("mimeType", "")),
                "size_bytes": int(body.get("size") or 0),
            }
        )
    return found


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    """Headers keyed lowercase. A message with no headers at all is a real
    shape the API returns, and it must not take the tool down."""
    out: dict[str, str] = {}
    for header in payload.get("headers") or ():
        if isinstance(header, dict) and "name" in header:
            out[str(header["name"]).lower()] = str(header.get("value", ""))
    return out


def _shape_envelope(
    message: dict[str, Any], *, max_header_chars: int | None = None
) -> dict[str, Any]:
    """The described-but-unread form: who, when, about what, and its labels.

    ``max_header_chars`` bounds each header, for the search path: a ``To:``
    addressed to a distribution list is unbounded in the API and one of them
    used to consume the whole result budget. ``gws_gmail_get`` passes nothing
    and keeps the headers whole — asking for one message is asking for all of
    it.
    """

    def cut(value: str) -> str:
        if max_header_chars is None or len(value) <= max_header_chars:
            return value
        return value[:max_header_chars] + "…"

    payload = message.get("payload") or {}
    headers = _header_map(payload)
    return {
        "id": str(message.get("id", "")),
        "thread_id": str(message.get("threadId", "")),
        "date": headers.get("date", ""),
        "from": cut(headers.get("from", "")),
        "to": cut(headers.get("to", "")),
        "subject": cut(headers.get("subject", "")),
        # NOT unescaped: `html.unescape` turns `&lt;script&gt;` back into real
        # `<script>` markup, reconstructing from untrusted mail exactly what the
        # body path deliberately strips. Gmail's snippet is already text with
        # its entities escaped; leaving them escaped is both safe and readable.
        "snippet": cut(str(message.get("snippet", ""))),
        "labels": [str(label) for label in (message.get("labelIds") or [])],
    }


def _shape_message(message: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    """One message, whole: envelope, cc, message-id, decoded body, attachments."""
    payload = message.get("payload") or {}
    headers = _header_map(payload)
    shaped = _shape_envelope(message)
    shaped["cc"] = headers.get("cc", "")
    shaped["message_id"] = headers.get("message-id", "")

    body = _extract_body(payload)
    shaped["body_chars"] = len(body)
    if max_chars >= 0 and len(body) > max_chars:
        shaped["body_text"] = body[:max_chars]
        shaped["body_truncated"] = True
    else:
        shaped["body_text"] = body
        shaped["body_truncated"] = False

    attachments = _extract_attachments(payload)
    if attachments:
        shaped["attachments"] = attachments
    return shaped


# ── gws_gmail_search / gws_gmail_get ──────────────────────────────────


def _fetch_message(message_id: str, fmt: str) -> dict[str, Any]:
    """One raw message from the CLI, by id. NEVER raises.

    ``pool.map`` re-raises on iteration, so an exception in one of N metadata
    fetches took the whole search down with it — while the error-DICT path was
    correctly isolated nine survivors out of ten. The isolation lived in the
    caller, one function away from the code that needed it. Today ``_run_gws``'s
    broad except makes this hard to reach; that is what makes it fragile, not
    what makes it safe.
    """
    import json as _json

    params = {"userId": "me", "id": message_id, "format": fmt}
    try:
        result = _run_gws(["gmail", "users", "messages", "get", "--params", _json.dumps(params)])
    except Exception as exc:  # noqa: BLE001 - one bad id must not lose the search
        logger.warning("gws: metadata fetch for %s raised %s", message_id, type(exc).__name__)
        return {
            "error": f"could not fetch this message: {type(exc).__name__}",
            "hint": "the other results in this search are unaffected",
        }
    return result if isinstance(result, dict) else {"error": str(result)[:200]}


def _fits(payload: dict[str, Any]) -> bool:
    """Would this result survive the engine's tool-output cap intact?"""
    import json as _json

    return len(_json.dumps(payload, default=str)) <= MAX_TOOL_OUTPUT_CHARS


def _gmail_search(args: dict[str, Any]) -> dict[str, Any]:
    """Search the mailbox and DESCRIBE what was found.

    The old version returned the Gmail API's ``{"messages": [{"id", "threadId"}]}``
    and nothing else, so every search had to be followed by a `gws_gmail_get`
    per id just to learn who a message was from. Production shows the loop it
    produced: five overlapping searches inside one minute, none of which told
    the agent anything it could answer with.

    One metadata call per id, a few at a time, is the cost of that. It is paid
    once per search instead of once per id per turn.
    """
    import json as _json

    query = str(args.get("query", ""))
    try:
        requested = int(args.get("max_results", 10))
    except (TypeError, ValueError):
        requested = 10
    max_results = max(1, min(requested, GMAIL_SEARCH_MAX_RESULTS))

    params = {"userId": "me", "q": query, "maxResults": max_results}
    listed = _run_gws(["gmail", "users", "messages", "list", "--params", _json.dumps(params)])
    if not isinstance(listed, dict):
        return {"error": "gws returned an unexpected shape for a message list"}
    if "error" in listed:
        return listed

    stubs = [m for m in (listed.get("messages") or []) if isinstance(m, dict)][:max_results]
    if not stubs:
        return {"query": query, "count": 0, "messages": [], "truncated": False}

    ids = [str(m.get("id", "")) for m in stubs]
    with ThreadPoolExecutor(max_workers=GMAIL_METADATA_CONCURRENCY) as pool:
        raw = list(pool.map(lambda i: _fetch_message(i, "metadata"), ids))

    described: list[dict[str, Any]] = []
    for stub, message in zip(stubs, raw, strict=False):
        if "error" in message:
            described.append(
                {
                    "id": str(stub.get("id", "")),
                    "thread_id": str(stub.get("threadId", "")),
                    "error": str(message.get("error", ""))[:200],
                    "hint": str(message.get("hint", "")),
                }
            )
            continue
        envelope = _shape_envelope(message, max_header_chars=GMAIL_SEARCH_HEADER_MAX_CHARS)
        # The ids we asked with are authoritative: a metadata response that
        # omits them still describes the message we listed.
        if not envelope["id"]:
            envelope["id"] = str(stub.get("id", ""))
        if not envelope["thread_id"]:
            envelope["thread_id"] = str(stub.get("threadId", ""))
        described.append(envelope)

    return _fit_search_results(query, described)


#: Envelope fields a search result can do without when the answer will not fit.
#: `to` and `cc` are the largest and the least load-bearing: an agent deciding
#: which message to open reads the sender, the subject and the snippet. They are
#: shed from the biggest entry first, and `gws_gmail_get` still has them.
_SEARCH_OPTIONAL_FIELDS = ("to", "cc")


def _fit_search_results(query: str, described: list[dict[str, Any]]) -> dict[str, Any]:
    """Make N described messages fit the tool-output cap, losing as little as possible.

    The first version popped from the end with no floor:
    ``while described and not _fits(out): described.pop()``. The pop was
    positional, so one oversized entry starved every other — four matches, one
    of them addressed to a 200-person list, produced ``count: 0`` in a
    166-character result against a 4,000-character cap. The tool reported no
    results for a query that matched four messages, and the agent's next move is
    the search-loop this whole change exists to eliminate.

    Three rules, in order of how much they cost the reader:

    1. **Shed optional fields from the largest entry first.** A recipient list
       is the usual offender and the least useful field in a search result.
    2. **Then drop whole entries, largest first** — the offender, not the tail.
    3. **Never return zero.** One described message beats none: it is what the
       agent asked for, and a truncated answer it can act on is worth more than
       an empty one it cannot. The last survivor is shed to its bare identity
       if that is what fitting takes.
    """
    total = len(described)
    kept = list(described)

    def envelope(messages: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "query": query,
            "count": len(messages),
            "messages": messages,
            "truncated": len(messages) < total,
        }
        if out["truncated"]:
            out["results_truncated"] = total - len(messages)
            out["note"] = (
                f"{total - len(messages)} of {total} match(es) omitted to fit the "
                "tool-output limit; narrow the query or lower max_results."
            )
        return out

    def largest(messages: list[dict[str, Any]]) -> int:
        import json as _json

        return max(
            range(len(messages)),
            key=lambda i: len(_json.dumps(messages[i], default=str)),
        )

    # 1. Shed optional fields, biggest entry first, until they are all gone.
    while not _fits(envelope(kept)):
        index = largest(kept)
        shed = [f for f in _SEARCH_OPTIONAL_FIELDS if kept[index].get(f)]
        if not shed:
            break
        trimmed = dict(kept[index])
        for field in shed:
            trimmed.pop(field, None)
        trimmed["fields_omitted"] = list(shed)
        kept[index] = trimmed

    # 2. Then drop whole entries, biggest first, never below one.
    while len(kept) > 1 and not _fits(envelope(kept)):
        kept.pop(largest(kept))

    # 3. A single entry that still does not fit keeps its identity and its
    #    subject; everything else goes. An id the agent can pass to
    #    gws_gmail_get is the minimum useful answer.
    if kept and not _fits(envelope(kept)):
        bare = {k: kept[0].get(k, "") for k in ("id", "thread_id", "from", "date")}
        bare["subject"] = str(kept[0].get("subject", ""))[:GMAIL_SEARCH_HEADER_MAX_CHARS]
        bare["fields_omitted"] = sorted(set(kept[0]) - set(bare))
        kept = [bare]

    return envelope(kept)


def _fit_thread(thread_id: str, shaped: list[dict[str, Any]]) -> dict[str, Any]:
    """Make a whole thread fit the cap, and say what it cost.

    The per-message fitting never reached this path: `_fit_one_message` ran only
    on the `message_id` branch, and the `while len(shaped) > 1` guard meant a
    ONE-message thread could not shrink at all. So the same message read two
    ways gave opposite answers —

        one-message thread, 200-recipient To:  json_len=4236  truncated: false
        the same message via message_id:       json_len= 779

    — 3x the cap at worst, while claiming nothing was lost. Three steps, in the
    order that costs the reader least:

    1. shrink each message's own envelope (attachments, recipients, snippet),
       which is what the `message_id` path already did;
    2. drop the OLDEST bodies, keeping their headers, so the conversation still
       reads end to end and only the newest messages carry their text;
    3. only then drop whole messages, oldest first — the newest is the one that
       was asked about.

    The note is added BEFORE the final fit check, because measuring the envelope
    without it overshot by the length of the note on every truncated thread.
    """
    total = len(shaped)
    kept = [_fit_one_message(m) for m in shaped]

    def envelope(messages: list[dict[str, Any]], bodies_dropped: int) -> dict[str, Any]:
        out: dict[str, Any] = {
            "thread_id": thread_id,
            "count": len(messages),
            "messages": messages,
            "messages_in_thread": total,
            "truncated": len(messages) < total or bool(bodies_dropped),
        }
        if out["truncated"]:
            lost = []
            if len(messages) < total:
                lost.append(f"the {total - len(messages)} oldest of {total} message(s)")
            if bodies_dropped:
                lost.append(f"the body of the {bodies_dropped} oldest kept message(s)")
            out["bodies_omitted"] = bodies_dropped
            out["note"] = (
                " and ".join(lost)
                + " were omitted to fit the tool-output limit; fetch them by id with "
                "gws_gmail_get."
            )
        return out

    bodies_dropped = 0
    # 2. Shed bodies oldest-first, keeping the headers so the thread still reads.
    for index in range(len(kept)):
        if _fits(envelope(kept, bodies_dropped)):
            break
        if len(kept) - index <= 1:
            break  # never strip the newest message's body here
        if not kept[index].get("body_text"):
            continue
        stripped = dict(kept[index])
        stripped["body_text"] = ""
        stripped["body_truncated"] = True
        stripped["body_omitted"] = True
        kept[index] = stripped
        bodies_dropped += 1

    # 3. Then whole messages, oldest first, never below one.
    while len(kept) > 1 and not _fits(envelope(kept, bodies_dropped)):
        dropped = kept.pop(0)
        if dropped.get("body_omitted"):
            bodies_dropped -= 1

    # A single remaining message that still does not fit gets the same
    # last-resort shedding the search path uses.
    if len(kept) == 1 and not _fits(envelope(kept, bodies_dropped)):
        only = dict(kept[0])
        body = str(only.get("body_text", ""))
        while body and len(body) > 100 and not _fits(envelope([only], bodies_dropped)):
            body = body[: len(body) // 2]
            only["body_text"] = body
            only["body_truncated"] = True
        kept = [only]

    return envelope(kept, bodies_dropped)


def _gmail_get(args: dict[str, Any]) -> dict[str, Any]:
    """Read one message, or a whole thread, as text.

    ``format`` stays for compatibility with instructions already written
    against it: ``metadata`` and ``minimal`` return the envelope only, and the
    default ``full`` decodes the body.
    """
    import json as _json

    message_id = str(args.get("message_id", "") or "")
    thread_id = str(args.get("thread_id", "") or "")
    fmt = str(args.get("format", "full") or "full").strip().lower()
    if fmt not in ("full", "metadata", "minimal"):
        return {
            "error": (
                f"format={fmt!r} is not valid. Use 'full' (the default, with the decoded "
                "body), or 'metadata'/'minimal' for headers and snippet only."
            ),
            "hint": "invalid_params",
        }
    with_body = fmt == "full"
    try:
        max_chars = int(args.get("max_chars", GMAIL_BODY_MAX_CHARS))
    except (TypeError, ValueError):
        max_chars = GMAIL_BODY_MAX_CHARS
    # Clamped to what can actually survive: an unclamped 10_000_000 re-decoded
    # a 3 MB base64 payload once per halving of the fit loop.
    max_chars = max(0, min(max_chars, GMAIL_BODY_MAX_CHARS))

    if not message_id and not thread_id:
        return {
            "error": "Either message_id or thread_id is required",
            "hint": "invalid_params: get the id from gws_gmail_search",
        }

    if thread_id:
        params = {"userId": "me", "id": thread_id, "format": "full" if with_body else "metadata"}
        raw = _run_gws(["gmail", "users", "threads", "get", "--params", _json.dumps(params)])
        if not isinstance(raw, dict):
            return {"error": "gws returned an unexpected shape for a thread"}
        if "error" in raw:
            return raw
        messages = [m for m in (raw.get("messages") or []) if isinstance(m, dict)]
        # Every message in the thread shares one budget: a thread of ten must
        # not be ten full bodies, nine of which the cap would eat.
        per_message = (
            max(GMAIL_THREAD_MIN_BODY_CHARS, max_chars // max(1, len(messages))) if with_body else 0
        )
        shaped = [
            _shape_message(m, max_chars=per_message) if with_body else _shape_envelope(m)
            for m in messages
        ]
        return _fit_thread(thread_id or str(raw.get("id", "")), shaped)

    params = {"userId": "me", "id": message_id, "format": "full" if with_body else "metadata"}
    raw = _run_gws(["gmail", "users", "messages", "get", "--params", _json.dumps(params)])
    if not isinstance(raw, dict):
        return {"error": "gws returned an unexpected shape for a message"}
    if "error" in raw:
        return raw
    if not with_body:
        # Through the SAME fitting as every other shape. This returned
        # `_shape_envelope(raw)` raw — no header bound, no cap — so one message
        # came back bounded through `format=full` and 6,975 characters through
        # `format=metadata`, which the model chooses from a schema enum. It is
        # the "same message, two entry points, opposite answers" defect the
        # thread path was fixed for, in a third entry point.
        return _fit_one_message(_shape_envelope(raw))
    message = _shape_message(raw, max_chars=max_chars)
    # A single pathological body (a 3 MB newsletter) can still overflow once
    # the envelope is counted; tighten until it fits rather than hand the
    # engine something it will cut in the middle of a word.
    budget = max_chars
    while budget > GMAIL_THREAD_MIN_BODY_CHARS and not _fits(message):
        budget //= 2
        message = _shape_message(raw, max_chars=budget)
    return _fit_one_message(message)


#: Attachments listed in full before the list is summarised. Sixty attachments
#: is 7 KB of filenames on its own — the body shrink loop cannot help, because
#: it only ever shrank the body.
_GMAIL_MAX_LISTED_ATTACHMENTS = 10

#: Labels listed in full before the list is summarised. Gmail's own system
#: labels are short and few; a mailbox with a hundred long USER label ids is
#: rare and not impossible, and 120 of them is 6,889 characters on their own.
#: Eight keeps every system label a message realistically carries.
_GMAIL_MAX_LISTED_LABELS = 8


def _fit_one_message(message: dict[str, Any]) -> dict[str, Any]:
    """Shrink the ENVELOPE when the body alone was not the problem.

    ``_gmail_get`` halved the body and gave up at 300 characters regardless of
    whether the result fitted, because ``to``, ``subject``, ``snippet``,
    ``labels`` and the whole ``attachments`` list were never counted. Measured:
    60 attachments produced a 7,373-character result with
    ``body_truncated: false``; add a 120-recipient ``To`` and a 400-character
    subject and it reached 10,237 against a 4,000 cap.

    Shed in order of what it costs the reader: the attachment list first (it is
    the biggest and the least often needed), then the label list, then the
    recipient headers, then the snippet — which is redundant beside a body —
    and only then the body.

    Everything shed is REPORTED. A result that quietly loses a 6,798-character
    ``To`` and comes back saying ``truncated: false`` is the contradiction the
    thread path was fixed to remove, and the single-message path had it too:
    only a trailing ``…`` recorded the loss.
    """
    if _fits(message):
        return message

    out = dict(message)
    shed: list[str] = []

    def done() -> dict[str, Any]:
        """Stamp what this cost before handing the result back."""
        if shed:
            out["truncated"] = True
            out["fields_omitted"] = list(shed)
        return out

    attachments = out.get("attachments") or []
    if len(attachments) > _GMAIL_MAX_LISTED_ATTACHMENTS:
        out["attachments"] = attachments[:_GMAIL_MAX_LISTED_ATTACHMENTS]
        out["attachments_omitted"] = len(attachments) - _GMAIL_MAX_LISTED_ATTACHMENTS
        shed.append("attachments")
        if _fits(done()):
            return done()

    # `labels` was the one field this never touched, which contradicted the
    # docstring above it — and no test gave a message more than one label, so a
    # commit could claim this was here while it was not.
    labels = out.get("labels") or []
    if len(labels) > _GMAIL_MAX_LISTED_LABELS:
        out["labels"] = labels[:_GMAIL_MAX_LISTED_LABELS]
        out["labels_omitted"] = len(labels) - _GMAIL_MAX_LISTED_LABELS
        shed.append("labels")
        if _fits(done()):
            return done()

    for field in ("to", "cc"):
        if out.get(field):
            out[field] = str(out[field])[:GMAIL_SEARCH_HEADER_MAX_CHARS] + "…"
            shed.append(field)
            if _fits(done()):
                return done()

    if out.get("snippet"):
        # The snippet is Gmail's preview of the body, and the body is right
        # there. It is the one field that costs nothing to lose.
        out.pop("snippet")
        shed.append("snippet")
        if _fits(done()):
            return done()

    if out.get("subject"):
        out["subject"] = str(out["subject"])[:GMAIL_SEARCH_HEADER_MAX_CHARS] + "…"
        shed.append("subject")
        if _fits(done()):
            return done()

    # Everything above failed, so the body is what is left to cut.
    body = str(out.get("body_text", ""))
    while body and len(body) > 100 and not _fits(done()):
        body = body[: len(body) // 2]
        out["body_text"] = body
        out["body_truncated"] = True
    return done()


def _resolve_gws_binary() -> str:
    """Resolve the actual gws binary, bypassing the Node.js wrapper.

    The ``gws`` symlink (``/usr/bin/gws -> run-gws.js``) launches a Node.js
    shim that calls ``process.cwd()`` before invoking the real Rust binary.
    When the engine daemon has a deleted CWD, this crashes with
    ``ENOENT: no such file or directory, uv_cwd``.

    Resolving to the Rust binary directly avoids the Node.js layer entirely.
    """
    global _GWS_BINARY
    if _GWS_BINARY is not None:
        return _GWS_BINARY

    # Primary: the real Rust binary extracted by the npm installer
    real_bin = _GWS_REAL_BINARY
    if Path(real_bin).is_file() and os.access(real_bin, os.X_OK):
        _GWS_BINARY = real_bin
        logger.debug("gws: using direct binary at %s", real_bin)
        return _GWS_BINARY

    # Fallback: resolve via shutil (follows symlinks)
    import shutil

    resolved = shutil.which("gws")
    if resolved:
        real_resolved = os.path.realpath(resolved)
        _GWS_BINARY = real_resolved
        logger.debug("gws: resolved via which -> %s", real_resolved)
        return _GWS_BINARY

    # Last resort: use "gws" and hope for the best
    _GWS_BINARY = "gws"
    return _GWS_BINARY


#: The exit-code/text patterns that say WHY a gws call failed, in the order
#: they are tried. `gws` writes nothing to stderr for several failure classes,
#: so the only thing the agent was told was "gws exited with code 1" — three
#: times in one week, once because an agent had copied the placeholder thread
#: id out of a benchmark prompt and put it in a real Gmail call. A reason it
#: can act on is the difference between retrying with a real id and retrying
#: with the same fake one.
_GWS_ERROR_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("404", "not found", "notfound", "does not exist"),
        "not_found: the id does not exist in this mailbox",
    ),
    # Rate limiting BEFORE auth: Google returns 403 for `userRateLimitExceeded`,
    # and classifying a throttle as `auth` sent the agent to `gws auth status`
    # for a problem that fixes itself in a minute. "token" and "permission"
    # are gone as needles too — they matched unrelated failures.
    (
        (
            "429",
            "rate limit",
            "ratelimit",
            "quota",
            "userratelimitexceeded",
            "too many requests",
        ),
        "rate_limited: Google is throttling this account",
    ),
    (
        (
            "401",
            "403",
            "unauthorized",
            "unauthenticated",
            "invalid_grant",
            "invalid credentials",
            "sign in",
            "signed in",
            "login",
            "insufficient permission",
        ),
        "auth: gws is not signed in",
    ),
    (
        ("400", "invalid", "malformed", "bad request", "unrecognized", "required"),
        "invalid_params",
    ),
)

#: What to say when nothing matched. Never a bare exit code — and never "it
#: said nothing" when it said something, which contradicted the `error` beside
#: it in the same dict.
_GWS_UNCLASSIFIED_HINT = (
    "unclassified: gws failed for a reason this handler does not recognise. "
    "The arguments may be malformed, or the CLI may not be signed in — check "
    "`gws auth status` on the host."
)
_GWS_SILENT_HINT = (
    "unclassified: gws failed and printed nothing at all. The arguments may be "
    "malformed, or the CLI may not be signed in — check `gws auth status`."
)


def _classify_gws_failure(returncode: int, stdout: str, stderr: str) -> str:
    """A reason for a failed gws call, from whatever text it produced."""
    haystack = f"{stderr}\n{stdout}".lower()
    for needles, hint in _GWS_ERROR_HINTS:
        if any(needle in haystack for needle in needles):
            return hint
    return _GWS_UNCLASSIFIED_HINT if haystack.strip() else _GWS_SILENT_HINT


def _run_gws(args: list[str], timeout: int = 30) -> dict[str, Any]:
    """Run a gws CLI command, return parsed JSON or ``{"error", "hint"}``."""
    import json as _json

    try:
        # Ensure the process CWD is valid — the engine daemon may have a
        # deleted temp dir as CWD which breaks subprocess spawning on some
        # systems (libc getcwd failure in posix_spawn).
        work_dir = str(Path.home())
        try:
            Path.cwd()
        except OSError:
            os.chdir(work_dir)

        proc = subprocess.run(
            [_resolve_gws_binary()] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=work_dir,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            hint = _classify_gws_failure(proc.returncode, stdout, stderr)
            # stdout counts as diagnostics: a failure whose only message went
            # there was discarded, and the agent got the generic hint while the
            # real reason sat unread.
            return {
                "error": (stderr or stdout)[:1000] or hint,
                "hint": hint,
            }
        try:
            result: dict[str, Any] = _json.loads(proc.stdout)
            return result
        except _json.JSONDecodeError:
            # Zero exit, unparseable stdout — a banner, an upgrade notice, a
            # progress line. This used to return {"output": …} with NO error
            # key, and every Gmail call site tests only `"error" in raw`: the
            # message became "that email is blank" and the search became "your
            # query matched nothing". A fabricated negative, from the tool
            # whose stated purpose is to stop lying about mail.
            return {
                "error": "gws returned output that is not JSON",
                "hint": "unparseable: the CLI printed something unexpected — check its version",
                "output": proc.stdout[:MAX_TOOL_OUTPUT_CHARS],
            }
    except subprocess.TimeoutExpired:
        return {
            "error": f"gws command timed out after {timeout}s",
            "hint": "timeout: Google or the CLI did not answer; retry once, then report it",
        }
    except FileNotFoundError:
        return {
            "error": "gws CLI not found — install with: npm install -g @googleworkspace/cli",
            "hint": "not_installed: this host has no gws binary",
        }
    except Exception as e:
        return {"error": f"gws failed: {e}", "hint": _GWS_UNCLASSIFIED_HINT}


# ── do-not-contact guard ─────────────────────────────────────────────────────

#: How long the one retry waits. Long enough for a connection pool to hand
#: back a live socket or a restarting Postgres to accept again; short enough
#: that it is invisible next to the CLI call this guard sits in front of.
_DNC_RETRY_DELAY_SECONDS = 0.5


def _dnc_mode() -> str:
    """``enforce`` (default) or ``observe`` for this send.

    Delegates to the governed flag ``ROBOTHOR_DNC_MODE``
    (``robothor.engine.feature_flags.do_not_contact_mode``) rather than reading
    ``os.environ`` here. Fail-closed is the right default for a compliance
    flag, but a default with no lever is one nobody can respond to: when the
    guard is wrong at 3am the only move left is editing code, and what actually
    happens is that someone comments out the call. ``observe`` keeps the check
    running and still files its evidence while the mail flows.

    Going through the flag store rather than the environment is what makes that
    lever an operator control instead of a private edit: DB-store-first
    resolution puts it in ``/api/controls``, on the dashboard, and in the flag
    audit log. Read per call — the store caches the DB answer briefly and reads
    the env live — so a flip needs no deploy. Anything unrecognised enforces.
    """
    from robothor.engine.feature_flags import do_not_contact_mode

    return do_not_contact_mode()


def _dnc_lookup(addresses: list[str], tenant_id: str) -> set[str]:
    """Read the opt-out list, with ONE retry on a connection-level failure.

    A dropped socket or a restarting Postgres is the common transient here and
    is over in well under a second; refusing on the first blip turns routine
    database churn into a mail outage, which is how an operator learns to
    distrust a guard and switch it off. One attempt more, then the answer
    stands — a retry loop inside a send path is how a transient fault becomes
    a hung run. ``OperationalError`` only: a ``ProgrammingError`` means the
    query is wrong, and asking again gets the same answer.
    """
    from psycopg2 import OperationalError

    from robothor.crm.dal import do_not_contact_emails

    try:
        return do_not_contact_emails(addresses, tenant_id=tenant_id)
    except OperationalError as exc:
        logger.warning(
            "do_not_contact lookup hit a connection error (%s) — one retry in %ss",
            exc,
            _DNC_RETRY_DELAY_SECONDS,
        )
        time.sleep(_DNC_RETRY_DELAY_SECONDS)
        return do_not_contact_emails(addresses, tenant_id=tenant_id)


def _dnc_outcome(
    tool_name: str,
    reason: str,
    message: str,
    run_id: str,
    *,
    record: bool = True,
) -> dict[str, Any] | None:
    """Turn a would-be refusal into the mode's answer: refuse, or note and pass.

    ``record`` is False on the one path where the guardrail write cannot work —
    a lookup that failed goes to the same database this write would.
    """
    if _dnc_mode() == "observe":
        logger.warning(
            "do_not_contact OBSERVE: %s would have been refused (%s) — sending anyway "
            "because ROBOTHOR_DNC_MODE=observe.",
            tool_name,
            reason,
        )
        if record:
            _log_dnc_block(tool_name, reason, run_id, action="observed", mode="observe")
        return None

    if record:
        _log_dnc_block(tool_name, reason, run_id)
    return {"error": message, "guard": "do_not_contact"}


def _dnc_refusal(
    tool_name: str,
    *recipient_fields: str,
    run_id: str = "",
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Refuse an outbound email addressed to anyone flagged ``do_not_contact``.

    Returns ``None`` when the send may proceed, or the tool error to return
    instead of sending. ``recipient_fields`` are raw header-ish strings — a To
    line, a Cc line, a joined reply-all list — and every address in them is
    checked, because an opt-out honoured only on the primary To would leave
    reply-all as an open door.

    Decisions worth stating plainly, since each is the kind that quietly turns
    a control into decoration:

    * A recipient the CRM has never heard of is ALLOWED. This is an opt-out
      list, not an allow-list.
    * A call with no tenant REFUSES. The list is per-tenant; reading some
      other tenant's list would answer a question nobody asked.
    * A lookup that RAISES refuses the send, after one bounded retry
      (``_dnc_lookup``). "We could not read the opt-out list" is not "nobody
      opted out", and the engine writes its own run rows to this same
      database — if it is unreachable the run is already failing.
    * ``ROBOTHOR_DNC_MODE=observe`` (``_dnc_mode``) turns every one of those
      refusals into a logged, recorded note and lets the mail go. That is the
      lever; it exists so nobody has to reach for the one below it, which is
      commenting out this call.

    The refusal is written to ``agent_guardrail_events`` so the control has
    evidence independent of this function's own log line. That write needs a
    real run (``run_id`` is NOT NULL and references ``agent_runs``), so a call
    made outside a run still refuses — it just has nowhere to file the note.
    """
    from psycopg2.errors import UndefinedColumn, UndefinedTable

    addresses = sorted(
        {a.lower() for field in recipient_fields for a in _EMAIL_RE.findall(field or "")}
    )
    if not addresses:
        return None

    # The opt-out list is per-tenant. Falling back to DEFAULT_TENANT here would
    # answer a question nobody asked — it can clear a recipient who is flagged
    # in the tenant this send actually belongs to, while reporting itself as
    # having checked. A guard may not guess whose list it is reading.
    tenant = (tenant_id or "").strip()
    if not tenant:
        logger.error(
            "do_not_contact refused %s: no tenant on the call, so there is no opt-out "
            "list to read. The caller must pass tenant_id (ToolContext.tenant_id).",
            tool_name,
        )
        return _dnc_outcome(
            tool_name,
            "no tenant on the call — opt-out list could not be scoped",
            f"{tool_name} refused: this call carries no tenant, so the do-not-contact "
            "list could not be scoped and it is unknown whether a recipient has opted "
            "out. Not sending. This is a wiring fault, not something to work around — "
            "report it to the operator.",
            run_id,
        )

    try:
        blocked = _dnc_lookup(addresses, tenant)
    except (UndefinedColumn, UndefinedTable) as exc:
        # Deploy beat `robothor migrate`. Pre-113 nobody can have been flagged,
        # so there is no opt-out to honour and refusing would only manufacture
        # an outage. ERROR, not debug: the window is meant to be minutes.
        #
        # Scoped to THIS column by name, not to the exception class. The same
        # SQL also names crm_people.deleted_at/tenant_id/additional_emails and
        # the whole contact_identifiers table; a carve-out keyed on the type
        # would turn any of those going missing — a botched migration, a
        # partial restore, a rename — into a silent allow, which is the exact
        # failure this guard exists to prevent. Anything else is an unreadable
        # list, and an unreadable list refuses.
        if "do_not_contact" not in str(exc):
            return _dnc_lookup_failed(tool_name, exc, run_id)
        logger.error(
            "do_not_contact check skipped for %s — schema predates migration 113 (%s). "
            "Run `robothor migrate` on this instance.",
            tool_name,
            exc,
        )
        return None
    except Exception as exc:
        return _dnc_lookup_failed(tool_name, exc, run_id)

    if not blocked:
        return None

    listed = ", ".join(sorted(blocked))
    return _dnc_outcome(
        tool_name,
        f"recipients flagged do_not_contact: {listed}",
        f"{tool_name} refused: {listed} has opted out of contact "
        "(crm_people.do_not_contact). Do not send to this address, and do not "
        "work around it by using another channel or another address for the same "
        "person. Remove them from the recipients and try again if the message is "
        "for someone else.",
        run_id,
    )


def _dnc_lookup_failed(tool_name: str, exc: BaseException, run_id: str) -> dict[str, Any] | None:
    """Refuse a send whose opt-out lookup could not be read.

    Deliberately does NOT file a guardrail event. That write goes to the same
    database the lookup just failed on, over its own connection, so it raises
    too — buying nothing but a second traceback stacked on the real one. The
    ERROR line below is the evidence for this branch; ``agent_guardrail_events``
    records blocks, which is a claim about a person, and there is no person
    here — only a list we could not read.
    """
    logger.error("do_not_contact lookup failed for %s: %s", tool_name, exc)
    return _dnc_outcome(
        tool_name,
        f"opt-out list could not be checked: {exc}",
        f"{tool_name} refused: the do-not-contact list could not be read, so it is "
        "unknown whether a recipient has opted out. Not sending. Retry once the CRM "
        "database is reachable, or tell the operator plainly that it did not go out.",
        run_id,
        record=False,
    )


def _log_dnc_block(
    tool_name: str,
    reason: str,
    run_id: str,
    *,
    action: str = "blocked",
    mode: str = "enforce",
) -> None:
    """File the refusal in ``agent_guardrail_events``; never raise."""
    if not run_id:
        return
    try:
        from robothor.engine.tracking import log_guardrail_event

        log_guardrail_event(
            run_id,
            "do_not_contact",
            action,
            tool_name=tool_name,
            reason=reason,
            mode=mode,
        )
    except Exception as exc:  # noqa: BLE001 — evidence must never break a run
        logger.error("could not record do_not_contact guardrail event: %s", exc)


# ── gws_calendar_* ────────────────────────────────────────────────────
#
# Split out of `_handle_gws_tool`, which was a 473-line if/elif chain. These
# three share one question — WHOSE calendar — and for a year the answer they
# gave was "the assistant's own", silently. See `_resolve_calendar`.


def _calendar_list(args: dict[str, Any]) -> dict[str, Any]:
    """Read a calendar. Says whose in the result — see `_resolve_calendar`."""
    import json as _json

    time_min = args.get("time_min", "")
    if not time_min:
        return {"error": "time_min is required"}
    try:
        calendar_id, calendar_kind = _resolve_calendar(args)
    except _InvalidCalendarError as bad:
        return bad.as_result()
    try:
        max_results = int(args.get("max_results") or 20)
    except (TypeError, ValueError):
        # A model emitting "max_results": null or "20" is ordinary, and raising
        # TypeError at it loses the whole call over a coercible argument.
        max_results = 20
    cal_params: dict[str, Any] = {
        "calendarId": calendar_id,
        "timeMin": time_min,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": max(1, min(max_results, 250)),
    }
    time_max = args.get("time_max")
    if time_max:
        cal_params["timeMax"] = time_max
    listed = _run_gws(["calendar", "events", "list", "--params", _json.dumps(cal_params)])
    if isinstance(listed, dict) and "error" not in listed:
        # "Nothing on your calendar today" is a different sentence from
        # "nothing on MY calendar today", and the agent could not tell them
        # apart. Now every read says which one it read.
        listed["calendar"] = _calendar_block(calendar_id, calendar_kind)
    return listed


def _calendar_create(
    args: dict[str, Any], *, run_id: str = "", tenant_id: str | None = None
) -> dict[str, Any]:
    """Put an event on a calendar and tell the attendees.

    Defaults to the OPERATOR's calendar and passes ``sendUpdates``: the two
    things whose absence let an itinerary be created on the assistant's own
    calendar, with the operator as an attendee, and nobody told.
    """
    import json as _json

    summary = args.get("summary", "")
    start = args.get("start", "")
    end = args.get("end", "")
    if not summary or not start or not end:
        return {"error": "summary, start, and end are required"}

    # Which calendar comes first, because who gets invited depends on it: the
    # operator is auto-added only for a calendar that is not theirs. Nothing
    # has reached the CLI at this point, so a bad `calendar` still refuses
    # before any read or write.
    try:
        calendar_id, calendar_kind = _resolve_calendar(args)
    except _InvalidCalendarError as bad:
        return bad.as_result()
    owner_email = _operator_calendar_address()

    # Google emails every attendee on insert and on every edit, so this is
    # outbound mail with a different sender. Checked before even the dedup
    # read, so nothing goes out for a blocked invitation.
    #
    # EVERY address that will receive an invitation, not just the caller's
    # list. The operator used to be auto-added AFTER this screen, which left a
    # live opt-out bypass: the same address refused through one door and mailed
    # through the other with `sendUpdates: "all"` on the wire.
    # `_recipients_for` is what the insert will actually send, computed once
    # and used for both.
    attendee_emails = _recipients_for(args, calendar_kind)
    refusal = _dnc_refusal(
        "gws_calendar_create", *attendee_emails, run_id=run_id, tenant_id=tenant_id
    )
    if refusal is not None:
        return refusal

    if not args.get("force"):
        # Against the SAME calendar the insert will use. A duplicate check
        # pointed at the assistant's calendar while the event goes to the
        # operator's finds nothing and then creates the duplicate.
        dup = _find_duplicate_event(
            summary=summary,
            start=start,
            attendees=attendee_emails,
            calendar_id=calendar_id,
            owner_email=owner_email,
        )
        if dup is not None:
            existing_start = (dup.get("start") or {}).get("dateTime") or (
                dup.get("start") or {}
            ).get("date", "")
            logger.warning(
                "gws_calendar_create deduped against existing event %s "
                "(summary=%r start=%s) — use force=true to override",
                dup.get("id"),
                dup.get("summary"),
                existing_start,
            )
            matched_on = (
                "the same title and start time"
                if not attendee_emails and not _attendee_set(dup)
                else "the same title and start time, and an overlapping guest list"
            )
            return {
                "status": "deduped",
                "calendar": _calendar_block(calendar_id, calendar_kind),
                "existing_event_id": dup.get("id"),
                "summary": dup.get("summary"),
                "start": existing_start,
                "htmlLink": dup.get("htmlLink"),
                # Nothing was created and nothing was sent — said outright,
                # because the schema tells the model to report both and the
                # htmlLink here belongs to the EXISTING event.
                "invitations_sent": False,
                # Names which rule fired. It used to claim "overlapping
                # attendees" on the attendee-less path, where neither side had
                # any: the same class of untruth I12 removed, in the sentence
                # the agent relays to the operator.
                "reason": (
                    f"An event with {matched_on} already exists within ±14 days. "
                    "NOT creating a duplicate and NOT sending any invitation; "
                    "htmlLink points at the EXISTING event. Pass force=true to "
                    "override."
                ),
            }

    event_body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if args.get("description"):
        event_body["description"] = args["description"]
    if args.get("location"):
        event_body["location"] = args["location"]
    # Already includes the auto-added operator where one applies, and is the
    # same list the do-not-contact screen above was run over.
    attendees = [{"email": e} for e in attendee_emails]
    if attendees:
        event_body["attendees"] = attendees

    with_meet = args.get("with_meet", True)
    if with_meet:
        request_id = f"robothor-{summary[:20]}-{start[:10]}".replace(" ", "-")
        event_body["conferenceData"] = {
            "createRequest": {
                "requestId": request_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

    cal_params: dict[str, Any] = {"calendarId": calendar_id}
    if with_meet:
        cal_params["conferenceDataVersion"] = 1
    # Without this Google mails nobody. An event on a calendar the attendee
    # does not read, with no invitation, is an event that did not happen as
    # far as the attendee is concerned — which is exactly what the operator
    # experienced on 2026-09-16.
    send_updates = _send_updates() if attendees else "none"
    if attendees:
        cal_params["sendUpdates"] = send_updates

    cal_result = _run_gws(
        [
            "calendar",
            "events",
            "insert",
            "--params",
            _json.dumps(cal_params),
            "--json",
            _json.dumps(event_body),
        ]
    )
    _record_calendar_event(result=cal_result if isinstance(cal_result, dict) else {})
    if isinstance(cal_result, dict) and "error" not in cal_result:
        cal_result["calendar"] = _calendar_block(calendar_id, calendar_kind)
        # With no attendees Google mails nobody whatever the flag says, so
        # `invitations_sent: true` there would be a claim about an empty set.
        # It means "Google was asked to send invitations", which is the only
        # thing the handler can know.
        cal_result["invitations_sent"] = bool(attendees) and send_updates != "none"
        cal_result["send_updates"] = send_updates
        # WHO was mailed is Google's decision, not this handler's: under
        # `externalOnly` it does not mail same-domain attendees, and the
        # operator — auto-added, always same-domain — was being reported as
        # notified when Google had told them nothing.
        #
        # Only `all` lets the handler name the recipients. Under any other
        # setting the key is OMITTED rather than returned empty: an empty list
        # beside `invitations_sent: true` read as "nobody was told", which
        # contradicted the boolean in the same dict — and returning [] when the
        # answer is "unknown" is the same class of false precision. Absent
        # means absent.
        if send_updates == "all" and attendees:
            cal_result["attendees_notified"] = [a["email"] for a in attendees]
    return cal_result


def _calendar_delete(args: dict[str, Any]) -> dict[str, Any]:
    """Remove an event and email the attendees a cancellation."""
    import json as _json

    event_id = args.get("event_id", "")
    if not event_id:
        return {"error": "event_id is required"}
    try:
        calendar_id, calendar_kind = _resolve_calendar(args)
    except _InvalidCalendarError as bad:
        return bad.as_result()
    # A cancellation nobody is told about is not a cancellation: the attendees
    # keep the slot and turn up. Hoisted, because the flag store caches for 5s
    # and reading it twice let the report contradict the call it described.
    send_updates = _send_updates()
    deleted = _run_gws(
        [
            "calendar",
            "events",
            "delete",
            "--params",
            _json.dumps(
                {
                    "calendarId": calendar_id,
                    "eventId": event_id,
                    "sendUpdates": send_updates,
                }
            ),
        ]
    )
    if isinstance(deleted, dict) and "error" not in deleted:
        deleted["calendar"] = _calendar_block(calendar_id, calendar_kind)
        deleted["event_id"] = event_id
        # What was ASKED of Google, not a claim about who it mailed. The
        # handler never reads the event, so it does not know whether it had
        # attendees — and reporting `cancellations_sent: true` for an event
        # with none is the same class of untruth as "the API said success so I
        # said it is on your calendar", which is the defect this all began as.
        deleted["send_updates"] = send_updates
    return deleted


def _handle_gws_tool(
    name: str, args: dict[str, Any], *, run_id: str = "", tenant_id: str | None = None
) -> dict[str, Any]:
    """Handle all gws_* tool calls by mapping to gws CLI commands."""
    import json as _json

    if name == "gws_gmail_search":
        return _gmail_search(args)

    if name == "gws_gmail_get":
        return _gmail_get(args)

    if name == "gws_gmail_reply":
        import base64
        from email.mime.text import MIMEText

        thread_id = args.get("thread_id", "")
        body = args.get("body", "")
        extra_cc = args.get("cc", "")

        if not thread_id:
            return {"error": "thread_id is required — get it from the task body"}
        if not body:
            return {"error": "body is required"}

        # Fetch thread to extract headers, recipients, and Message-ID.
        # NOTE: Do NOT include metadataHeaders here — gws CLI v0.8.0 does not
        # correctly serialize array query params, causing the API to return 0
        # headers per message.  Fetching all headers (format=metadata only) works.
        fetch_params = {
            "userId": "me",
            "id": thread_id,
            "format": "metadata",
        }
        thread_data = _run_gws(
            ["gmail", "users", "threads", "get", "--params", _json.dumps(fetch_params)]
        )
        if isinstance(thread_data, str):
            try:
                thread_data = _json.loads(thread_data)
            except _json.JSONDecodeError:
                return {"error": f"Failed to parse thread data: {thread_data[:200]}"}
        if isinstance(thread_data, dict) and "error" in thread_data:
            return thread_data

        messages = thread_data.get("messages", []) if isinstance(thread_data, dict) else []
        if not messages:
            return {"error": f"Thread {thread_id} has no messages"}

        last_msg = messages[-1]
        last_headers: dict[str, str] = {}
        for h in last_msg.get("payload", {}).get("headers", []):
            last_headers[h["name"]] = h["value"]

        # Duplicate guard: skip if last message is already from us.
        # MUST truthiness-check — empty ROBOTHOR_EMAIL is a substring of
        # every string, so a bare `in` check would drop every reply.
        last_from = last_headers.get("From", "")
        if ROBOTHOR_EMAIL and ROBOTHOR_EMAIL in last_from.lower():
            return {
                "status": "skipped",
                "reason": "Already replied to this thread — last message is from robothor",
            }

        # Extract Message-ID for In-Reply-To / References
        message_id_header = last_headers.get("Message-ID", "")

        # Extract subject (auto-prefix Re: if needed)
        original_subject = last_headers.get("Subject", "")
        if original_subject.lower().startswith("re:"):
            subject = original_subject
        else:
            subject = f"Re: {original_subject}"

        # Collect all recipients from entire thread (reply-all)
        all_addresses: set[str] = set()
        for m in messages:
            for h in m.get("payload", {}).get("headers", []):
                if h["name"] in ("From", "To", "Cc"):
                    all_addresses.update(a.lower() for a in _EMAIL_RE.findall(h["value"]))

        # Remove our own address from recipients
        all_addresses.discard(ROBOTHOR_EMAIL)

        # Add any extra CC from args
        extra_addrs: set[str] = set()
        if extra_cc:
            extra_addrs.update(a.lower() for a in _EMAIL_RE.findall(extra_cc))
            extra_addrs.discard(ROBOTHOR_EMAIL)

        to_addresses = sorted(all_addresses)
        cc_addresses = sorted(extra_addrs - all_addresses)

        if not to_addresses and not cc_addresses:
            return {"error": "No recipients found in thread"}

        refusal = _dnc_refusal(
            name, *to_addresses, *cc_addresses, run_id=run_id, tenant_id=tenant_id
        )
        if refusal is not None:
            return refusal

        # Build MIME message with proper threading headers
        msg = MIMEText(body)
        msg["To"] = ", ".join(to_addresses)
        msg["Subject"] = subject
        if cc_addresses:
            msg["Cc"] = ", ".join(cc_addresses)
        if message_id_header:
            msg["In-Reply-To"] = message_id_header
            msg["References"] = message_id_header

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        reply_json: dict[str, Any] = {"raw": raw, "threadId": thread_id}

        reply_result = _run_gws(
            [
                "gmail",
                "users",
                "messages",
                "send",
                "--params",
                '{"userId":"me"}',
                "--json",
                _json.dumps(reply_json),
            ],
            timeout=30,
        )
        _record_sent_email(
            result=reply_result if isinstance(reply_result, dict) else {},
            to=", ".join(to_addresses),
            cc=", ".join(cc_addresses),
            subject=subject,
            body=body,
        )
        return reply_result

    if name == "gws_gmail_send":
        import base64
        from email.mime.text import MIMEText

        to = args.get("to", "")
        subject = args.get("subject", "")
        body = args.get("body", "")
        cc = args.get("cc", "")
        thread_id = args.get("thread_id")
        in_reply_to = args.get("in_reply_to", "")

        refusal = _dnc_refusal(name, to, cc, run_id=run_id, tenant_id=tenant_id)
        if refusal is not None:
            return refusal

        # Warn if this looks like a reply but has no thread_id
        if not thread_id and subject.lower().startswith("re:"):
            logger.warning(
                "gws_gmail_send: subject starts with 'Re:' but no thread_id provided — "
                "this will create a new thread. Use gws_gmail_reply instead. to=%s subject=%s",
                to,
                subject,
            )

        # Guard: prevent duplicate replies to the same thread
        if thread_id:
            try:
                # NOTE: Do NOT include metadataHeaders — gws CLI bug (see gws_gmail_reply).
                check_params = {
                    "userId": "me",
                    "id": thread_id,
                    "format": "metadata",
                }
                thread_data = _run_gws(
                    [
                        "gmail",
                        "users",
                        "threads",
                        "get",
                        "--params",
                        _json.dumps(check_params),
                    ]
                )
                if isinstance(thread_data, str):
                    thread_data = _json.loads(thread_data)
                if isinstance(thread_data, dict):
                    messages = thread_data.get("messages", [])
                    if messages:
                        last_msg = messages[-1]
                        headers = {
                            h["name"]: h["value"]
                            for h in last_msg.get("payload", {}).get("headers", [])
                            if h.get("name") == "From"
                        }
                        last_from = headers.get("From", "")
                        if ROBOTHOR_EMAIL and ROBOTHOR_EMAIL in last_from.lower():
                            return {
                                "status": "skipped",
                                "reason": "Already replied to this thread — last message is from robothor",
                            }
            except Exception:
                logger.debug("Gmail send duplicate guard failed", exc_info=True)

        content_type = args.get("content_type", "text")
        # Defensive: if the caller forgot content_type but the body is clearly HTML
        # (starts with <!DOCTYPE or <html), treat it as HTML. Prevents agents from
        # silently sending rendered HTML as plaintext and showing raw tags.
        if content_type != "html":
            body_head = body.lstrip()[:15].lower()
            if body_head.startswith("<!doctype html") or body_head.startswith("<html"):
                logger.warning(
                    "gws_gmail_send: body looks like HTML but content_type=%r; "
                    "auto-upgrading to html. to=%s",
                    content_type,
                    to,
                )
                content_type = "html"
        subtype = "html" if content_type == "html" else "plain"
        msg = MIMEText(body, _subtype=subtype)
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        send_json: dict[str, Any] = {"raw": raw}
        if thread_id:
            send_json["threadId"] = thread_id

        result = _run_gws(
            [
                "gmail",
                "users",
                "messages",
                "send",
                "--params",
                '{"userId":"me"}',
                "--json",
                _json.dumps(send_json),
            ],
            timeout=30,
        )

        # Add warning to result if threading was likely intended but missing
        if (
            not thread_id
            and subject.lower().startswith("re:")
            and isinstance(result, dict)
            and "error" not in result
        ):
            result["_warning"] = (
                "No thread_id was provided but subject starts with 'Re:'. "
                "This message was sent as a new thread, not a reply. "
                "Use gws_gmail_reply to reply within existing conversations."
            )

        _record_sent_email(
            result=result if isinstance(result, dict) else {},
            to=to,
            cc=cc,
            subject=subject,
            body=body,
        )
        return result

    if name == "gws_gmail_modify":
        message_id = args.get("message_id", "")
        if not message_id:
            return {"error": "message_id is required"}
        add_labels = args.get("add_labels", [])
        remove_labels = args.get("remove_labels", [])
        modify_body: dict[str, Any] = {}
        if add_labels:
            modify_body["addLabelIds"] = add_labels
        if remove_labels:
            modify_body["removeLabelIds"] = remove_labels
        if not modify_body:
            return {"error": "At least one of add_labels or remove_labels is required"}
        return _run_gws(
            [
                "gmail",
                "users",
                "messages",
                "modify",
                "--params",
                _json.dumps({"userId": "me", "id": message_id}),
                "--json",
                _json.dumps(modify_body),
            ]
        )

    if name == "gws_calendar_list":
        return _calendar_list(args)

    if name == "gws_calendar_create":
        return _calendar_create(args, run_id=run_id, tenant_id=tenant_id)

    if name == "gws_calendar_delete":
        return _calendar_delete(args)

    if name == "gws_chat_send":
        space = args.get("space", "")
        text = args.get("text", "")
        if not space or not text:
            return {"error": "space and text are required"}
        return _run_gws(
            [
                "chat",
                "spaces",
                "messages",
                "create",
                "--params",
                _json.dumps({"parent": space}),
                "--json",
                _json.dumps({"text": text}),
            ]
        )

    if name == "gws_chat_list_spaces":
        page_size = min(args.get("page_size", 50), 1000)
        return _run_gws(
            [
                "chat",
                "spaces",
                "list",
                "--params",
                _json.dumps({"pageSize": page_size}),
            ]
        )

    if name == "gws_chat_list_messages":
        space = args.get("space", "")
        if not space:
            return {"error": "space is required"}
        page_size = min(args.get("page_size", 25), 100)
        return _run_gws(
            [
                "chat",
                "spaces",
                "messages",
                "list",
                "--params",
                _json.dumps({"parent": space, "pageSize": page_size}),
            ]
        )

    return {"error": f"Unknown gws tool: {name}"}


# Mutating gws tools. Kept as its own set because the guardrail engine and the
# benchmark deny-list both ask "does this tool change anything at Google", and
# the answer differs from "may a benchmark call it" — which is now no, for
# every gws tool.
_GWS_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "gws_gmail_reply",
        "gws_gmail_send",
        "gws_gmail_modify",
        "gws_calendar_create",
        "gws_calendar_delete",
        "gws_chat_send",
    }
)


def _benchmark_refusal(tool_name: str) -> dict[str, Any]:
    """Every gws tool is refused under ``ctx.is_benchmark`` — reads included.

    These shell out to the `gws` CLI, which carries real Workspace credentials
    and is opaque to the runner's allow-list guard. Reads used to be allowed
    through on the reasoning that inspecting state is harmless; it is not. A
    benchmark prompt that says "reply to thread thread_def456" produced a real
    Gmail lookup against the operator's real mailbox on this instance, and a
    graded run that can read the operator's mail is a graded run that can put
    the operator's mail in its answer. A benchmark must not touch the real
    account at all, in either direction.
    """
    return {
        "error": (
            f"Tool '{tool_name}' is disabled in benchmark mode — a benchmark never "
            "touches the real Google account, not even to read."
        ),
        "guard": "is_benchmark",
    }


# Register all GWS tools as async handlers that delegate to sync
# _handle_gws_tool. `_make_handler` below is the only producer: a second
# module-level `_gws_handler` used to sit here, referenced by nothing, carrying
# its own copy of the benchmark gate — two copies of a safety check, one of
# them unreachable, which is the shape that makes one of them go stale.
for _tool_name in (
    "gws_gmail_search",
    "gws_gmail_get",
    "gws_gmail_reply",
    "gws_gmail_send",
    "gws_gmail_modify",
    "gws_calendar_list",
    "gws_calendar_create",
    "gws_calendar_delete",
    "gws_chat_send",
    "gws_chat_list_spaces",
    "gws_chat_list_messages",
):

    def _make_handler(tn: str) -> Callable[..., Any]:
        async def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            if ctx.is_benchmark:
                return _benchmark_refusal(tn)
            return await asyncio.to_thread(
                _handle_gws_tool, tn, args, run_id=ctx.run_id, tenant_id=ctx.tenant_id
            )

        return handler

    HANDLERS[_tool_name] = _make_handler(_tool_name)
