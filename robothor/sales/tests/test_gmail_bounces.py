"""Structured delivery failures are associated with an owned send and recipient."""

import base64
from datetime import UTC, datetime

import pytest

from robothor.sales.tests.test_gmail_delivery import action
from robothor.sales.tests.test_gmail_sync import ThreadProvider, sent


def report(original_id, *, recipient="alice@example.com", status="5.1.1", outcome="failed"):
    return (
        f"From: Mail Delivery Subsystem <mailer-daemon@example.net>\r\nTo: sales@example.com\r\n"
        f"Subject: Delivery status notification\r\nMIME-Version: 1.0\r\nContent-Type: multipart/report; report-type=delivery-status; boundary=notice\r\n\r\n"
        f"--notice\r\nContent-Type: text/plain\r\n\r\nDelivery report.\r\n"
        f"--notice\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; example.net\r\n\r\n"
        f"Final-Recipient: rfc822; {recipient}\r\nAction: {outcome}\r\nStatus: {status}\r\n\r\n"
        f"--notice\r\nContent-Type: text/rfc822-headers\r\n\r\nMessage-ID: {original_id}\r\nFrom: sales@example.com\r\nTo: {recipient}\r\n\r\n--notice--\r\n"
    ).encode()


class BounceProvider:
    def __init__(self, gmail, raw):
        self.gmail, self.mailbox, self.raw = gmail, gmail.mailbox, raw
        self.occurred = str(int(datetime.now(UTC).timestamp() * 1000))
        self.raw_reads = 0
        self.candidate = True
        self.cursor = None
        self.pages = []

    def __getattr__(self, name):
        return getattr(self.gmail, name)

    async def list_messages(self, **params):
        self.pages.append(params)
        return {
            "messages": [{"id": "bounce123", "threadId": "separate123"}],
            "nextPageToken": self.cursor,
        }

    async def get_metadata(self, raw_id):
        return {
            "id": raw_id,
            "threadId": "separate123",
            "internalDate": self.occurred,
            "payload": {
                "headers": [
                    {
                        "name": "Content-Type",
                        "value": "multipart/report; report-type=delivery-status"
                        if self.candidate
                        else "text/plain",
                    }
                ]
            },
        }

    async def get_raw(self, raw_id):
        self.raw_reads += 1
        return {
            "id": raw_id,
            "threadId": "separate123",
            "raw": base64.urlsafe_b64encode(self.raw).decode(),
        }


@pytest.mark.asyncio
async def test_bounce_in_separate_thread_suppresses_and_survives_sent_copy_replay(
    sales, monkeypatch
):
    from robothor.sales.gmail_bounces import GmailBounceWorker
    from robothor.sales.gmail_sync import GmailThreadWorker

    p, _, gmail, original, outbound = await sent(sales, monkeypatch)
    provider = BounceProvider(gmail, report(gmail.message_id(original["id"], original["payload"])))
    sales.configure({"sending_enabled": False}, "operator:test")
    assert await GmailBounceWorker(sales, provider).tick()
    with sales.ops.transaction() as cur:
        assert sales._suppressed("alice@example.com", cur)
        cur.execute(
            "SELECT result FROM operation_jobs WHERE tenant_id=%s AND kind='sales.gmail_bounces'",
            (sales.tenant,),
        )
        assert cur.fetchone()["result"]["final"] is True
    assert action(sales, original["id"])["receipt"]["delivery_status"] == "bounced"
    await GmailThreadWorker(sales, ThreadProvider(gmail, [outbound])).tick()
    assert action(sales, original["id"])["receipt"]["delivery_status"] == "bounced"
    assert provider.raw_reads == 1


@pytest.mark.asyncio
async def test_delayed_report_holds_conversation_without_permanent_suppression(sales, monkeypatch):
    from robothor.sales.gmail_bounces import GmailBounceWorker

    p, _, gmail, original, _ = await sent(sales, monkeypatch)
    provider = BounceProvider(
        gmail,
        report(
            gmail.message_id(original["id"], original["payload"]), status="4.2.0", outcome="delayed"
        ),
    )
    assert await GmailBounceWorker(sales, provider).tick()
    with sales.ops.transaction() as cur:
        assert not sales._suppressed("alice@example.com", cur)
    assert sales.get(p["id"])["owner"] == "human_review"
    assert action(sales, original["id"])["receipt"]["delivery_status"] == "delivery_delayed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["recipient", "original_id"])
async def test_unrelated_reports_cannot_change_owned_action(sales, monkeypatch, mismatch):
    from robothor.sales.gmail_bounces import GmailBounceWorker

    _, _, gmail, original, _ = await sent(sales, monkeypatch)
    rfc = gmail.message_id(original["id"], original["payload"])
    raw = (
        report(rfc, recipient="other@example.com")
        if mismatch == "recipient"
        else report("<other@example.com>")
    )
    assert await GmailBounceWorker(sales, BounceProvider(gmail, raw)).tick()
    assert action(sales, original["id"])["receipt"]["delivery_status"] == "provider_accepted"
    with sales.ops.transaction() as cur:
        assert not sales._suppressed("alice@example.com", cur)


@pytest.mark.asyncio
async def test_normal_mail_body_is_not_fetched(sales, monkeypatch):
    from robothor.sales.gmail_bounces import GmailBounceWorker

    _, _, gmail, _, _ = await sent(sales, monkeypatch)
    provider = BounceProvider(gmail, b"must not be read")
    provider.candidate = False
    assert await GmailBounceWorker(sales, provider).tick()
    assert provider.raw_reads == 0


