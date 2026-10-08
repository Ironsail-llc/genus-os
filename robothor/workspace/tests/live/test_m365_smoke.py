"""LIVE Microsoft 365 smoke suite: every open "verify against a live tenant" question.

Runs against a real dev/trial tenant ONLY when asked (see
``docs/workspace/microsoft365.md`` > *Live verification*)::

    GENUS_LIVE_M365=1 GENUS_LIVE_M365_TENANTS=<directory-id> ... \\
        pytest robothor/workspace/tests/live -m live_m365 -p no:cacheprovider

Everywhere else (CI, a developer's ``pytest``) every test here is SKIPPED by
the module ``skipif`` before a fixture runs, so nothing reads a credential or
opens a socket. ``robothor/workspace/tests/test_live_harness.py`` proves that.

The guards in ``harness.py`` hold even when it runs: a tenant not in
``GENUS_LIVE_M365_TENANTS`` is refused before the first request, and every
request passes a fence that allows only the configured assistant and owner
mailboxes (the canary may only be read, to prove its 403). Every test tags
what it writes with the run tag and deletes it; the session sweeps the tag.

Each test answers one question from the runbook's checklist; a failure is a
finding about Exchange, not flakiness -- read its message before retrying.
"""

from __future__ import annotations

import base64
import contextlib
import email.message
import email.utils
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import pytest

from robothor.workspace.errors import NotFound, PermissionDenied, PreconditionFailed
from robothor.workspace.tests.live.harness import addresses_of, poll, skip_reason, write_sample

pytestmark = [
    pytest.mark.live_m365,
    pytest.mark.skipif(skip_reason() is not None, reason=skip_reason() or ""),
    pytest.mark.timeout(900),
]

ZONE = "America/New_York"
WINDOWS_ZONE = "Eastern Standard Time"


# ── helpers ──────────────────────────────────────────────────────────────────


def _q(value: str) -> str:
    return quote(value, safe="")


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mime(sender: str, to: list[str], subject: str, body: str) -> str:
    """The base64url RFC 5322 message a gws handler hands a provider."""
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(usegmt=True)
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")


def _mail(mailbox: str, graph: Any) -> Any:
    from robothor.workspace.microsoft.mail import GraphMail

    return GraphMail(mailbox, graph=graph)


def _calendar(cfg: Any, graph: Any) -> Any:
    from robothor.workspace.microsoft.calendar import GraphCalendar

    return GraphCalendar(
        graph,
        assistant_mailbox=cfg.assistant,
        owner_mailbox=cfg.owner,
        default_timezone=lambda: ZONE,
    )


def _slot(days: int = 3, hour: int = 15, minutes: int = 30) -> tuple[str, str]:
    """A local (ZONE) start/end ``days`` from now, as offset-less ISO strings."""
    day = (datetime.now(UTC) + timedelta(days=days)).date()
    # Offset-less on purpose: the provider reads it in the event's zone.
    start = datetime(day.year, day.month, day.day, hour, 0)  # noqa: DTZ001
    return start.isoformat(), (start + timedelta(minutes=minutes)).isoformat()


def _event(tag: str, what: str, **extra: Any) -> dict[str, Any]:
    start, end = _slot(extra.pop("days", 3), extra.pop("hour", 15))
    return {
        "summary": f"{tag} {what}",
        "start": {"dateTime": start, "timeZone": ZONE},
        "end": {"dateTime": end, "timeZone": ZONE},
        **extra,
    }


async def _inbox_message(graph: Any, mailbox: str, needle: str, *, since: datetime) -> Any:
    """The newest inbox message whose subject contains ``needle``, or None."""
    items = await graph.get_all(
        f"/users/{mailbox}/mailFolders/inbox/messages",
        {
            "$filter": f"receivedDateTime ge {_iso(since - timedelta(minutes=2))}",
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,conversationId,isRead,from,toRecipients,ccRecipients",
            "$top": "50",
        },
        max_items=200,
    )
    for item in items:
        if needle in str(item.get("subject") or ""):
            return item
    return None


