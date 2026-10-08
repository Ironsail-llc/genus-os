"""Microsoft 365 mail (GraphMail) against a fake Exchange Online tenant."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from email.mime.text import MIMEText

import httpx
import pytest

from robothor.workspace.errors import NotFound, UnknownEffect, Unsupported, WorkspaceError
from robothor.workspace.microsoft.graph import GraphClient
from robothor.workspace.microsoft.mail import (
    SUPPORTED_SEARCH,
    GraphMail,
    labels_for,
    translate_query,
)
from robothor.workspace.query import parse_query
from robothor.workspace.tests.fake_graph import FakeGraphTenant
from robothor.workspace.tests.fake_graph_mail import FakeExchangeMail, install_mail

ME = "assistant@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"
DAVE = "dave@example.com"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class StaticToken:
    def __init__(self, tenant: FakeGraphTenant) -> None:
        tenant.issued_tokens.append("tok")

    async def token(self) -> str:
        return "tok"


@pytest.fixture
def tenant() -> FakeGraphTenant:
    return FakeGraphTenant()


@pytest.fixture
def exchange(tenant: FakeGraphTenant) -> FakeExchangeMail:
    return install_mail(tenant)


@pytest.fixture
async def mail(tenant: FakeGraphTenant, exchange: FakeExchangeMail):
    graph = GraphClient(StaticToken(tenant), transport=tenant.transport())
    yield GraphMail(ME, graph=graph, now=lambda: NOW)
    await graph.aclose()


def _raw(to: str, subject: str = "Hi", body: str = "Hello", cc: str = "", html: bool = False):
    msg = MIMEText(body, _subtype="html" if html else "plain")
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _addresses(recipients: list[dict]) -> list[str]:
    return [r["emailAddress"]["address"] for r in recipients]


# ── query translation ─────────────────────────────────────────────────


def _t(q: str):
    return translate_query(parse_query(q), now=NOW)


def test_filterable_query_uses_filter_and_server_order() -> None:
    plan = _t("from:bob@example.com is:unread")
    assert plan.search is None
    assert plan.orderby == "receivedDateTime desc"
    # Graph needs the orderby property first in $filter, or it answers
    # InefficientFilter; a no-op lower bound puts it there.
    assert plan.filter == (
        "receivedDateTime ge 1900-01-01T00:00:00Z"
        " and from/emailAddress/address eq 'bob@example.com'"
        " and isRead eq false"
    )


def test_dates_and_relative_dates() -> None:
    assert _t("after:2026/10/01 before:2026-10-05").filter == (
        "receivedDateTime ge 2026-10-01T00:00:00Z and receivedDateTime lt 2026-10-05T00:00:00Z"
    )
    assert _t("newer_than:2d").filter == "receivedDateTime ge 2026-10-05T12:00:00Z"
    assert _t("older_than:1y").filter == "receivedDateTime lt 2025-10-07T12:00:00Z"


def test_flags_labels_folders_and_attachments() -> None:
    plan = _t("in:inbox is:starred is:important has:attachment label:Clients -is:unread")
    assert plan.folder == "inbox"
    assert plan.filter == (
        "receivedDateTime ge 1900-01-01T00:00:00Z"
        " and flag/flagStatus eq 'flagged'"
        " and importance eq 'high'"
        " and hasAttachments eq true"
        " and categories/any(c: c eq 'Clients')"
        " and isRead eq true"
    )
    assert _t("in:sent").folder == "sentitems"
    assert _t("in:trash").folder == "deleteditems"
    assert _t("in:spam").folder == "junkemail"
    assert _t("in:anywhere").folder is None
    assert _t("in:anywhere").exclude_folders == ()
    assert _t("is:unread").exclude_folders == ("deleteditems", "junkemail")


def test_any_of_filterable_terms_is_an_or_group() -> None:
    assert _t("{from:bob@example.com from:carol@example.com}").filter == (
        "receivedDateTime ge 1900-01-01T00:00:00Z and "
        "(from/emailAddress/address eq 'bob@example.com'"
        " or from/emailAddress/address eq 'carol@example.com')"
    )


def test_quotes_are_escaped_in_filters() -> None:
    assert "categories/any(c: c eq 'O''Brien')" in (_t("label:O'Brien").filter or "")


def test_text_terms_switch_to_kql_and_keep_structured_terms_exact() -> None:
    plan = _t('subject:invoice "board pack" from:bob@example.com is:unread')
    assert plan.filter is None and plan.orderby is None
    assert plan.search == 'subject:invoice AND "board pack" AND from:bob@example.com'
    # is:unread has no KQL form: applied to every result, never dropped.
    assert len(plan.predicates) == 2


@pytest.mark.parametrize(
    "query",
    [
        "foo:bar",  # an operator the parser does not know
        "is:snoozed",
        "has:drive",
        "in:somelabel",
        "-subject:invoice",
        "{subject:invoice is:starred}",
        "(a b",
        "in:inbox in:sent",
    ],
)
def test_untranslatable_queries_are_refused_never_widened(query: str) -> None:
    with pytest.raises(Unsupported) as exc:
        _t(query)
    assert "supported" in str(exc.value).lower()


def test_refusal_lists_the_supported_operators() -> None:
    with pytest.raises(Unsupported) as exc:
        _t("foo:bar")
    for operator in ("from:", "subject:", "is:unread", "newer_than:"):
        assert operator in SUPPORTED_SEARCH
        assert operator in str(exc.value)


# ── search ────────────────────────────────────────────────────────────


async def test_search_filters_on_the_server_newest_first(mail, exchange, tenant) -> None:
    old = exchange.deliver(ME, sender=BOB, to=[ME], subject="one")
    exchange.deliver(ME, sender=CAROL, to=[ME], subject="two")
    new = exchange.deliver(ME, sender=BOB, to=[ME], subject="three")
    exchange.deliver(ME, sender=BOB, to=[ME], subject="read", is_read=True)

    out = await mail.search(parse_query("from:bob@example.com is:unread"), max_results=10)

    assert [m["id"] for m in out["messages"]] == [new["id"], old["id"]]
    assert out["messages"][0]["threadId"] == new["conversationId"]
    listed = tenant.requests_matching("GET", rf"/users/{ME}/messages")
    assert listed[-1].url.params["$orderby"] == "receivedDateTime desc"
    assert "$search" not in listed[-1].url.params


async def test_search_with_text_uses_kql_and_sorts_itself(mail, exchange, tenant) -> None:
    a = exchange.deliver(
        ME, sender=BOB, to=[ME], subject="Invoice 1", received="2026-10-01T09:00:00Z"
    )
    b = exchange.deliver(
        ME, sender=BOB, to=[ME], subject="Invoice 2", received="2026-10-03T09:00:00Z"
    )
    exchange.deliver(ME, sender=BOB, to=[ME], subject="Lunch")
    exchange.deliver(ME, sender=CAROL, to=[ME], subject="Invoice 3")

    out = await mail.search(parse_query("subject:invoice from:bob@example.com"), max_results=10)

    assert [m["id"] for m in out["messages"]] == [b["id"], a["id"]]
    request = tenant.requests_matching("GET", rf"/users/{ME}/messages")[-1]
    assert request.url.params["$search"] == '"subject:invoice AND from:bob@example.com"'
    assert "$filter" not in request.url.params and "$orderby" not in request.url.params


async def test_kql_results_are_narrowed_by_structured_terms(mail, exchange) -> None:
    exchange.deliver(ME, sender=BOB, to=[ME], subject="Invoice", is_read=True)
    unread = exchange.deliver(ME, sender=BOB, to=[ME], subject="Invoice again")
    out = await mail.search(parse_query("subject:invoice is:unread"), max_results=10)
    assert [m["id"] for m in out["messages"]] == [unread["id"]]


async def test_search_excludes_trash_and_junk_unless_asked(mail, exchange) -> None:
    kept = exchange.deliver(ME, sender=BOB, to=[ME])
    trashed = exchange.deliver(ME, sender=BOB, to=[ME], folder="deleteditems")
    exchange.deliver(ME, sender=BOB, to=[ME], folder="junkemail")
    out = await mail.search(parse_query("from:bob@example.com"), max_results=10)
    assert [m["id"] for m in out["messages"]] == [kept["id"]]
    out = await mail.search(parse_query("in:trash"), max_results=10)
    assert [m["id"] for m in out["messages"]] == [trashed["id"]]


async def test_search_respects_max_results(mail, exchange) -> None:
    for i in range(5):
        exchange.deliver(ME, sender=BOB, to=[ME], subject=f"m{i}")
    out = await mail.search(parse_query(""), max_results=2)
    assert len(out["messages"]) == 2


async def test_untranslatable_search_makes_no_request(mail, tenant) -> None:
    with pytest.raises(Unsupported):
        await mail.search(parse_query("filename:pdf"), max_results=10)
    assert tenant.requests == []


# ── read ──────────────────────────────────────────────────────────────


async def test_get_message_full_has_the_google_shape(mail, exchange, tenant) -> None:
    msg = exchange.deliver(
        ME,
        sender=BOB,
        sender_name="Bob Example",
        to=[ME, CAROL],
        cc=[DAVE],
        subject="Quarterly",
        body="The numbers are in.",
        attachments=[{"name": "q3.pdf", "contentType": "application/pdf", "size": 1234}],
        flagged=True,
        categories=["Clients"],
    )

    raw = await mail.get_message(msg["id"], fmt="full")
    shaped = mail.shape_message(raw, max_chars=1000)

    assert shaped["id"] == msg["id"]
    assert shaped["thread_id"] == msg["conversationId"]
    assert shaped["from"] == "Bob Example <bob@example.com>"
    assert shaped["to"] == f"{ME}, {CAROL}"
    assert shaped["cc"] == DAVE
    assert shaped["subject"] == "Quarterly"
    assert shaped["message_id"] == msg["internetMessageId"]
    assert shaped["body_text"] == "The numbers are in."
    assert shaped["body_truncated"] is False
    assert shaped["snippet"] == "The numbers are in."
    assert shaped["date"] == "Tue, 06 Oct 2026 09:01:00 +0000"
    assert shaped["attachments"] == [
        {"filename": "q3.pdf", "mime_type": "application/pdf", "size_bytes": 1234}
    ]
    assert shaped["labels"] == ["UNREAD", "INBOX", "STARRED", "Clients"]
    # The handlers' header-reading guards see Gmail-style headers.
    names = [h["name"] for h in raw["payload"]["headers"]]
    assert {"From", "To", "Cc", "Subject", "Message-ID", "Date", "X-Mailer"} <= set(names)
    assert names.count("Date") == 1

    request = tenant.requests_matching("GET", rf"/users/{ME}/messages/.+")[-1]
    prefer = request.headers["prefer"]
    assert 'IdType="ImmutableId"' in prefer
    assert 'outlook.body-content-type="text"' in prefer
    assert "internetMessageHeaders" in request.url.params["$select"]


async def test_metadata_read_skips_the_body(mail, exchange, tenant) -> None:
    msg = exchange.deliver(ME, sender=BOB, to=[ME], body="secret body")
    raw = await mail.get_message(msg["id"], fmt="metadata")
    envelope = mail.shape_envelope(raw)
    assert envelope["id"] == msg["id"] and envelope["from"] == BOB
    request = tenant.requests_matching("GET", rf"/users/{ME}/messages/.+")[-1]
    selected = request.url.params["$select"].split(",")
    assert "body" not in selected


async def test_html_body_falls_back_through_html_to_text(mail, exchange, tenant) -> None:
    msg = exchange.deliver(
        ME,
        sender=BOB,
        to=[ME],
        body="<html><script>alert(1)</script><p>Hello <b>there</b></p></html>",
        content_type="html",
    )

    @tenant.route("GET", rf"/users/{ME}/messages/[^/]+")
    async def ignore_prefer(tenant_, request, match):
        # A server that answers HTML despite the text preference.
        return httpx.Response(200, json=exchange.message(ME, msg["id"]))

    shaped = mail.shape_message(await mail.get_message(msg["id"], fmt="full"), max_chars=500)
    assert "Hello" in shaped["body_text"] and "there" in shaped["body_text"]
    assert "alert" not in shaped["body_text"] and "<" not in shaped["body_text"]


async def test_snippet_is_escaped_like_gmail(mail, exchange) -> None:
    msg = exchange.deliver(ME, sender=BOB, to=[ME], body="a <script> & b")
    shaped = mail.shape_envelope(await mail.get_message(msg["id"], fmt="metadata"))
    assert shaped["snippet"] == "a &lt;script&gt; &amp; b"


async def test_missing_message_is_not_found(mail) -> None:
    with pytest.raises(NotFound):
        await mail.get_message("AAk-nope=", fmt="full")


async def test_thread_is_oldest_first_without_server_order(mail, exchange, tenant) -> None:
    first = exchange.deliver(ME, sender=BOB, to=[ME], subject="Plan")
    conv = first["conversationId"]
    second = exchange.deliver(
        ME, sender=ME, to=[BOB], subject="RE: Plan", conversation_id=conv, folder="sentitems"
    )
    third = exchange.deliver(ME, sender=BOB, to=[ME], subject="RE: Plan", conversation_id=conv)
    exchange.deliver(ME, sender=CAROL, to=[ME], subject="Other")

    thread = await mail.get_thread(conv, fmt="metadata")

    assert thread["id"] == conv
    assert [m["id"] for m in thread["messages"]] == [first["id"], second["id"], third["id"]]
    request = tenant.requests_matching("GET", rf"/users/{ME}/messages")[-1]
    assert request.url.params["$filter"] == f"conversationId eq '{conv}'"
    assert "$orderby" not in request.url.params  # InefficientFilter in production
    headers = {h["name"]: h["value"] for h in thread["messages"][1]["payload"]["headers"]}
    assert headers["From"] == ME


async def test_unknown_thread_is_not_found(mail) -> None:
    with pytest.raises(NotFound):
        await mail.get_thread("AAQkConv-none==", fmt="metadata")


# ── send ──────────────────────────────────────────────────────────────


async def test_send_is_draft_then_send_and_the_id_survives(mail, exchange, tenant) -> None:
    result = await mail.send(_raw(f"{BOB}, {CAROL}", subject="Proposal", cc=DAVE))

    sent = exchange.message(ME, result["id"])  # the SAME id, now in Sent Items
    assert exchange.folder_of(ME, sent) == "sentitems"
    assert sent["isDraft"] is False
    assert result == {
        "id": sent["id"],
        "threadId": sent["conversationId"],
        "labelIds": ["SENT"],
        "internetMessageId": sent["internetMessageId"],
    }
    assert _addresses(sent["toRecipients"]) == [BOB, CAROL]
    assert _addresses(sent["ccRecipients"]) == [DAVE]
    create, send = exchange.writes()
    assert create.headers["content-type"].startswith("text/plain")
    assert create.url.path.endswith("/messages")
    assert send.url.path.endswith(f"/messages/{result['id']}/send")


async def test_send_into_a_thread_replies_in_the_same_conversation(mail, exchange) -> None:
    original = exchange.deliver(ME, sender=BOB, to=[ME], cc=[CAROL], subject="Plan")
    conv = original["conversationId"]

    result = await mail.send(_raw(BOB, subject="Re: Plan", body="On it"), thread_id=conv)

    sent = exchange.message(ME, result["id"])
    assert sent["conversationId"] == conv == result["threadId"]
    assert _addresses(sent["toRecipients"]) == [BOB]
    assert _addresses(sent["ccRecipients"]) == []  # createReplyAll added carol; not approved
    assert sent["body"]["content"] == "On it"
    assert exchange.folder_of(ME, sent) == "sentitems"


async def test_reply_uses_exactly_the_approved_recipients(mail, exchange) -> None:
    first = exchange.deliver(ME, sender=BOB, to=[ME, CAROL], cc=[DAVE], subject="Kickoff")
    conv = first["conversationId"]
    newest = exchange.deliver(
        ME, sender=CAROL, to=[ME, BOB], subject="RE: Kickoff", conversation_id=conv
    )
    exchange.deliver(
        ME,
        sender=ME,
        to=[BOB],
        subject="RE: Kickoff",
        conversation_id=conv,
        folder="drafts",
        is_draft=True,
    )

    result = await mail.reply(
        _raw(f"{BOB}, {CAROL}", subject="Re: Kickoff", body="Thanks"), thread_id=conv
    )

    sent = exchange.message(ME, result["id"])
    assert sent["conversationId"] == conv and result["threadId"] == conv
    assert _addresses(sent["toRecipients"]) == [BOB, CAROL]
    assert _addresses(sent["ccRecipients"]) == []
    assert _addresses(sent["bccRecipients"]) == []
    assert sent["body"] == {"contentType": "text", "content": "Thanks"}
    reply_all = [r for r in exchange.writes() if r.url.path.endswith("/createReplyAll")]
    assert len(reply_all) == 1
    assert reply_all[0].url.path.endswith(f"/messages/{newest['id']}/createReplyAll")
    assert result["labelIds"] == ["SENT"]


async def test_html_body_is_sent_as_html(mail, exchange) -> None:
    original = exchange.deliver(ME, sender=BOB, to=[ME])
    result = await mail.reply(
        _raw(BOB, body="<p>Hi</p>", html=True), thread_id=original["conversationId"]
    )
    assert exchange.message(ME, result["id"])["body"]["contentType"] == "html"


async def test_reply_to_an_unknown_thread_writes_nothing(mail, exchange) -> None:
    with pytest.raises(NotFound):
        await mail.reply(_raw(BOB), thread_id="AAQkConv-none==")
    assert exchange.writes() == []


async def test_send_failure_is_unknown_effect_and_not_retried(mail, exchange, tenant) -> None:
    tenant.fail("POST", rf"/users/{ME}/messages/[^/]+/send", 503)
    with pytest.raises(UnknownEffect):
        await mail.send(_raw(BOB))
    sends = [r for r in exchange.writes() if r.url.path.endswith("/send")]
    assert len(sends) == 1


# ── modify ────────────────────────────────────────────────────────────


async def test_modify_maps_gmail_labels_both_ways(mail, exchange) -> None:
    msg = exchange.deliver(ME, sender=BOB, to=[ME], categories=["Old"])
    mid = msg["id"]

    out = await mail.modify(
        mid, add_labels=["STARRED", "IMPORTANT", "Clients"], remove_labels=["UNREAD", "Old"]
    )
    stored = exchange.message(ME, mid)
    assert stored["isRead"] is True
    assert stored["flag"] == {"flagStatus": "flagged"}
    assert stored["importance"] == "high"
    assert stored["categories"] == ["Clients"]
    assert out == {
        "id": mid,
        "threadId": msg["conversationId"],
        "labelIds": ["INBOX", "STARRED", "IMPORTANT", "Clients"],
    }

    out = await mail.modify(mid, add_labels=["UNREAD"], remove_labels=["STARRED", "IMPORTANT"])
    stored = exchange.message(ME, mid)
    assert stored["isRead"] is False
    assert stored["flag"] == {"flagStatus": "notFlagged"}
    assert stored["importance"] == "normal"
    assert out["labelIds"] == ["UNREAD", "INBOX", "Clients"]


async def test_archive_and_trash_are_moves_that_keep_the_id(mail, exchange) -> None:
    msg = exchange.deliver(ME, sender=BOB, to=[ME])
    out = await mail.modify(msg["id"], add_labels=[], remove_labels=["INBOX"])
    assert exchange.folder_of(ME, exchange.message(ME, msg["id"])) == "archive"
    assert out["id"] == msg["id"] and "INBOX" not in out["labelIds"]

    out = await mail.modify(msg["id"], add_labels=["TRASH"], remove_labels=[])
    assert exchange.folder_of(ME, exchange.message(ME, msg["id"])) == "deleteditems"
    assert "TRASH" in out["labelIds"]

    out = await mail.modify(msg["id"], add_labels=["INBOX"], remove_labels=["TRASH"])
    assert exchange.folder_of(ME, exchange.message(ME, msg["id"])) == "inbox"
    assert "INBOX" in out["labelIds"]


async def test_modify_accepts_a_comma_separated_string(mail, exchange) -> None:
    msg = exchange.deliver(ME, sender=BOB, to=[ME])
    out = await mail.modify(msg["id"], add_labels="STARRED, Clients", remove_labels="")
    assert out["labelIds"] == ["UNREAD", "INBOX", "STARRED", "Clients"]


@pytest.mark.parametrize(
    ("add", "remove"),
    [(["SENT"], []), (["DRAFT"], []), (["SPAM"], []), (["INBOX"], ["INBOX"])],
)
async def test_unmappable_label_changes_are_refused_before_writing(
    mail, exchange, add, remove
) -> None:
    msg = exchange.deliver(ME, sender=BOB, to=[ME])
    with pytest.raises(Unsupported):
        await mail.modify(msg["id"], add_labels=add, remove_labels=remove)
    assert exchange.writes() == []


def test_labels_for_a_graph_message() -> None:
    folders = {
        "inbox": "F-in",
        "sentitems": "F-sent",
        "deleteditems": "F-del",
        "junkemail": "F-junk",
        "drafts": "F-dr",
        "archive": "F-ar",
    }
    message = {
        "isRead": False,
        "parentFolderId": "F-in",
        "flag": {"flagStatus": "flagged"},
        "importance": "high",
        "categories": ["Clients"],
    }
    assert labels_for(message, folders) == ["UNREAD", "INBOX", "STARRED", "IMPORTANT", "Clients"]
    assert labels_for({"isRead": True, "parentFolderId": "F-sent"}, folders) == ["SENT"]
    assert labels_for({"isRead": True, "isDraft": True, "parentFolderId": "F-dr"}, folders) == [
        "DRAFT"
    ]
    assert labels_for({"isRead": True, "parentFolderId": "F-del"}, folders) == ["TRASH"]
    assert labels_for({"isRead": True, "parentFolderId": "F-junk"}, folders) == ["SPAM"]
    assert labels_for({"isRead": True, "parentFolderId": "F-ar"}, folders) == []


async def test_the_fake_rejects_orderby_with_a_conversation_filter(tenant, exchange) -> None:
    # The production failure get_thread avoids by sorting client-side.
    graph = GraphClient(StaticToken(tenant), transport=tenant.transport())
    try:
        with pytest.raises(WorkspaceError, match="InefficientFilter"):
            await graph.get(
                f"/users/{ME}/messages",
                {"$filter": "conversationId eq 'x'", "$orderby": "receivedDateTime desc"},
            )
    finally:
        await graph.aclose()
