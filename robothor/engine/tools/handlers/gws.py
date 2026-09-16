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
from html import unescape as _unescape
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
# `MAX_TOOL_OUTPUT_CHARS` is the engine's cap on one tool result: past it the
# JSON is cut head-and-tail with a marker in the middle. These three numbers
# are derived from it rather than picked, so raising the cap raises them.

#: Room for the headers, labels, snippet and JSON punctuation around a body.
_GMAIL_ENVELOPE_CHARS = 1500

#: Default cap on ONE message's decoded body. A body longer than this is cut
#: here, by the handler, which can say `body_truncated` and keep the beginning
#: intact — rather than by the engine, which cuts blind and lands its hole
#: wherever the character count falls.
GMAIL_BODY_MAX_CHARS = MAX_TOOL_OUTPUT_CHARS - _GMAIL_ENVELOPE_CHARS

#: The floor a per-message budget never goes below in a long thread, so that
#: "the messages in order" does not degrade into a list of empty strings.
GMAIL_THREAD_MIN_BODY_CHARS = 300

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
    owner_email = _resolve_owner_email()
    if explicit:
        if explicit.lower() == "primary":
            return explicit, "own"
        if owner_email and explicit.lower() == owner_email:
            return explicit, "operator"
        return explicit, "other"

    choice = str(args.get("calendar") or "operator").strip().lower()
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
    """True when two meeting titles likely name the same series.

    Either normalized string contains the other (catches "Team Weekly" vs
    "Team Weekly Leadership"), OR normalized strings are equal.
    """
    na, nb = _normalize_summary(a), _normalize_summary(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return na in nb or nb in na


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
    """
    a = {e for e in proposed if e and e != owner_email}
    b = {e for e in existing if e and e != owner_email}
    if not a or not b:
        return False
    inter = a & b
    if len(inter) >= 2:
        return True
    smaller = min(len(a), len(b))
    return smaller > 0 and (len(inter) / smaller) >= 0.5


def _find_duplicate_event(
    summary: str,
    start: str,
    attendees: list[str],
    calendar_id: str,
    owner_email: str,
    window_days: int = 14,
) -> dict[str, Any] | None:
    """Return an existing event dict if one in the ±window overlaps this proposal, else None.

    Only dedups against events with same-or-substring summary AND attendee overlap
    (per _attendees_overlap). Silent on any list failure — dedup is best-effort.
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
        if not _summaries_match(summary, event.get("summary", "") or ""):
            continue
        existing_attendees = _attendee_set(event)
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

    **Void elements never suppress.** ``meta`` and ``link`` were in ``_DROP``,
    and ``HTMLParser`` never fires ``handle_endtag`` for an element that has no
    end tag — so ``_suppress`` was incremented and never decremented, and every
    character after the tag was dropped. Practically every HTML email opens
    with ``<meta charset>``, and marketing and invoice mail almost always
    carries a ``<link rel="stylesheet">``, so the common case returned
    ``body_text: ""`` — indistinguishable from a genuinely blank message, which
    is the one failure this converter exists to prevent. The suite passed
    because its only HTML fixture was ``<head><style>…</style></head>``: the one
    head layout containing no void tag.
    """

    #: Elements with no end tag (HTML5's void elements). ``HTMLParser`` reports
    #: them through ``handle_starttag`` alone unless they are written
    #: self-closing, so nothing here may ever touch the suppression depth.
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

    #: Elements whose CONTENT is not the email. Only non-void tags belong here:
    #: a void tag has no content to drop.
    _DROP = frozenset({"script", "style", "head", "title"})
    _BREAK = frozenset(
        {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._VOID:
            # Never opens a region. `br` is also in _BREAK and still breaks.
            if tag in self._BREAK and not self._suppress:
                self._chunks.append("\n")
            return
        if tag in self._DROP:
            self._suppress += 1
        elif tag in self._BREAK and not self._suppress:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        """``<meta … />``. Explicit, so a self-closing non-void tag — which
        ``HTMLParser`` would otherwise route to ``handle_starttag`` alone —
        cannot open a suppression region it will never close either."""
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._VOID:
            return
        if tag in self._DROP and self._suppress:
            self._suppress -= 1
        elif tag in self._BREAK and not self._suppress:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppress:
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks).replace("\xa0", " ")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in joined.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _html_to_text(html: str) -> str:
    """Readable text from an HTML email body. Never raises on bad markup."""
    parser = _HtmlToText()
    try:
        parser.feed(html)
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


def _shape_envelope(message: dict[str, Any]) -> dict[str, Any]:
    """The described-but-unread form: who, when, about what, and its labels."""
    payload = message.get("payload") or {}
    headers = _header_map(payload)
    return {
        "id": str(message.get("id", "")),
        "thread_id": str(message.get("threadId", "")),
        "date": headers.get("date", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "snippet": _unescape(str(message.get("snippet", ""))),
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
    """One raw message from the CLI, by id."""
    import json as _json

    params = {"userId": "me", "id": message_id, "format": fmt}
    result = _run_gws(["gmail", "users", "messages", "get", "--params", _json.dumps(params)])
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
        envelope = _shape_envelope(message)
        # The ids we asked with are authoritative: a metadata response that
        # omits them still describes the message we listed.
        if not envelope["id"]:
            envelope["id"] = str(stub.get("id", ""))
        if not envelope["thread_id"]:
            envelope["thread_id"] = str(stub.get("threadId", ""))
        described.append(envelope)

    # Drop from the END until the whole result fits, rather than letting the
    # engine cut the last entry in half. Fewer, whole beats more, holed.
    total = len(described)
    out: dict[str, Any] = {
        "query": query,
        "count": total,
        "messages": described,
        "truncated": False,
    }
    while described and not _fits(out):
        described.pop()
        out["messages"] = described
        out["count"] = len(described)
        out["truncated"] = True
    if out["truncated"]:
        out["note"] = (
            f"{total - len(described)} more match(es) omitted to fit the tool-output "
            "limit; narrow the query or lower max_results."
        )
    return out


def _gmail_get(args: dict[str, Any]) -> dict[str, Any]:
    """Read one message, or a whole thread, as text.

    ``format`` stays for compatibility with instructions already written
    against it: ``metadata`` and ``minimal`` return the envelope only, and the
    default ``full`` decodes the body.
    """
    import json as _json

    message_id = str(args.get("message_id", "") or "")
    thread_id = str(args.get("thread_id", "") or "")
    fmt = str(args.get("format", "full") or "full")
    with_body = fmt == "full"
    try:
        max_chars = int(args.get("max_chars", GMAIL_BODY_MAX_CHARS))
    except (TypeError, ValueError):
        max_chars = GMAIL_BODY_MAX_CHARS
    max_chars = max(0, max_chars)

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
        out: dict[str, Any] = {
            "thread_id": thread_id or str(raw.get("id", "")),
            "count": len(shaped),
            "messages": shaped,
        }
        # Oldest first, newest last — the order the API returns and the order a
        # conversation reads in. Trim the OLDEST when it does not fit: the
        # newest message is the one that was asked about.
        while len(shaped) > 1 and not _fits(out):
            shaped.pop(0)
            out["messages"] = shaped
            out["count"] = len(shaped)
            out["truncated"] = True
        return out

    params = {"userId": "me", "id": message_id, "format": "full" if with_body else "metadata"}
    raw = _run_gws(["gmail", "users", "messages", "get", "--params", _json.dumps(params)])
    if not isinstance(raw, dict):
        return {"error": "gws returned an unexpected shape for a message"}
    if "error" in raw:
        return raw
    if not with_body:
        return _shape_envelope(raw)
    message = _shape_message(raw, max_chars=max_chars)
    # A single pathological body (a 3 MB newsletter) can still overflow once
    # the envelope is counted; tighten until it fits rather than hand the
    # engine something it will cut in the middle of a word.
    budget = max_chars
    while budget > GMAIL_THREAD_MIN_BODY_CHARS and not _fits(message):
        budget //= 2
        message = _shape_message(raw, max_chars=budget)
    return message


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
    (
        (
            "401",
            "403",
            "unauthorized",
            "unauthenticated",
            "credential",
            "token",
            "sign in",
            "signed in",
            "login",
            "permission",
        ),
        "auth: gws is not signed in",
    ),
    (
        ("400", "invalid", "malformed", "bad request", "unrecognized", "required"),
        "invalid_params",
    ),
    (("429", "rate limit", "quota"), "rate_limited: Google is throttling this account"),
)

#: What to say when nothing matched. Never a bare exit code.
_GWS_UNCLASSIFIED_HINT = (
    "gws failed and said nothing; the arguments may be malformed, or the CLI "
    "may not be signed in. Check `gws auth status` on the host."
)


def _classify_gws_failure(returncode: int, stdout: str, stderr: str) -> str:
    """A reason for a failed gws call, from whatever text it produced."""
    haystack = f"{stderr}\n{stdout}".lower()
    for needles, hint in _GWS_ERROR_HINTS:
        if any(needle in haystack for needle in needles):
            return hint
    return _GWS_UNCLASSIFIED_HINT


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
            return {
                "error": stderr[:1000] or hint,
                "hint": hint,
            }
        try:
            result: dict[str, Any] = _json.loads(proc.stdout)
            return result
        except _json.JSONDecodeError:
            return {"output": proc.stdout[:MAX_TOOL_OUTPUT_CHARS]}
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
    calendar_id, calendar_kind = _resolve_calendar(args)
    cal_params: dict[str, Any] = {
        "calendarId": calendar_id,
        "timeMin": time_min,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": min(args.get("max_results", 20), 250),
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

    # Google emails every attendee on insert and on every edit, so this is
    # outbound mail with a different sender. Checked first, before even the
    # dedup read, so nothing goes out for a blocked invitation.
    attendee_emails = [e for e in (args.get("attendees") or []) if e]
    refusal = _dnc_refusal(
        "gws_calendar_create", *attendee_emails, run_id=run_id, tenant_id=tenant_id
    )
    if refusal is not None:
        return refusal

    calendar_id, calendar_kind = _resolve_calendar(args)
    owner_email = _resolve_owner_email()

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
            return {
                "status": "deduped",
                "calendar": _calendar_block(calendar_id, calendar_kind),
                "existing_event_id": dup.get("id"),
                "summary": dup.get("summary"),
                "start": existing_start,
                "htmlLink": dup.get("htmlLink"),
                "reason": (
                    "An event with a matching title and overlapping attendees "
                    "already exists within ±14 days. Not creating a duplicate. "
                    "Pass force=true to bypass this check."
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
    attendees = [{"email": e} for e in attendee_emails]
    if owner_email and not any(a["email"].lower() == owner_email for a in attendees):
        attendees.append({"email": owner_email})
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
        cal_result["invitations_sent"] = bool(attendees) and send_updates != "none"
        cal_result["attendees_notified"] = (
            [a["email"] for a in attendees] if cal_result["invitations_sent"] else []
        )
    return cal_result


def _calendar_delete(args: dict[str, Any]) -> dict[str, Any]:
    """Remove an event and email the attendees a cancellation."""
    import json as _json

    event_id = args.get("event_id", "")
    if not event_id:
        return {"error": "event_id is required"}
    calendar_id, calendar_kind = _resolve_calendar(args)
    # A cancellation nobody is told about is not a cancellation: the
    # attendees keep the slot and turn up.
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
                    "sendUpdates": _send_updates(),
                }
            ),
        ]
    )
    if isinstance(deleted, dict) and "error" not in deleted:
        deleted["calendar"] = _calendar_block(calendar_id, calendar_kind)
        deleted["event_id"] = event_id
        deleted["cancellations_sent"] = _send_updates() != "none"
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


# Register all GWS tools as async handlers that delegate to sync _handle_gws_tool
async def _gws_handler(
    args: dict[str, Any], ctx: ToolContext, *, tool_name: str = ""
) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _benchmark_refusal(tool_name)
    return await asyncio.to_thread(
        _handle_gws_tool, tool_name, args, run_id=ctx.run_id, tenant_id=ctx.tenant_id
    )


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