async def _deliver(cfg: Any, graph: Any, janitor: list[str], subject: str, body: str) -> Any:
    """Owner -> assistant, then wait until the assistant's inbox has it."""
    since = datetime.now(UTC)
    sent = await _mail(cfg.owner, graph).send(_mime(cfg.owner, [cfg.assistant], subject, body))
    janitor.append(f"/users/{cfg.owner}/messages/{_q(sent['id'])}")
    received = await poll(
        lambda: _inbox_message(graph, cfg.assistant, subject, since=since),
        what="the owner's message in the assistant inbox",
    )
    janitor.append(f"/users/{cfg.assistant}/messages/{_q(received['id'])}")
    return received


async def _organiser_event(cfg: Any, graph: Any, janitor: list[str], event: dict) -> Any:
    """Create on the OWNER's calendar (the owner organises); cleaned up after."""
    cal = _calendar(cfg, graph)
    ref = cal.resolve("operator")
    created = await cal.create(ref, event, conference=False, send_updates="all")
    janitor.append(f"/users/{cfg.owner}/events/{_q(created['id'])}")
    return ref, created


async def _attendee_copy(cfg: Any, graph: Any, subject: str) -> Any:
    """The assistant's copy of an invitation, once Exchange has delivered it."""
    now = datetime.now(UTC)

    async def probe() -> Any:
        items = await graph.get_all(
            f"/users/{cfg.assistant}/calendar/calendarView",
            {
                "startDateTime": _iso(now - timedelta(days=1)),
                "endDateTime": _iso(now + timedelta(days=40)),
                "$select": "id,subject,responseStatus,isCancelled,seriesMasterId,type",
                "$top": "100",
            },
            max_items=500,
        )
        return next((i for i in items if subject in str(i.get("subject") or "")), None)

    return await poll(probe, what="the invitation on the assistant's calendar")


# ── auth ─────────────────────────────────────────────────────────────────────


async def test_auth_certificate_rs256_with_both_thumbprints(graph_factory, live_config) -> None:
    """Entra accepts the RS256 assertion carrying x5t#S256 AND the SHA-1 x5t."""
    graph = await graph_factory(algorithm="RS256")
    answer = await graph.get(f"/users/{live_config.assistant}/mailFolders/inbox", {"$select": "id"})
    assert answer.get("id"), "a token from the RS256 assertion should read the assistant inbox"


async def test_auth_certificate_ps256_fallback(graph_factory, live_config) -> None:
    """The fallback: a PS256 assertion with x5t#S256 only (no SHA-1 thumbprint)."""
    graph = await graph_factory(algorithm="PS256")
    answer = await graph.get(f"/users/{live_config.owner}/calendar", {"$select": "id"})
    assert answer.get("id"), "a token from the PS256 assertion should read the owner calendar"


# ── scope ────────────────────────────────────────────────────────────────────


async def test_scope_canary_mailbox_is_403_access_denied(graph, live_config) -> None:
    """An out-of-scope mailbox answers 403 ErrorAccessDenied -- not 404 -- for mail AND calendar."""
    for path in ("/mailFolders/inbox/messages", "/calendar/events"):
        with pytest.raises(PermissionDenied) as caught:
            await graph.get(f"/users/{live_config.canary}{path}", {"$top": "1", "$select": "id"})
        assert caught.value.status == 403, f"{path}: expected 403, got {caught.value.status}"
        assert caught.value.code == "ErrorAccessDenied", f"{path}: code {caught.value.code!r}"


# ── mail ─────────────────────────────────────────────────────────────────────


async def test_mail_send_keeps_immutable_id_readable(graph, live_config, run_tag, janitor) -> None:
    """draft -> send: the draft's immutable id still reads the message once it is in Sent Items."""
    mail = _mail(live_config.assistant, graph)
    subject = f"{run_tag} immutable id"
    sent = await mail.send(_mime(live_config.assistant, [live_config.owner], subject, "body"))
    janitor.append(f"/users/{live_config.assistant}/messages/{_q(sent['id'])}")
    assert sent["id"] and sent["threadId"]

    async def in_sent() -> Any:
        try:
            message = await mail.get_message(sent["id"], fmt="metadata")
        except NotFound:
            return None
        return message if "SENT" in message["labelIds"] else None

    message = await poll(in_sent, what="the sent message under its draft id in Sent Items")
    assert message["id"] == sent["id"], "the immutable id changed when the draft was sent"
    assert message["threadId"] == sent["threadId"]
    # The owner received it, so the cleanup also covers the owner's copy.
    since = datetime.now(UTC) - timedelta(minutes=5)
    received = await poll(
        lambda: _inbox_message(graph, live_config.owner, subject, since=since),
        what="the message in the owner inbox",
    )
    janitor.append(f"/users/{live_config.owner}/messages/{_q(received['id'])}")


