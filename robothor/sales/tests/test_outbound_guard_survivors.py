"""One test per outbound guard that survived deletion on a green suite.

A hostile review deleted each of twenty outbound guards in turn and re-ran the
whole suite. Seven mutants lived. A guard no test can tell the absence of is a
comment: the two that mattered most were the service-workflow identity check on
``sales_process_queue`` (the only thing stopping an ordinary agent driving the
delivery stage) and ``sending_enabled`` in the Instantly tick (whose identical
Gmail twin WAS caught, which is how the asymmetry hid).

Each test here is written to fail if, and only if, its guard is removed. Delete
the line the docstring names and exactly this file should go red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg2.extras import Json

from robothor.operations.store import Conflict, digest
from robothor.sales.providers import ProviderError
from robothor.sales.tests.test_delivery import setup
from robothor.sales.tests.test_guards import approved, draft, prepared


# ── 1. handlers/sales.py — the service-workflow identity gate ──────────────


def _ctx(**overrides):
    base = {
        "agent_id": "workflow:sales-delivery",
        "run_id": "run-1",
        "tenant_id": "tenant-under-test",
        "user_id": "service:workflow:sales-delivery",
        "user_role": "service",
        "is_benchmark": False,
    }
    return SimpleNamespace(**{**base, **overrides})


@pytest.mark.parametrize(
    ("label", "ctx"),
    [
        # An ordinary agent: this is the case the guard exists for.
        ("an ordinary agent", _ctx(agent_id="main", user_id="service:main")),
        # A workflow-shaped agent_id with somebody else's user identity.
        ("a borrowed workflow name", _ctx(user_id="service:main")),
        # The right names under a human role.
        ("a human role", _ctx(user_role="owner")),
        ("no role at all", _ctx(user_role="")),
    ],
)
async def test_only_a_native_service_workflow_can_drive_the_queue(label, ctx):
    """handlers/sales.py — `if name == "sales_process_queue" and (...)`.

    Deleting that condition let any agent holding the tool pump the delivery
    stage. Nothing downstream re-checks the caller's identity.
    """
    from robothor.engine.tools.handlers.sales import handler

    result = await handler("sales_process_queue")({"stage": "delivery"}, ctx)

    assert result == {"error": "Sales queue execution requires a native service workflow identity"}


async def test_the_gate_admits_the_identity_the_workflow_runner_actually_carries():
    """A gate that refused everything would pass the test above and ship broken."""
    from robothor.engine.tools.handlers.sales import handler

    result = await handler("sales_process_queue")({"stage": "delivery"}, _ctx(tenant_id=""))

    # Past the identity gate, stopped by the next check — proof it was admitted.
    assert result == {"error": "Authenticated tenant required"}


# ── 2. sales/delivery.py — sending_enabled in the Instantly tick ───────────


@pytest.mark.asyncio
async def test_the_instantly_tick_does_nothing_while_sending_is_paused(sales):
    """sales/delivery.py — `or not settings.sending_enabled` in `tick`.

    The identical Gmail gate was caught by a test; this one was not, and the
    approved action underneath is fully sendable — `validate_send` is never
    reached because the tick returns before claiming.
    """
    p, provider, worker = setup(sales)
    action = draft(sales, p)
    sales.ops.decide(action, True, "operator:test")
    sales.configure({"sending_enabled": False}, "operator:test")

    assert await worker.tick() is False

    assert provider.calls == []
    result = next(a for a in sales.overview()["actions"] if a["id"] == action)
    assert result["status"] == "approved"

    # And the same action does go out once the operator un-pauses, so the test
    # above is measuring the pause and not some unrelated refusal.
    sales.configure({"sending_enabled": True}, "operator:test")
    assert await worker.tick() is True
    assert provider.calls == ["create", "lead", "activate"]


@pytest.mark.asyncio
async def test_the_instantly_tick_does_nothing_for_another_provider(sales):
    """sales/delivery.py — `settings.email_provider != "instantly"` in `tick`.

    The provider is switched BEFORE the draft, so the approval is current and
    its sender context hash matches: nothing else in the chain objects, and
    this branch is the only thing keeping the Instantly worker's hands off a
    send the operator routed to Gmail.
    """
    p, provider, worker = setup(sales)
    sales.configure({"email_provider": "gmail"}, "operator:test")
    action = draft(sales, p)
    sales.ops.decide(action, True, "operator:test")

    assert await worker.tick() is False

    assert provider.calls == []
    assert next(a for a in sales.overview()["actions"] if a["id"] == action)["status"] == "approved"


# ── 3. operations/store.py — decide() requires an operator actor ───────────


@pytest.mark.parametrize(
    "actor", ["agent:main", "main", "service:workflow:sales-delivery", "", "operator", "Operator:x"]
)
def test_only_an_operator_actor_can_approve_a_send(sales, actor):
    """operations/store.py — `if not actor.startswith("operator:")` in `decide`.

    This is the approval path's only check that a human decided. Without it an
    agent could approve its own draft and the delivery worker would send it.
    """
    p = prepared(sales)
    action = draft(sales, p)

    with pytest.raises(Conflict, match="Human operator decision required"):
        sales.ops.decide(action, True, actor)

    assert next(a for a in sales.overview()["actions"] if a["id"] == action)["status"] == "review"
    assert sales.ops.claim_action(kind="sales.email") is None


def test_an_agent_cannot_reject_either(sales):
    """Rejection is a decision too: the guard is before the approved/rejected split."""
    p = prepared(sales)
    action = draft(sales, p)

    with pytest.raises(Conflict, match="Human operator decision required"):
        sales.ops.decide(action, False, "agent:main")


# ── 4. sales/service.py — the sender allowlist ─────────────────────────────
#
# Guards 4 and 7 sit BEHIND `approved_hash` and `sender_context_hash` in
# `validate_send`, and those two cover the sender, the allowlist membership,
# the postal address, the opt-out link and the whole approved body. So NO
# configuration change and no ordinary edit can reach them — which is exactly
# why deleting either left 644 tests green. Reaching them at all means
# presenting a payload that has already satisfied every earlier check, which
# is the state these two exist to refuse. `_restamp` builds it.


def _restamp(sales, action, payload):
    """Put a payload past the integrity checks so the guard behind them runs."""
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_actions SET payload=%s, payload_hash=%s, approved_hash=%s "
            "WHERE tenant_id=%s AND id=%s RETURNING *",
            (Json(payload), digest(payload), digest(payload), sales.tenant, action["id"]),
        )
        return dict(cur.fetchone())


def _rehash(sales, payload):
    """The sender_context_hash the CURRENT settings would produce."""
    from robothor.sales.setup import sender_context_hash

    return {**payload, "sender_context_hash": sender_context_hash(sales.settings(), payload["sender"])}


def test_a_send_is_refused_when_its_sender_leaves_the_allowlist(sales):
    """sales/service.py — `if payload["sender"] not in settings.senders`.

    Approval binds a sender. The allowlist is a check in its own right, not a
    restatement of the context hash: with the hash agreeing that this sender is
    no longer enabled, the send must still be refused rather than proceeding.
    """
    p = prepared(sales)
    action = approved(sales, p)
    sales.validate_send(action)

    sales.configure({"senders": ["someone-else@example.com"]}, "operator:test")
    action = _restamp(sales, action, _rehash(sales, action["payload"]))

    with pytest.raises(Conflict, match="Sender not enabled"):
        sales.validate_send(action)


def test_an_empty_allowlist_enables_nobody(sales):
    """`not in []` is the same branch, but "" and [] are where guards rot."""
    p = prepared(sales)
    action = approved(sales, p)

    sales.configure({"senders": []}, "operator:test")
    action = _restamp(sales, action, _rehash(sales, action["payload"]))

    with pytest.raises(Conflict, match="Sender not enabled"):
        sales.validate_send(action)


def test_restamping_alone_does_not_make_a_send_valid(sales):
    """_restamp must not be a way to pass validate_send, or 4 and 7 prove nothing."""
    p = prepared(sales)
    action = approved(sales, p)

    sales.validate_send(_restamp(sales, action, action["payload"]))


# ── 5. sales/delivery.py — the mailbox-readiness expiry ────────────────────


def _account(sender="sales@example.com"):
    return {
        "email": sender,
        "status": 1,
        "warmup_status": 1,
        "setup_pending": False,
        "stat_warmup_score": 95,
        "timestamp_warmup_start": "2026-08-01T00:00:00+00:00",
    }


@pytest.mark.parametrize(
    ("label", "approved_until"),
    [
        ("never reviewed", None),
        ("review expired", datetime(2026, 9, 18, 14, tzinfo=UTC)),
        ("expiring exactly now", datetime(2026, 9, 18, 15, tzinfo=UTC)),
        # A naive expiry never reaches _check_mailbox: SalesSettings refuses it
        # first ("Mailbox readiness expiry must include its timezone"), so the
        # `not approved_until.tzinfo` half of the branch is belt and braces.
        # test_a_naive_expiry_is_refused_before_it_is_ever_stored pins that.
    ],
)
def test_a_stale_mailbox_review_stops_the_send(sales, label, approved_until):
    """sales/delivery.py — the first branch of `_check_mailbox`.

    The operator's mailbox readiness review has a deliberate expiry. A healthy
    account is NOT a reviewed account: every field below says the mailbox is
    fine, and the send must still be refused.
    """
    from robothor.sales.delivery import DeliveryWorker
    from robothor.sales.models import SalesSettings

    now = datetime(2026, 9, 18, 15, tzinfo=UTC)
    settings = SalesSettings.model_validate(
        {
            "senders": ["sales@example.com"],
            "mailbox_approved_until": ({"sales@example.com": approved_until} if approved_until else {}),
        }
    )

    with pytest.raises(Conflict, match="mailbox readiness review"):
        DeliveryWorker._check_mailbox(settings, "sales@example.com", _account(), now)


def test_a_current_mailbox_review_passes(sales):
    """Otherwise the test above would pass against a guard that refuses everything."""
    from robothor.sales.delivery import DeliveryWorker
    from robothor.sales.models import SalesSettings

    now = datetime(2026, 9, 18, 15, tzinfo=UTC)
    settings = SalesSettings.model_validate(
        {
            "senders": ["sales@example.com"],
            "mailbox_approved_until": {"sales@example.com": now + timedelta(days=1)},
        }
    )

    DeliveryWorker._check_mailbox(settings, "sales@example.com", _account(), now)


def test_one_senders_review_does_not_cover_another(sales):
    """The expiry is keyed per sender; a shared pass would defeat the review."""
    from robothor.sales.delivery import DeliveryWorker
    from robothor.sales.models import SalesSettings

    now = datetime(2026, 9, 18, 15, tzinfo=UTC)
    settings = SalesSettings.model_validate(
        {
            "senders": ["sales@example.com", "other@example.com"],
            "mailbox_approved_until": {"sales@example.com": now + timedelta(days=1)},
        }
    )

    with pytest.raises(Conflict, match="mailbox readiness review"):
        DeliveryWorker._check_mailbox(settings, "other@example.com", _account("other@example.com"), now)


# ── 6. sales/gmail.py — the approved sender must BE the host mailbox ───────


def _gmail(monkeypatch, mailbox="sales@example.com"):
    from robothor.sales.gmail import Gmail
    from robothor.sales.tests.test_gmail import GmailCLI

    monkeypatch.setenv("ROBOTHOR_SALES_GMAIL_TENANT_ID", "tenant-under-test")
    monkeypatch.setenv("ROBOTHOR_SALES_GMAIL_MAILBOX", mailbox)
    cli = GmailCLI()
    return Gmail("tenant-under-test", runner=cli), cli


def _payload(sender="stranger@example.com"):
    return {
        "recipient": "alice@example.com",
        "sender": sender,
        "subject": "Subject",
        "body": "Body",
        "claim_ids": ["access"],
        "knowledge_version": "v1",
        "evidence_ids": ["service"],
    }


@pytest.mark.asyncio
async def test_gmail_refuses_to_prepare_a_send_from_another_address(monkeypatch):
    """sales/gmail.py — `if draft.sender != self.mailbox` in `prepare`.

    One local OAuth account is bound to one tenant. An approval naming a
    different sender must not be posted through this mailbox — Gmail would
    silently rewrite the From header to the authenticated account, so the
    recipient would receive a message no one approved.
    """
    gmail, cli = _gmail(monkeypatch)

    with pytest.raises(ProviderError, match="Approved sender differs from Gmail host mailbox"):
        await gmail.prepare("action-1", _payload())

    assert cli.calls == []


@pytest.mark.asyncio
async def test_gmail_refuses_to_reconcile_a_send_from_another_address(monkeypatch):
    """sales/gmail.py — `if payload.get("sender") != self.mailbox` in `find_sent`.

    The same check on the read side. Reconciling somebody else's approval
    against this mailbox would attach the wrong evidence to the wrong send.
    """
    gmail, cli = _gmail(monkeypatch)

    with pytest.raises(ProviderError, match="Approved sender differs from Gmail host mailbox"):
        await gmail.find_sent("action-1", _payload())

    assert cli.calls == []


@pytest.mark.asyncio
async def test_gmail_accepts_the_bound_mailbox(monkeypatch):
    """The complement, so the two tests above cannot pass against a blanket refusal."""
    gmail, _ = _gmail(monkeypatch)

    prepared_send = await gmail.prepare("action-1", _payload(sender="sales@example.com"))

    assert prepared_send["mailbox"] == "sales@example.com"


# ── 7. sales/service.py — the CAN-SPAM postal address and opt-out link ─────


@pytest.mark.parametrize(
    ("label", "drop"),
    [
        ("the postal address is missing from the body", "TEST POSTAL ADDRESS"),
        ("the opt-out link is missing from the body", "https://example.com/unsubscribe"),
    ],
)
def test_an_approved_body_without_the_configured_address_and_opt_out_cannot_send(
    sales, label, drop
):
    """sales/service.py — the postal_address / unsubscribe_url branch of validate_send.

    The legal requirement, checked immediately before the external effect. Both
    hashes agree with the body here, so nothing upstream objects: this branch is
    the only thing between a footer-less message and the provider.
    """
    p = prepared(sales)
    action = approved(sales, p)
    payload = {**action["payload"], "body": action["payload"]["body"].replace(drop, "")}
    action = _restamp(sales, action, payload)

    with pytest.raises(Conflict, match="address and opt-out link"):
        sales.validate_send(action)


@pytest.mark.parametrize("cleared", ["postal_address", "unsubscribe_url"])
def test_clearing_the_configuration_invalidates_an_approved_send(sales, cleared):
    """Unconfiguring either one must stop an approval that already carries it."""
    p = prepared(sales)
    action = approved(sales, p)

    sales.configure({cleared: ""}, "operator:test")
    action = _restamp(sales, action, _rehash(sales, action["payload"]))

    with pytest.raises(Conflict, match="address and opt-out link"):
        sales.validate_send(action)


def test_a_naive_expiry_is_refused_before_it_is_ever_stored(sales):
    """The model layer that makes half of _check_mailbox's first branch moot."""
    with pytest.raises(ValueError, match="must include its timezone"):
        sales.configure(
            {"mailbox_approved_until": {"sales@example.com": "2027-01-01T12:00:00"}},
            "operator:test",
        )
