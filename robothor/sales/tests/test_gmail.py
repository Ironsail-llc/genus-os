"""Gmail uses the existing connection without relaxing sales identity or retry rules."""

import base64
import json
from email import policy
from email.parser import BytesParser

import pytest

from robothor.sales.providers import ProviderError, UnknownEffect
from robothor.settings import reset_settings


@pytest.fixture
def mailbox(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SALES_GMAIL_TENANT_ID", "test-tenant")
    monkeypatch.setenv("ROBOTHOR_SALES_GMAIL_MAILBOX", "sales@example.com")
    # The Gmail host binding is a declared setting now, and settings are
    # resolved once per process; a test that reconfigures the host has to
    # say so. `reset_settings` exists for exactly this.
    reset_settings()


class GmailCLI:
    def __init__(self):
        self.calls = []
        self.profile = "sales@example.com"
        self.response = {"id": "abc123", "threadId": "def456"}
        self.messages = {}
        self.matches = []
        self.next_page = None

    def __call__(self, args, timeout=30):
        self.calls.append(args)
        params = json.loads(args[args.index("--params") + 1])
        assert params["userId"] == "sales@example.com"
        if args[:3] == ["gmail", "users", "getProfile"]:
            return {"emailAddress": self.profile, "historyId": "123"}
        if args[:4] == ["gmail", "users", "messages", "send"]:
            return self.response
        if args[:4] == ["gmail", "users", "messages", "get"]:
            return self.messages[params["id"]]
        if args[:4] == ["gmail", "users", "messages", "list"]:
            return {"messages": [{"id": x} for x in self.matches], "nextPageToken": self.next_page}
        raise AssertionError("Unexpected command")


def payload(**changes):
    return {
        "sender": "sales@example.com",
        "recipient": "alice@example.com",
        "subject": "Pharmacy workflows",
        "body": "Hello Alice.\nApproved postal address\nhttps://example.com/unsubscribe",
        "purpose": "initial",
        "reply_to_uuid": None,
        "claim_ids": ["access"],
        "evidence_ids": ["service"],
        "knowledge_version": "v1",
        **changes,
    }


def message(
    *,
    message_id="<inbound@example.com>",
    sender="alice@example.com",
    recipient="sales@example.com",
    body="Please explain.",
    subject="Pharmacy workflows",
    labels=None,
):
    return {
        "id": "abc123",
        "threadId": "def456",
        "labelIds": labels or ["INBOX"],
        "internalDate": "1789815600000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": recipient},
                {"name": "Subject", "value": subject},
                {"name": "Message-ID", "value": message_id},
            ],
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
        },
    }


def provider(cli):
    from robothor.sales.gmail import Gmail

    return Gmail("test-tenant", runner=cli)


def test_host_connection_requires_explicit_tenant_binding(mailbox):
    from robothor.sales.gmail import Gmail

    with pytest.raises(ProviderError, match="binding"):
        Gmail("different-tenant", runner=GmailCLI())


@pytest.mark.asyncio
async def test_only_approved_participants_and_content_enter_mime(mailbox):
    cli = GmailCLI()
    gmail = provider(cli)
    prepared = await gmail.prepare("action-1", payload())
    parsed = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(prepared["raw"])
    )
    assert parsed["From"] == "sales@example.com"
    assert parsed["To"] == "alice@example.com"
    assert parsed["Cc"] is None and parsed["Bcc"] is None
    assert parsed["Subject"] == payload()["subject"]
    assert parsed.get_content().replace("\r\n", "\n") == payload()["body"] + "\n"
    assert parsed["Message-ID"] == gmail.message_id("action-1", payload())
    result = await gmail.send(prepared)
    assert result["delivery_status"] == "provider_accepted"
    assert result["id"] == gmail.provider_id("abc123")
    assert result["thread_id"] == gmail.provider_id("def456")
    assert "delivery_confirmed" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"sender": "other@example.com"},
        {"subject": "Hello\r\nBcc: hidden@example.com"},
        {"recipient": "alice@example.com,hidden@example.com"},
    ],
)
async def test_changed_sender_and_header_injection_fail_before_send(mailbox, changes):
    cli = GmailCLI()
    with pytest.raises((ProviderError, ValueError)):
        await provider(cli).prepare("action-1", payload(**changes))
    assert not any(c[:4] == ["gmail", "users", "messages", "send"] for c in cli.calls)