async def test_mail_reply_all_stays_in_conversation(graph, live_config, run_tag, janitor) -> None:
    """createReplyAll + PATCH recipients + send stays in the conversation, with ONLY our recipients."""
    cfg = live_config
    inbound = await _deliver(cfg, graph, janitor, f"{run_tag} reply-all", "please reply")
    conversation = inbound["conversationId"]
    mail = _mail(cfg.assistant, graph)
    reply = _mime(cfg.assistant, [cfg.owner], f"Re: {run_tag} reply-all", "the reply")
    sent = await mail.reply(reply, thread_id=conversation)
    janitor.append(f"/users/{cfg.assistant}/messages/{_q(sent['id'])}")
    assert sent["threadId"] == conversation, "the reply draft left the conversation"

    async def sent_copy() -> Any:
        try:
            raw = await graph.get(
                f"/users/{cfg.assistant}/messages/{_q(sent['id'])}",
                {"$select": "id,conversationId,isDraft,toRecipients,ccRecipients,bccRecipients"},
            )
        except NotFound:
            return None
        return None if raw.get("isDraft") else raw

    raw = await poll(sent_copy, what="the sent reply")
    assert raw["conversationId"] == conversation
    assert addresses_of(raw.get("toRecipients")) == [cfg.owner]
    assert addresses_of(raw.get("ccRecipients")) == []
    assert addresses_of(raw.get("bccRecipients")) == []


async def test_mail_conversation_thread_avoids_inefficient_filter(
    graph, live_config, run_tag, janitor
) -> None:
    """A conversationId $filter with no $orderby reads the thread (no InefficientFilter), oldest first."""
    cfg = live_config
    inbound = await _deliver(cfg, graph, janitor, f"{run_tag} thread", "first")
    mail = _mail(cfg.assistant, graph)
    sent = await mail.reply(
        _mime(cfg.assistant, [cfg.owner], f"Re: {run_tag} thread", "second"),
        thread_id=inbound["conversationId"],
    )
    janitor.append(f"/users/{cfg.assistant}/messages/{_q(sent['id'])}")

    async def both() -> Any:
        thread = await mail.get_thread(inbound["conversationId"], fmt="metadata")
        ids = [m["id"] for m in thread["messages"]]
        return thread if inbound["id"] in ids and sent["id"] in ids else None

    thread = await poll(both, what="both messages in the thread read")
    ids = [m["id"] for m in thread["messages"]]
    assert ids.index(inbound["id"]) < ids.index(sent["id"]), "the thread is not oldest first"


async def test_mail_search_phrase_quoting(graph, live_config, run_tag, janitor) -> None:
    """A Gmail "quoted phrase" becomes a KQL phrase: the words in order match, out of order do not."""
    from robothor.workspace.query import parse_query

    cfg = live_config
    hit = await _deliver(cfg, graph, janitor, f"{run_tag} amber falcon harbour", "phrase")
    miss = await _deliver(cfg, graph, janitor, f"{run_tag} harbour falcon amber", "decoy")
    mail = _mail(cfg.assistant, graph)
    query = parse_query(f'"{run_tag} amber falcon harbour"')

    async def indexed() -> Any:
        found = await mail.search(query, max_results=10)
        ids = {m["id"] for m in found["messages"]}
        return ids if hit["id"] in ids else None

    ids = await poll(indexed, timeout=600, interval=15, what="the search index to include the hit")
    assert miss["id"] not in ids, "the phrase matched the same words out of order"