@pytest.mark.asyncio
async def test_repeated_cursor_is_held_and_does_not_publish_complete_coverage(sales, monkeypatch):
    from robothor.sales.gmail_bounces import GmailBounceWorker

    _, _, gmail, original, _ = await sent(sales, monkeypatch)
    provider = BounceProvider(gmail, report(gmail.message_id(original["id"], original["payload"])))
    provider.cursor = "repeat"
    worker = GmailBounceWorker(sales, provider)
    assert await worker.tick()
    assert await worker.tick()
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT status,result FROM operation_jobs WHERE tenant_id=%s AND kind='sales.gmail_bounces' ORDER BY created_at",
            (sales.tenant,),
        )
        rows = cur.fetchall()
        assert rows[0]["result"]["final"] is False
        assert rows[1]["status"] == "pending" and rows[1]["result"] is None


@pytest.mark.asyncio
async def test_malformed_report_stays_visible_without_claiming_success(sales, monkeypatch):
    from robothor.sales.gmail_bounces import GmailBounceWorker

    _, _, gmail, original, _ = await sent(sales, monkeypatch)
    assert await GmailBounceWorker(sales, BounceProvider(gmail, b"not a report")).tick()
    reads = sales.provider_reads(kind="sales.gmail_bounces")
    assert len(reads["items"]) == 1
    assert action(sales, original["id"])["receipt"]["delivery_status"] == "provider_accepted"


@pytest.mark.asyncio
async def test_later_delay_cannot_erase_permanent_failure_in_same_page(sales, monkeypatch):
    from robothor.sales.gmail_bounces import GmailBounceWorker

    _, _, gmail, original, _ = await sent(sales, monkeypatch)
    worker = GmailBounceWorker(sales, BounceProvider(gmail, b""))
    worker.plan()
    job = sales.ops.claim("sales.gmail_bounces")
    base = {
        "original_id": gmail.message_id(original["id"], original["payload"]),
        "recipient": "alice@example.com",
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    worker.commit(
        job,
        [
            {
                **base,
                "provider_id": gmail.provider_id("failed1"),
                "action": "failed",
                "status": "5.1.1",
            },
            {
                **base,
                "provider_id": gmail.provider_id("delayed2"),
                "action": "delayed",
                "status": "4.2.0",
            },
        ],
        None,
    )
    assert action(sales, original["id"])["receipt"]["delivery_status"] == "bounced"


@pytest.mark.asyncio
async def test_human_sent_copy_recovery_preserves_bounce_evidence(sales, monkeypatch):
    from robothor.sales.gmail_bounces import GmailBounceWorker
    from robothor.sales.gmail_recovery import GmailRecovery
    from robothor.sales.tests.test_gmail_recovery import uncertain

    _, cli, delivery, key = await uncertain(sales, monkeypatch)
    draft = action(sales, key)["payload"]
    provider = BounceProvider(delivery.provider, report(delivery.provider.message_id(key, draft)))
    await GmailBounceWorker(sales, provider).tick()
    recovery = GmailRecovery(sales, delivery.provider)
    proof = await recovery.inspect(key, actor="operator:test")
    await recovery.reconcile(
        key,
        expected_hash=proof["content_hash"],
        reason="Reviewed Gmail sent copy with its failed delivery",
        actor="operator:test",
    )
    assert action(sales, key)["receipt"]["delivery_status"] == "bounced"
    assert action(sales, key)["receipt"]["delivery_failure"]["status"] == "5.1.1"


@pytest.mark.parametrize("global_report", [False, True])
def test_machine_delivery_report_required_and_international_variant_supported(global_report):
    from robothor.sales.gmail_bounces import parse_report
    from robothor.sales.providers import ProviderError

    raw = report("<original@example.com>")
    if global_report:
        raw = raw.replace(b"message/delivery-status", b"message/global-delivery-status").replace(
            b"report-type=delivery-status", b"report-type=global-delivery-status"
        )
    assert parse_report(raw, "sales@example.com")[0]["action"] == "failed"
    missing = raw.replace(
        b"message/global-delivery-status" if global_report else b"message/delivery-status",
        b"text/plain",
    )
    with pytest.raises(ProviderError):
        parse_report(missing, "sales@example.com")


@pytest.mark.asyncio
async def test_report_arriving_before_send_ack_is_committed_cannot_be_erased(sales, monkeypatch):
    from robothor.sales.gmail_bounces import GmailBounceWorker
    from robothor.sales.tests.test_gmail_delivery import gmail_setup
    from robothor.sales.tests.test_guards import draft

    p, _, delivery = gmail_setup(sales, monkeypatch)
    key = draft(sales, p)
    sales.ops.decide(key, True, "operator:test")
    submit = delivery.provider.submit
    original = action(sales, key)

    async def report_during_send(prepared):
        receipt = await submit(prepared)
        provider = BounceProvider(
            delivery.provider, report(delivery.provider.message_id(key, original["payload"]))
        )
        await GmailBounceWorker(sales, provider).tick()
        return receipt

    delivery.provider.submit = report_during_send
    assert await delivery.tick()
    assert action(sales, key)["status"] == "completed"
    assert action(sales, key)["receipt"]["delivery_status"] == "bounced"