@pytest.mark.asyncio
async def test_account_switch_is_detected_again_at_send(mailbox):
    cli = GmailCLI()
    gmail = provider(cli)
    prepared = await gmail.prepare("action-1", payload())
    cli.profile = "different@example.com"
    with pytest.raises(ProviderError, match="identity"):
        await gmail.send(prepared)
    assert not any(c[:4] == ["gmail", "users", "messages", "send"] for c in cli.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response", [{"error": "token-secret recipient@example.com"}, {}, {"id": "abc123"}]
)
async def test_ambiguous_send_is_redacted_and_not_retried(mailbox, response):
    cli = GmailCLI()
    cli.response = response
    gmail = provider(cli)
    prepared = await gmail.prepare("action-1", payload())
    with pytest.raises(UnknownEffect) as caught:
        await gmail.send(prepared)
    assert "token-secret" not in str(caught.value)
    assert sum(c[:4] == ["gmail", "users", "messages", "send"] for c in cli.calls) == 1


@pytest.mark.asyncio
async def test_reply_requires_owned_participants_and_real_thread_headers(mailbox):
    cli = GmailCLI()
    cli.messages["abc123"] = message()
    gmail = provider(cli)
    p = payload(purpose="reply", reply_to_uuid=gmail.provider_id("abc123"))
    prepared = await gmail.prepare("action-2", p)
    parsed = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(prepared["raw"])
    )
    assert prepared["threadId"] == "def456"
    assert parsed["In-Reply-To"] == "<inbound@example.com>"
    assert parsed["References"] == "<inbound@example.com>"
    cli.messages["abc123"] = message(sender="stranger@example.com")
    with pytest.raises(ProviderError, match="participants"):
        await gmail.prepare("action-2", p)


@pytest.mark.asyncio
async def test_provider_ids_are_account_scoped_and_subject_is_not_silently_rewritten(mailbox):
    cli = GmailCLI()
    cli.messages["abc123"] = message(subject="Another topic")
    gmail = provider(cli)
    with pytest.raises(ProviderError, match="subject"):
        await gmail.prepare(
            "action-2", payload(purpose="reply", reply_to_uuid=gmail.provider_id("abc123"))
        )
    with pytest.raises(ProviderError, match="account"):
        await gmail.prepare(
            "action-2", payload(purpose="reply", reply_to_uuid="gmail:wrong:abc123")
        )


@pytest.mark.asyncio
async def test_unknown_send_recovery_requires_exact_sent_copy_not_search_hit_alone(mailbox):
    cli = GmailCLI()
    gmail = provider(cli)
    cli.matches = ["abc123"]
    cli.messages["abc123"] = message(
        message_id=gmail.message_id("action-1", payload()),
        sender="sales@example.com",
        recipient="alice@example.com",
        body=payload()["body"] + "\n",
        labels=["SENT"],
    )
    result = await gmail.find_sent("action-1", payload())
    assert result["delivery_status"] == "sent_copy_verified"
    assert result["id"] == gmail.provider_id("abc123")
    cli.messages["abc123"]["payload"]["body"]["data"] = base64.urlsafe_b64encode(
        b"different"
    ).decode()
    with pytest.raises(ProviderError, match="content"):
        await gmail.find_sent("action-1", payload())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "matches,next_page", [([], None), (["abc123", "def456"], None), (["abc123"], "more")]
)
async def test_missing_duplicate_or_incomplete_search_is_never_permission_to_resend(
    mailbox, matches, next_page
):
    cli = GmailCLI()
    cli.matches, cli.next_page = matches, next_page
    with pytest.raises(ProviderError, match="unique"):
        await provider(cli).find_sent("action-1", payload())
    assert not any(c[:4] == ["gmail", "users", "messages", "send"] for c in cli.calls)


@pytest.mark.asyncio
async def test_reply_acknowledged_in_wrong_thread_is_held_for_review(mailbox):
    cli = GmailCLI()
    cli.messages["abc123"] = message()
    gmail = provider(cli)
    prepared = await gmail.prepare(
        "action-2", payload(purpose="reply", reply_to_uuid=gmail.provider_id("abc123"))
    )
    cli.response = {"id": "fff123", "threadId": "wrong_thread"}
    with pytest.raises(UnknownEffect, match="thread"):
        await gmail.send(prepared)
    assert sum(c[:4] == ["gmail", "users", "messages", "send"] for c in cli.calls) == 1


@pytest.mark.asyncio
async def test_sent_copy_can_prove_unicode_subject_and_body(mailbox):
    from email.header import Header

    cli = GmailCLI()
    gmail = provider(cli)
    p = payload(subject="Café care", body="Hello — approved text.\n")
    cli.matches = ["abc123"]
    cli.messages["abc123"] = message(
        message_id=gmail.message_id("action-unicode", p),
        sender=p["sender"],
        recipient=p["recipient"],
        subject=Header(p["subject"], "utf-8").encode(),
        body=p["body"],
        labels=["SENT"],
    )
    result = await gmail.find_sent("action-unicode", p)
    assert result["delivery_status"] == "sent_copy_verified"