async def test_mail_labels_categories_round_trip(graph, live_config, run_tag, janitor) -> None:
    """A label is an Outlook category; STARRED/UNREAD map to flag/isRead; all round-trip."""
    cfg = live_config
    inbound = await _deliver(cfg, graph, janitor, f"{run_tag} labels", "label me")
    mail = _mail(cfg.assistant, graph)
    category = f"{run_tag}-label"
    await mail.modify(inbound["id"], add_labels=[category, "STARRED"], remove_labels=["UNREAD"])
    message = await mail.get_message(inbound["id"], fmt="metadata")
    assert category in message["labelIds"]
    assert "STARRED" in message["labelIds"]
    assert "UNREAD" not in message["labelIds"]
    raw = await graph.get(
        f"/users/{cfg.assistant}/messages/{_q(inbound['id'])}", {"$select": "categories"}
    )
    assert category in raw.get("categories", []), "the category did not reach Exchange as written"

    await mail.modify(inbound["id"], add_labels=["UNREAD"], remove_labels=[category, "STARRED"])
    message = await mail.get_message(inbound["id"], fmt="metadata")
    assert category not in message["labelIds"]
    assert "STARRED" not in message["labelIds"]
    assert "UNREAD" in message["labelIds"]


async def test_mail_internet_message_headers_order(
    graph, live_config, run_tag, janitor, capture_scrubber
) -> None:
    """internetMessageHeaders come back as an ordered list, newest Received first; the
    shaped message keeps that order; an Authentication-Results sample is captured, scrubbed."""
    cfg = live_config
    inbound = await _deliver(cfg, graph, janitor, f"{run_tag} headers", "headers")
    raw = await graph.get(
        f"/users/{cfg.assistant}/messages/{_q(inbound['id'])}",
        {"$select": "internetMessageHeaders"},
    )
    headers = raw.get("internetMessageHeaders")
    assert isinstance(headers, list) and headers, "no internetMessageHeaders on a received message"
    assert all(isinstance(h, dict) and "name" in h and "value" in h for h in headers)

    received = [h["value"] for h in headers if h["name"].lower() == "received"]
    stamps = [
        email.utils.parsedate_to_datetime(v.rsplit(";", 1)[1].strip()) for v in received if ";" in v
    ]
    assert stamps == sorted(stamps, reverse=True), "Received headers are not newest first"

    shaped = await _mail(cfg.assistant, graph).get_message(inbound["id"], fmt="full")
    # The shaper writes From/To/Cc/Subject/Date/Message-ID itself, then passes
    # every other Graph header through -- in Graph's order.
    synthesized = {"from", "to", "cc", "subject", "date", "message-id"}
    passed_through = [h["name"] for h in headers if h["name"].lower() not in synthesized]
    shaped_tail = [
        h["name"] for h in shaped["payload"]["headers"] if h["name"].lower() not in synthesized
    ]
    assert shaped_tail == passed_through, "the shaper reordered or dropped headers"

    auth = [h for h in headers if h["name"].lower() == "authentication-results"]
    if capture_scrubber is not None and cfg.capture_dir:
        write_sample(
            cfg.capture_dir,
            "authentication-results",
            {"internetMessageHeaders": auth, "names_in_order": [h["name"] for h in headers]},
            capture_scrubber,
        )


# ── calendar ─────────────────────────────────────────────────────────────────


async def test_calendar_iana_timezone_accepted_on_create(graph, live_config, run_tag, janitor):
    """Exchange takes an IANA timeZone on create -- one POST, no Windows-name retry."""
    cal = _calendar(live_config, graph)
    ref = cal.resolve("operator")
    before = len(graph.fence.passed)
    created = await cal.create(ref, _event(run_tag, "iana"), conference=False, send_updates="all")
    janitor.append(f"/users/{live_config.owner}/events/{_q(created['id'])}")
    posts = [p for m, p in graph.fence.passed[before:] if m == "POST" and p.endswith("/events")]
    assert len(posts) == 1, "the IANA create was refused and retried with a Windows zone"
    assert created["start"]["timeZone"] == ZONE


async def test_calendar_windows_timezone_fallback_path(graph, live_config, run_tag, janitor):
    """The fallback body (Windows zone names) is accepted and reads back as the IANA zone."""
    from robothor.workspace.microsoft.calendar import (
        _with_windows_zones,
        event_to_graph,
        to_normalized,
    )

    body = _with_windows_zones(
        event_to_graph(_event(run_tag, "windows zone"), zone=ZONE, conference=False)
    )
    assert body["start"]["timeZone"] == WINDOWS_ZONE
    raw = await graph.post(f"/users/{live_config.owner}/calendar/events", body)
    janitor.append(f"/users/{live_config.owner}/events/{_q(raw['id'])}")
    shaped = to_normalized(raw, live_config.owner)
    assert shaped["start"]["timeZone"] == ZONE


