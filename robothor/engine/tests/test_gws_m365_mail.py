"""The REAL gws mail handlers, with workspace_provider=microsoft365, against a fake tenant.

Every guard lives in the handler, in front of the provider; these tests prove
it stays there when the provider is Microsoft Graph: a refused send makes ZERO
Graph writes, the duplicate-reply guard never reaches ``createReplyAll``, and a
benchmark run makes no Graph request at all.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import gws
from robothor.workspace.microsoft.graph import GraphClient
from robothor.workspace.tests.fake_graph import FakeGraphTenant
from robothor.workspace.tests.fake_graph_mail import FakeExchangeMail, install_mail

ME = "assistant@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"
FLAGGED = {"mallory@example.com"}
TENANT = "tenant-a"


class StaticToken:
    def __init__(self, tenant: FakeGraphTenant) -> None:
        tenant.issued_tokens.append("tok")

    async def token(self) -> str:
        return "tok"


def _dnc_lookup(emails, tenant_id="default"):
    return {e.strip().lower() for e in emails if e and e.strip().lower() in FLAGGED}


@pytest.fixture
def tenant() -> FakeGraphTenant:
    return FakeGraphTenant()


@pytest.fixture
def exchange(tenant: FakeGraphTenant) -> FakeExchangeMail:
    return install_mail(tenant)


@pytest.fixture
async def m365(monkeypatch: pytest.MonkeyPatch, tenant: FakeGraphTenant, exchange):
    """workspace_provider=microsoft365, the vault replaced by the fake tenant."""
    from robothor import workspace
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE_PROVIDER", "microsoft365")
    monkeypatch.setenv("ROBOTHOR_M365_ASSISTANT_MAILBOX", ME)
    reset_settings()
    workspace.reset_workspace_cache()
    built: list[str] = []
    clients: list[GraphClient] = []

    async def from_vault(tenant_id: str, **_: Any) -> GraphClient:
        built.append(tenant_id)
        client = GraphClient(StaticToken(tenant), transport=tenant.transport())
        clients.append(client)
        return client

    monkeypatch.setattr("robothor.workspace.microsoft.graph_client_from_vault", from_vault)
    monkeypatch.setattr(gws, "ROBOTHOR_EMAIL", ME)

    def no_cli(*a: Any, **k: Any) -> Any:
        raise AssertionError("a mail call reached the gws CLI under microsoft365")

    monkeypatch.setattr(gws, "_run_gws", no_cli)
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(gws, "_record_sent_email", lambda **k: recorded.append(k))
    with (
        patch("robothor.crm.dal.do_not_contact_emails", side_effect=_dnc_lookup),
        patch("robothor.engine.tracking.log_guardrail_event") as audit,
    ):
        yield {"built": built, "recorded": recorded, "audit": audit}
    for client in clients:
        await client.aclose()
    workspace.reset_workspace_cache()
    reset_settings()


async def _call(tool: str, args: dict[str, Any], *, benchmark: bool = False) -> dict[str, Any]:
    ctx = ToolContext(agent_id="main", run_id="run-1", tenant_id=TENANT, is_benchmark=benchmark)
    return await gws.HANDLERS[tool](args, ctx)


# ── reads ─────────────────────────────────────────────────────────────


async def test_search_returns_google_shaped_envelopes(m365, exchange) -> None:
    exchange.deliver(ME, sender=BOB, to=[ME], subject="Old", is_read=True)
    newest = exchange.deliver(ME, sender=BOB, to=[ME], subject="Contract", body="Signed copy")
    out = await _call("gws_gmail_search", {"query": "from:bob@example.com is:unread"})

    assert out["count"] == 1
    (message,) = out["messages"]
    assert set(message) == {"id", "thread_id", "date", "from", "to", "subject", "snippet", "labels"}
    assert message["id"] == newest["id"]
    assert message["thread_id"] == newest["conversationId"]
    assert message["subject"] == "Contract"
    assert message["labels"] == ["UNREAD", "INBOX"]
    assert m365["built"] == [TENANT]  # the tool's tenant reaches the vault lookup


async def test_untranslatable_search_is_refused_with_no_request(m365, tenant) -> None:
    out = await _call("gws_gmail_search", {"query": "filename:pdf from:bob@example.com"})
    assert out["hint"] == "unsupported"
    assert "Supported:" in out["error"]
    assert tenant.requests == []


async def test_get_message_and_thread(m365, exchange) -> None:
    first = exchange.deliver(ME, sender=BOB, to=[ME], subject="Plan", body="Step one")
    conv = first["conversationId"]
    exchange.deliver(
        ME, sender=CAROL, to=[ME], subject="RE: Plan", body="Step two", conversation_id=conv
    )

    one = await _call("gws_gmail_get", {"message_id": first["id"]})
    assert one["body_text"] == "Step one" and one["thread_id"] == conv
    assert one["message_id"] == first["internetMessageId"]

    thread = await _call("gws_gmail_get", {"thread_id": conv})
    assert [m["body_text"] for m in thread["messages"]] == ["Step one", "Step two"]


# ── send / reply ──────────────────────────────────────────────────────


async def test_send_returns_the_sent_messages_id(m365, exchange) -> None:
    out = await _call("gws_gmail_send", {"to": BOB, "subject": "Hello", "body": "Hi Bob"})

    sent = exchange.message(ME, out["id"])
    assert exchange.folder_of(ME, sent) == "sentitems"
    assert out["threadId"] == sent["conversationId"]
    assert [r["emailAddress"]["address"] for r in sent["toRecipients"]] == [BOB]
    assert m365["recorded"][0]["result"]["id"] == out["id"]


async def test_reply_threads_into_the_conversation(m365, exchange) -> None:
    original = exchange.deliver(ME, sender=BOB, to=[ME], cc=[CAROL], subject="Budget")
    conv = original["conversationId"]

    out = await _call("gws_gmail_reply", {"thread_id": conv, "body": "Approved."})

    sent = exchange.message(ME, out["id"])
    assert sent["conversationId"] == conv == out["threadId"]
    # The handler's reply-all: everyone on the thread but the assistant.
    assert sorted(r["emailAddress"]["address"] for r in sent["toRecipients"]) == [BOB, CAROL]
    assert sent["ccRecipients"] == []
    assert sent["body"]["content"] == "Approved."


async def test_do_not_contact_reply_makes_zero_graph_writes(m365, exchange) -> None:
    original = exchange.deliver(ME, sender=BOB, to=[ME], cc=["mallory@example.com"])
    out = await _call("gws_gmail_reply", {"thread_id": original["conversationId"], "body": "x"})
    assert out["guard"] == "do_not_contact"
    assert exchange.writes() == []


async def test_do_not_contact_send_makes_zero_graph_requests(m365, tenant) -> None:
    out = await _call(
        "gws_gmail_send", {"to": "mallory@example.com", "subject": "Offer", "body": "hi"}
    )
    assert out["guard"] == "do_not_contact"
    assert tenant.requests == []


async def test_duplicate_reply_guard_skips_before_create_reply_all(m365, exchange) -> None:
    original = exchange.deliver(ME, sender=BOB, to=[ME], subject="Q")
    conv = original["conversationId"]
    exchange.deliver(
        ME,
        sender=ME,
        to=[BOB],
        subject="RE: Q",
        conversation_id=conv,
        folder="sentitems",
        is_read=True,
    )

    out = await _call("gws_gmail_reply", {"thread_id": conv, "body": "again"})

    assert out["status"] == "skipped"
    assert exchange.writes() == []


async def test_send_with_thread_id_also_honours_the_duplicate_guard(m365, exchange) -> None:
    original = exchange.deliver(ME, sender=BOB, to=[ME], subject="Q")
    conv = original["conversationId"]
    exchange.deliver(
        ME,
        sender=ME,
        to=[BOB],
        subject="RE: Q",
        conversation_id=conv,
        folder="sentitems",
        is_read=True,
    )
    out = await _call(
        "gws_gmail_send", {"to": BOB, "subject": "Re: Q", "body": "b", "thread_id": conv}
    )
    assert out["status"] == "skipped"
    assert exchange.writes() == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("gws_gmail_search", {"query": "is:unread"}),
        ("gws_gmail_get", {"message_id": "AAk-x"}),
        ("gws_gmail_send", {"to": BOB, "subject": "s", "body": "b"}),
        ("gws_gmail_reply", {"thread_id": "AAQk-x", "body": "b"}),
        ("gws_gmail_modify", {"message_id": "AAk-x", "add_labels": ["STARRED"]}),
    ],
)
async def test_benchmark_refusal_makes_zero_graph_requests(m365, tenant, tool, args) -> None:
    out = await _call(tool, args, benchmark=True)
    assert out["guard"] == "is_benchmark"
    assert tenant.requests == []


# ── modify ────────────────────────────────────────────────────────────


async def test_modify_maps_labels(m365, exchange) -> None:
    msg = exchange.deliver(ME, sender=BOB, to=[ME])
    out = await _call(
        "gws_gmail_modify",
        {"message_id": msg["id"], "add_labels": ["STARRED"], "remove_labels": ["UNREAD", "INBOX"]},
    )
    assert out == {"id": msg["id"], "threadId": msg["conversationId"], "labelIds": ["STARRED"]}
    assert exchange.folder_of(ME, exchange.message(ME, msg["id"])) == "archive"


async def test_unmappable_label_is_refused_as_a_tool_error(m365, exchange) -> None:
    msg = exchange.deliver(ME, sender=BOB, to=[ME])
    out = await _call("gws_gmail_modify", {"message_id": msg["id"], "add_labels": ["SPAM"]})
    assert out["hint"] == "unsupported"
    assert exchange.writes() == []


# ── what stays dark ───────────────────────────────────────────────────


async def test_calendar_tools_stay_refused_under_microsoft365(m365, tenant) -> None:
    out = await _call("gws_calendar_list", {"time_min": "2026-10-01T00:00:00Z"})
    assert out["hint"] == "unsupported"
    assert "calendar" in out["error"]
    assert tenant.requests == []


async def test_mail_is_refused_without_an_assistant_mailbox(m365, monkeypatch, tenant) -> None:
    from robothor import workspace
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_M365_ASSISTANT_MAILBOX", "")
    reset_settings()
    workspace.reset_workspace_cache()
    out = await _call("gws_gmail_search", {"query": "is:unread"})
    assert out["hint"] == "unsupported"
    assert "m365_assistant_mailbox" in out["error"]
    assert tenant.requests == []