async def test_calendar_original_start_timezone_format(graph, live_config, run_tag, janitor):
    """originalStartTimeZone comes back in a form timezones.to_iana maps (IANA or Windows)."""
    from robothor.workspace.microsoft.timezones import to_iana

    ref, created = await _organiser_event(live_config, graph, janitor, _event(run_tag, "zone fmt"))
    raw = await graph.get(
        f"/users/{live_config.owner}/events/{_q(created['id'])}",
        {"$select": "originalStartTimeZone,originalEndTimeZone,start"},
    )
    original = raw.get("originalStartTimeZone")
    assert original, "no originalStartTimeZone on the event"
    assert to_iana(original) == ZONE, f"originalStartTimeZone {original!r} does not map to {ZONE}"
    assert raw["start"]["timeZone"] == "UTC", "the UTC Prefer was not honoured"


async def test_calendar_412_on_concurrent_edit(graph, live_config, run_tag, janitor) -> None:
    """A PATCH with a stale If-Match answers 412 (PreconditionFailed)."""
    cal = _calendar(live_config, graph)
    ref, created = await _organiser_event(live_config, graph, janitor, _event(run_tag, "412"))
    stale = (await cal.get(ref, created["id"]))["etag"]
    await cal.conditional_patch(
        ref, created["id"], {"location": f"{run_tag} room A"}, etag=stale, send_updates="all"
    )
    with pytest.raises(PreconditionFailed):
        await cal.conditional_patch(
            ref, created["id"], {"location": f"{run_tag} room B"}, etag=stale, send_updates="all"
        )
    assert (await cal.get(ref, created["id"])).get("location") == f"{run_tag} room A"


async def test_calendar_412_on_recurring_occurrence(graph, live_config, run_tag, janitor) -> None:
    """One occurrence of a series honours If-Match the same way: a stale etag is 412."""
    cal = _calendar(live_config, graph)
    event = _event(
        run_tag,
        "series",
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA,SU;COUNT=3"],
        days=2,
    )
    ref, master = await _organiser_event(live_config, graph, janitor, event)
    now = datetime.now(UTC)
    listed = await cal.list(
        ref,
        {
            "time_min": _iso(now),
            "time_max": _iso(now + timedelta(days=14)),
            "single_events": True,
            "order_by": "startTime",
        },
    )
    occurrences = [e for e in listed["items"] if e.get("recurringEventId")]
    occurrences = [e for e in occurrences if run_tag in str(e.get("summary") or "")]
    assert len(occurrences) == 3, f"expected 3 occurrences, saw {len(occurrences)}"
    occurrence = occurrences[1]
    stale = (await cal.get(ref, occurrence["id"]))["etag"]
    await cal.conditional_patch(
        ref, occurrence["id"], {"location": f"{run_tag} moved"}, etag=stale, send_updates="all"
    )
    with pytest.raises(PreconditionFailed):
        await cal.conditional_patch(
            ref, occurrence["id"], {"location": f"{run_tag} again"}, etag=stale, send_updates="all"
        )


async def test_calendar_rsvp_accept_decline_tentative(graph, live_config, run_tag, janitor):
    """The attendee's accept / tentativelyAccept / decline each reach the organiser's copy."""
    cfg = live_config
    cal = _calendar(cfg, graph)
    subject_event = _event(run_tag, "rsvp", attendees=[{"email": cfg.assistant}])
    ref, created = await _organiser_event(cfg, graph, janitor, subject_event)
    invite = await _attendee_copy(cfg, graph, subject_event["summary"])
    own = cal.resolve("own")

    for response in ("tentative", "accepted", "declined"):
        await cal.rsvp(own, invite["id"], response, send_response=True)

        async def landed(response: str = response) -> Any:
            event = await cal.get(ref, created["id"])
            me = [a for a in event.get("attendees", []) if a["email"].lower() == cfg.assistant]
            return event if me and me[0]["responseStatus"] == response else None

        await poll(landed, what=f"the organiser to see {response!r}")


async def test_calendar_organiser_cancel_notifies_attendee(graph, live_config, run_tag, janitor):
    """The organiser's delete of a meeting goes to /cancel, and the attendee receives the cancellation."""
    cfg = live_config
    cal = _calendar(cfg, graph)
    event = _event(run_tag, "cancel me", attendees=[{"email": cfg.assistant}])
    ref, created = await _organiser_event(cfg, graph, janitor, event)
    await _attendee_copy(cfg, graph, event["summary"])
    since = datetime.now(UTC)
    result = await cal.delete(ref, created["id"], send_updates="all")
    assert result["method"] == "cancel"
    notice = await poll(
        lambda: _cancellation(graph, cfg.assistant, event["summary"], since),
        what="the cancellation in the assistant inbox",
    )
    janitor.append(f"/users/{cfg.assistant}/messages/{_q(notice['id'])}")


async def test_calendar_organiser_plain_delete_also_notifies(graph, live_config, run_tag, janitor):
    """Observed, not assumed: does a plain organiser DELETE of a meeting also notify attendees?

    Microsoft documents that it does. The provider uses /cancel regardless;
    this pins the documented behaviour so a change in Exchange is noticed.
    """
    cfg = live_config
    event = _event(run_tag, "plain delete", attendees=[{"email": cfg.assistant}])
    ref, created = await _organiser_event(cfg, graph, janitor, event)
    await _attendee_copy(cfg, graph, event["summary"])
    since = datetime.now(UTC)
    await graph.delete(f"/users/{cfg.owner}/events/{_q(created['id'])}")
    notice = await poll(
        lambda: _cancellation(graph, cfg.assistant, event["summary"], since),
        what="a cancellation after a plain DELETE",
    )
    janitor.append(f"/users/{cfg.assistant}/messages/{_q(notice['id'])}")


async def _cancellation(graph: Any, mailbox: str, summary: str, since: datetime) -> Any:
    items = await graph.get_all(
        f"/users/{mailbox}/messages",
        {
            "$filter": f"receivedDateTime ge {_iso(since - timedelta(minutes=2))}",
            "$select": "id,subject,meetingMessageType",
            "$top": "50",
        },
        max_items=200,
    )
    for item in items:
        if summary in str(item.get("subject") or "") and (
            item.get("meetingMessageType") == "meetingCancelled"
            or str(item.get("subject") or "").lower().startswith(("canceled", "cancelled"))
        ):
            return item
    return None


async def test_calendar_all_day_under_utc_prefer(graph, live_config, run_tag, janitor) -> None:
    """An all-day event reads back as the same dates (end exclusive) despite Prefer UTC."""
    cal = _calendar(live_config, graph)
    first = (datetime.now(UTC) + timedelta(days=5)).date()
    event = {
        "summary": f"{run_tag} all day",
        "start": {"date": first.isoformat(), "timeZone": ZONE},
        "end": {"date": (first + timedelta(days=1)).isoformat(), "timeZone": ZONE},
    }
    ref, created = await _organiser_event(live_config, graph, janitor, event)
    read = await cal.get(ref, created["id"])
    assert read["start"] == {"date": first.isoformat()}
    assert read["end"] == {"date": (first + timedelta(days=1)).isoformat()}
    raw = await graph.get(
        f"/users/{live_config.owner}/events/{_q(created['id'])}", {"$select": "isAllDay,start"}
    )
    assert raw["isAllDay"] is True
    assert date.fromisoformat(raw["start"]["dateTime"][:10]) in (first, first - timedelta(days=1))


async def test_calendar_attendee_replace_keeps_rsvps(graph, live_config, run_tag, janitor):
    """PATCHing the whole attendees list (what the merge loop sends) keeps an existing RSVP."""
    cfg = live_config
    cal = _calendar(cfg, graph)
    event = _event(run_tag, "keep rsvp", attendees=[{"email": cfg.assistant}])
    ref, created = await _organiser_event(cfg, graph, janitor, event)
    invite = await _attendee_copy(cfg, graph, event["summary"])
    await cal.rsvp(cal.resolve("own"), invite["id"], "accepted", send_response=True)

    async def accepted() -> Any:
        current = await cal.get(ref, created["id"])
        me = [a for a in current.get("attendees", []) if a["email"].lower() == cfg.assistant]
        return current if me and me[0]["responseStatus"] == "accepted" else None

    current = await poll(accepted, what="the organiser to see the accept")
    replaced = [{"email": a["email"]} for a in current["attendees"] if not a.get("organizer")]
    await cal.conditional_patch(
        ref,
        created["id"],
        {"attendees": replaced, "location": f"{run_tag} after replace"},
        etag=current["etag"],
        send_updates="all",
    )
    after = await cal.get(ref, created["id"])
    me = [a for a in after.get("attendees", []) if a["email"].lower() == cfg.assistant]
    assert me and me[0]["responseStatus"] == "accepted", "replacing the attendees reset the RSVP"


async def test_calendar_plain_text_description_round_trip(graph, live_config, run_tag, janitor):
    """A plain-text description reads back as the same text (body requested as text)."""
    cal = _calendar(live_config, graph)
    description = f'{run_tag}\nline two: 5 < 6 & "quotes"\n\nlast line'
    ref, created = await _organiser_event(
        live_config, graph, janitor, _event(run_tag, "description", description=description)
    )
    read = await cal.get(ref, created["id"])
    got = (read.get("description") or "").replace("\r\n", "\n").strip()
    assert got == description, "the description did not round-trip as plain text"


# ── ingest ───────────────────────────────────────────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, stream: str, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((stream, event_type, payload))

    def ids(self) -> set[str]:
        return {str(p.get("id")) for _, _, p in self.events}


def _mail_ingestor(cfg: Any, graph: Any, store: Any, publish: Any) -> Any:
    from robothor.workspace.ingest.microsoft import GraphMailIngestor

    return GraphMailIngestor(
        graph=graph, mailbox=cfg.assistant, tenant_id="live-smoke", store=store, publish=publish
    )


def _spoil(link: str) -> str:
    """The same deltaLink with a token Exchange never issued."""
    parts = urlsplit(link)
    query = [
        (k, "AAAAinvalid-genus-live-token" if "token" in k.lower() else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(query, safe="$'(),")))


async def _ingest_until(ingestor: Any, recorder: _Recorder, subject: str) -> Any:
    async def probe() -> Any:
        report = await ingestor.run_once()
        hit = [p for _, _, p in recorder.events if subject in str(p.get("subject") or "")]
        return (report, hit[0]) if hit else None

    return await poll(probe, what="the delta to publish the new message")


async def test_ingest_delta_initial_then_incremental(graph, live_config, run_tag, janitor):
    """The first round is a baseline ('initial'); a new message arrives through 'incremental'."""
    from robothor.workspace.ingest.state import MemoryIngestStore

    store, recorder = MemoryIngestStore(), _Recorder()
    ingestor = _mail_ingestor(live_config, graph, store, recorder)
    first = await ingestor.run_once()
    assert first.mode == "initial"
    subject = f"{run_tag} delta incremental"
    inbound = await _deliver(live_config, graph, janitor, subject, "delta")
    report, payload = await _ingest_until(ingestor, recorder, subject)
    assert report.mode == "incremental"
    assert payload["id"] == inbound["id"]
    assert payload.get("provider") == "microsoft365"
    published = [p for _, _, p in recorder.events if p["id"] == inbound["id"]]
    assert len(published) == 1, "the message was published more than once"


async def test_ingest_forced_resync_does_not_replay(graph, live_config, run_tag, janitor):
    """An invalid deltaLink makes Graph drop the delta; the resync publishes nothing already seen."""
    from robothor.workspace.ingest.microsoft import PROVIDER, needs_resync
    from robothor.workspace.ingest.state import MemoryIngestStore

    cfg = live_config
    store, recorder = MemoryIngestStore(), _Recorder()
    ingestor = _mail_ingestor(cfg, graph, store, recorder)
    await ingestor.run_once()
    subject = f"{run_tag} delta resync"
    await _deliver(cfg, graph, janitor, subject, "before the resync")
    await _ingest_until(ingestor, recorder, subject)
    before = recorder.ids()

    state = await store.load("live-smoke", PROVIDER, cfg.assistant, "mail")
    assert state is not None and state.delta_link
    await store.save(state.with_(delta_link=_spoil(state.delta_link)))
    try:
        report = await ingestor.run_once()
    except Exception as exc:  # pragma: no cover - a live finding, reported precisely
        status = getattr(exc, "status", None)
        code = getattr(exc, "code", None)
        pytest.fail(
            f"a spoiled deltaLink raised instead of resyncing: status={status} code={code} "
            f"(needs_resync={needs_resync(exc)}); add Graph's answer to _RESYNC_CODES",
            pytrace=False,
        )
    assert report.mode == "resync"
    replayed = before & {str(p.get("id")) for _, _, p in recorder.events[len(before) :]}
    assert not replayed, f"{len(replayed)} message(s) were published again after the resync"

    # And the explicit path: no deltaLink at all with a high-water mark.
    state = await store.load("live-smoke", PROVIDER, cfg.assistant, "mail")
    await store.save(state.with_(delta_link=""))
    count = len(recorder.events)
    report = await ingestor.run_once()
    assert report.mode == "resync"
    again = {str(p.get("id")) for _, _, p in recorder.events[count:]}
    assert not (again & before), "a resync from the high-water mark replayed old mail"


async def test_ingest_calendar_delta_baseline_then_new(graph, live_config, run_tag, janitor):
    """The calendar delta: the first round publishes nothing; a new event publishes calendar.new."""
    from robothor.workspace.ingest.microsoft import GraphCalendarIngestor
    from robothor.workspace.ingest.state import MemoryIngestStore

    recorder = _Recorder()
    ingestor = GraphCalendarIngestor(
        graph=graph,
        mailbox=live_config.owner,
        tenant_id="live-smoke",
        store=MemoryIngestStore(),
        publish=recorder,
    )
    first = await ingestor.run_once()
    assert first.mode == "initial" and first.published == 0
    event = _event(run_tag, "ingest calendar", days=2)
    await _organiser_event(live_config, graph, janitor, event)

    async def probe() -> Any:
        await ingestor.run_once()
        return [
            (t, p) for _, t, p in recorder.events if event["summary"] in str(p.get("title") or "")
        ]

    hits = await poll(probe, what="calendar.new for the created event")
    assert hits[0][0] == "calendar.new"


# ── doctor ───────────────────────────────────────────────────────────────────


async def test_doctor_m365_checks_green(graph_factory, live_config, monkeypatch) -> None:
    """workspace.m365_* are green on the dev tenant: connection, canary DENIED, identity."""
    from robothor.doctor.checks import workspace_m365
    from robothor.doctor.tests.conftest import make_ctx
    from robothor.settings import reset_settings

    cfg = live_config
    for name, value in {
        "ROBOTHOR_WORKSPACE_PROVIDER": "microsoft365",
        "ROBOTHOR_M365_ASSISTANT_MAILBOX": cfg.assistant,
        "ROBOTHOR_M365_OWNER_MAILBOX": cfg.owner,
        "ROBOTHOR_M365_SCOPE_CANARY_MAILBOX": cfg.canary,
        "ROBOTHOR_AI_EMAIL": cfg.assistant,
    }.items():
        monkeypatch.setenv(name, value)
    reset_settings()

    async def fenced_graph(_tenant: str) -> Any:
        return await graph_factory()

    monkeypatch.setattr(workspace_m365, "_graph", fenced_graph)
    monkeypatch.setattr(workspace_m365, "_vault_connected", lambda _tenant: True)
    by_id = {check.id: check for check in workspace_m365.CHECKS}
    ctx = make_ctx(timeout_s=120.0)
    try:
        for check_id in (
            "workspace.m365_connection",
            "workspace.m365_scope",
            "workspace.m365_canary_configured",
            "workspace.m365_assistant_identity",
        ):
            rows = await by_id[check_id].run(ctx)
            for row in rows if isinstance(rows, list) else [rows]:
                assert row.status == "pass", f"{check_id} {row.sub_id or ''}: {row.detail}"
        # Recommended, and it compares the dev tenant with THIS machine's
        # ROBOTHOR_TIMEZONE: it must run against Graph and answer, not pass.
        tz = await by_id["workspace.m365_timezone"].run(ctx)
        assert tz.status in ("pass", "fail", "skip") and tz.detail
        assert "graph HTTP 5" not in tz.detail, f"workspace.m365_timezone: {tz.detail}"
    finally:
        with contextlib.suppress(Exception):
            reset_settings()
