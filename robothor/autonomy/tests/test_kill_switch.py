"""The owner's switch governs completion, not only submission.

Seven hostile reviews found the same defect in four places: every path that can
advance, complete or capture evidence for an operation reached the merchant's
page without ever reading ``autonomy_settings.enabled`` or the grant. These are
those reviewers' probes, kept as the regression suite.

The rule under test, stated once: no code path may advance, complete or capture
evidence for an operation without consulting, in this order, the runtime
settings (``enabled``, and ``payment_processing`` where money is involved) and
the grant (existence, version, revocation, expiry). "Read-only recovery" is not
an exemption, because these paths write durable completion, payment facts and
receipts.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.broker import BrowserBroker, ExecutionPlan
from robothor.autonomy.handoff_recovery import HandoffChecks
from robothor.autonomy.handoffs import HandoffStore
from robothor.autonomy.models import Delegation, RuntimeSettings, WebOperation
from robothor.autonomy.tests.test_handoffs import STATUS_URL, SUBMISSION_URL, pending, request
from robothor.autonomy.tests.test_store import policy, proposal

DISABLED = RuntimeSettings(enabled=False)
ENABLED = RuntimeSettings(
    enabled=True, payment_processing=True, payment_assessment_reference="synthetic-test"
)


def account_policy():
    return Delegation(
        agent_ids={"main"},
        origins={"https://account.example"},
        actions={"account"},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def account_proposal(key="kill-switch-account"):
    return WebOperation(
        origin="https://account.example",
        action="account",
        purpose="Requested account",
        idempotency_key=key,
    )


# --------------------------------------------------------------------------
# F1 -- retained reconciliation completes with the switch off
# --------------------------------------------------------------------------


def oldest_work(store, identity):
    """Put this owner at the front of the bounded scan.

    ``enabled_scopes`` takes the 32 owners with the oldest waiting work, and
    the suite shares one database, so a freshly created tenant is otherwise
    behind every handoff every other test left behind.
    """
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_handoffs SET updated_at=now()-interval '30 days' "
            "WHERE tenant_id=%s AND owner_id=%s",
            (identity.tenant_id, identity.owner_id),
        )


def test_reconcile_authority_refuses_revoked_expired_and_disabled(store, identity):
    """The single chokepoint every reconciliation path must cross."""
    op = pending(store, identity)
    assert store.check_reconcile_authority(identity, op["id"], "main")

    with pytest.raises(PermissionError, match="agent_not_allowed"):
        store.check_reconcile_authority(identity, op["id"], "other-agent")

    store.configure(identity, RuntimeSettings(enabled=True))
    with pytest.raises(PermissionError, match="payment_processing_not_enabled"):
        store.check_reconcile_authority(identity, op["id"], "main")

    store.configure(identity, DISABLED)
    with pytest.raises(PermissionError, match="autonomous_execution_not_enabled"):
        store.check_reconcile_authority(identity, op["id"], "main")

    store.configure(identity, ENABLED)
    grant_id = store.operation(identity, op["id"])["grant_id"]
    store.revoke_grant(identity, grant_id)
    with pytest.raises(PermissionError, match="grant_revoked"):
        store.check_reconcile_authority(identity, op["id"], "main")


def test_reconcile_authority_refuses_an_expired_grant(store, identity):
    op = pending(store, identity)
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_grants SET policy=jsonb_set(policy,'{expires_at}',to_jsonb(%s::text)) "
            "WHERE id=%s",
            (
                (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                store.operation(identity, op["id"])["grant_id"],
            ),
        )
    with pytest.raises(PermissionError, match="grant_expired"):
        store.check_reconcile_authority(identity, op["id"], "main")


@pytest.mark.e2e
@pytest.mark.timeout(60)
async def test_revoked_grant_and_disabled_switch_refuse_retained_reconciliation(
    store, identity, monkeypatch
):
    """The $25 probe: a revoked grant plus enabled=False drove a purchase to
    completed, wrote a payment fact and captured a receipt."""
    from robothor.autonomy.workflows import outcome
    from robothor.autonomy.workflows.manager import WorkflowManager

    monkeypatch.setattr(outcome, "OUTCOME_TIMEOUT_SECONDS", 0.1)
    grant = store.create_grant(identity, policy())
    op = store.reserve(identity, grant["id"], "main", proposal(key="kill-switch-retained"))

    async def merchant(route):
        if route.request.method == "POST":
            await route.fulfill(body="ok")
        else:
            await route.fulfill(
                content_type="text/html",
                body="""<p id="amount">$6.00</p>
                <button id="submit" onclick="fetch('/pay',{method:'POST'}).then(()=>{
                setTimeout(()=>{document.querySelector('#confirmation').textContent=
                'Your order has been confirmed.';},1500);
                })">Buy</button><p id="confirmation"></p>""",
            )

    async with async_playwright() as pw:

        async def factory():
            return await pw.chromium.launch(headless=True)

        manager = WorkflowManager(store, factory, request_router=merchant)
        try:
            opened = await manager.open(identity, "main", op["id"], "https://shop.example/checkout")
            wid = opened["workflow_id"]
            plan = ExecutionPlan(
                url="https://shop.example/checkout",
                submit_selector="#submit",
                amount_selector="#amount",
            )
            result = await manager.execute(identity, "main", wid, str(uuid4()), 0, plan)
            assert result["state"] == "reconciling"
            await manager._live[wid].page.wait_for_timeout(2000)
            observed = await manager.inspect(identity, "main", wid)
            confirmation = next(
                item for item in observed["confirmations"] if "order has been" in item["text"]
            )

            store.revoke_grant(identity, grant["id"])
            store.configure(identity, DISABLED)

            with pytest.raises(PermissionError):
                await manager.inspect(identity, "main", wid)
            with pytest.raises(PermissionError):
                await manager.reconcile(
                    identity,
                    "main",
                    wid,
                    str(uuid4()),
                    0,
                    confirmation["selector"],
                    confirmation["text"],
                )
            assert store.operation(identity, op["id"])["state"] == "reconciling"
            from robothor.autonomy.payment_journal import PaymentJournal

            assert (
                PaymentJournal(store).read(identity, op["id"])["position"]["state"] != "submitted"
            )
        finally:
            await manager.shutdown()


@pytest.mark.e2e
@pytest.mark.timeout(60)
async def test_retained_reconciliation_refuses_text_that_is_not_affirmative(
    store, identity, monkeypatch
):
    """ "Refund issued: your order was CANCELLED and $0.00 was charged." is not
    a confirmation, however precisely the agent quotes it back."""
    from robothor.autonomy.workflows import outcome
    from robothor.autonomy.workflows.manager import WorkflowManager

    monkeypatch.setattr(outcome, "OUTCOME_TIMEOUT_SECONDS", 0.1)
    grant = store.create_grant(identity, policy())
    op = store.reserve(identity, grant["id"], "main", proposal(key="kill-switch-negative"))

    async def merchant(route):
        if route.request.method == "POST":
            await route.fulfill(body="ok")
        else:
            await route.fulfill(
                content_type="text/html",
                body="""<p id="amount">$6.00</p>
                <button id="submit" onclick="fetch('/pay',{method:'POST'}).then(()=>{
                setTimeout(()=>{document.querySelector('#confirmation').textContent=
                'Refund issued: your order was CANCELLED and $0.00 was charged.';},1500);
                })">Buy</button><p id="confirmation"></p>""",
            )

    async with async_playwright() as pw:

        async def factory():
            return await pw.chromium.launch(headless=True)

        manager = WorkflowManager(store, factory, request_router=merchant)
        try:
            opened = await manager.open(identity, "main", op["id"], "https://shop.example/checkout")
            wid = opened["workflow_id"]
            await manager.execute(
                identity,
                "main",
                wid,
                str(uuid4()),
                0,
                ExecutionPlan(
                    url="https://shop.example/checkout",
                    submit_selector="#submit",
                    amount_selector="#amount",
                ),
            )
            await manager._live[wid].page.wait_for_timeout(2000)
            observed = await manager.inspect(identity, "main", wid)
            candidate = next(item for item in observed["confirmations"] if "Refund" in item["text"])
            result = await manager.reconcile(
                identity, "main", wid, str(uuid4()), 0, candidate["selector"], candidate["text"]
            )
            assert result["state"] == "reconciling"
            assert result["reason"] == "confirmation_not_affirmative"
            assert store.operation(identity, op["id"])["state"] == "reconciling"
        finally:
            await manager.shutdown()


@pytest.mark.e2e
@pytest.mark.timeout(60)
async def test_a_disabled_switch_discards_the_retained_browser_context(
    store, identity, monkeypatch
):
    """A retained context outlives the switch by up to an hour without this."""
    from robothor.autonomy.workflows import outcome
    from robothor.autonomy.workflows.manager import WorkflowManager

    monkeypatch.setattr(outcome, "OUTCOME_TIMEOUT_SECONDS", 0.1)
    grant = store.create_grant(identity, account_policy())
    op = store.reserve(identity, grant["id"], "main", account_proposal("kill-switch-discard"))

    async def merchant(route):
        await route.fulfill(content_type="text/html", body="<p id='x'>Register</p>")

    async with async_playwright() as pw:

        async def factory():
            return await pw.chromium.launch(headless=True)

        manager = WorkflowManager(store, factory, request_router=merchant)
        try:
            await manager.open(identity, "main", op["id"], "https://account.example/register")
            assert manager.active_count == 1
            store.configure(identity, DISABLED)
            await manager.expire_unauthorized()
            assert manager.active_count == 0
        finally:
            await manager.shutdown()


# --------------------------------------------------------------------------
# F2 -- handoffs reach completed without ever submitting, and wedge the budget
# --------------------------------------------------------------------------


def test_a_handoff_cannot_be_created_before_the_operation_has_submitted(store, identity):
    """prepare -> handoff -> reconcile reached completed with no begin_submit,
    no bind_plan, no preflight, no terms audit and no price verification."""
    grant = store.create_grant(identity, policy())
    op = store.reserve(identity, grant["id"], "main", proposal(key="kill-switch-reserved"))
    with pytest.raises(PermissionError, match="operation_not_pending"):
        HandoffStore(store).create(identity, op["id"], "main", request())
    assert store.operation(identity, op["id"])["state"] == "reserved"


def test_handoff_creation_uses_the_journal_transition_table(store, identity):
    """A direct UPDATE bypassed reserved -> {cancelled, failed, awaiting_input}."""
    op = pending(store, identity)
    HandoffStore(store).create(identity, op["id"], "main", request())
    with store.transaction() as cur:
        cur.execute(
            "SELECT event FROM autonomy_events WHERE tenant_id=%s AND owner_id=%s "
            "AND subject_id=%s ORDER BY id",
            (identity.tenant_id, identity.owner_id, op["id"]),
        )
        events = [row["event"] for row in cur.fetchall()]
    assert "reconciling" in events, events


def test_idempotent_replay_is_re_authorized_before_it_returns_success(store, identity):
    op = pending(store, identity)
    handoffs = HandoffStore(store)
    spec = request()
    first = handoffs.create(identity, op["id"], "main", spec)
    store.configure(identity, DISABLED)
    with pytest.raises(PermissionError, match="autonomous_execution_not_enabled"):
        handoffs.create(identity, op["id"], "main", spec)
    store.configure(identity, ENABLED)
    store.revoke_grant(identity, store.operation(identity, op["id"])["grant_id"])
    with pytest.raises(PermissionError, match="grant_revoked"):
        handoffs.create(identity, op["id"], "main", spec)
    assert first["state"] == "awaiting_external_action"


def test_a_foreign_agent_is_refused_before_the_fingerprint_is_compared(store, identity):
    """The existing test passed for the wrong reason: handoff_request_changed
    was raised first, so deleting the agent guard left it green."""
    op = pending(store, identity)
    with pytest.raises(PermissionError, match="agent_not_allowed"):
        HandoffStore(store).create(identity, op["id"], "other-agent", request())


def test_acknowledge_refuses_while_the_switch_is_off_or_the_grant_is_gone(store, identity):
    """The probe returned the private confirmation plan with the feature off."""
    op = pending(store, identity)
    handoffs = HandoffStore(store)
    asked = handoffs.create(identity, op["id"], "main", request())
    store.configure(identity, DISABLED)
    with pytest.raises(PermissionError, match="autonomous_execution_not_enabled"):
        handoffs.acknowledge(identity, asked["id"])
    store.configure(identity, ENABLED)
    store.revoke_grant(identity, store.operation(identity, op["id"])["grant_id"])
    with pytest.raises(PermissionError, match="grant_revoked"):
        handoffs.acknowledge(identity, asked["id"])
    assert handoffs.list(identity)[0]["state"] == "awaiting_external_action"


def test_an_expired_handoff_releases_the_operation_and_the_owner_can_clear_it(store, identity):
    """Three handoffs zeroed a 3000 cap with no merchant contact, then a genuine
    purchase was refused and cancel raised invalid_transition."""
    op = pending(store, identity)
    handoffs = HandoffStore(store)
    asked = handoffs.create(identity, op["id"], "main", request())
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_handoffs SET expires_at=now()-interval '1 second' WHERE id=%s",
            (asked["id"],),
        )
    released = HandoffChecks(store).release_expired(identity)
    assert released == 1
    assert handoffs.list(identity)[0]["state"] == "expired"
    row = store.operation(identity, op["id"])
    assert row["state"] == "awaiting_input"
    assert row["input_reason"] == "external_verification_expired"

    # The owner route clears the wedged operation and returns its reservation.
    store.abandon(identity, op["id"])
    assert store.operation(identity, op["id"])["state"] == "failed"
    assert store.spending_projection(identity)["months"].get("USD", {}) == {}


def test_abandon_is_refused_for_an_operation_that_is_not_wedged(store, identity):
    grant = store.create_grant(identity, policy())
    op = store.reserve(identity, grant["id"], "main", proposal(key="kill-switch-abandon"))
    with pytest.raises(ValueError, match="invalid_transition"):
        store.abandon(identity, op["id"])


# --------------------------------------------------------------------------
# F3 -- the recovery daemon does it automatically after a restart
# --------------------------------------------------------------------------


def test_claim_expires_a_handoff_whose_grant_or_switch_is_gone(store, identity):
    """The probe claimed, decrypted the URL, restored the session and completed
    with the grant revoked and both settings false."""
    op = pending(store, identity)
    handoffs = HandoffStore(store)
    asked = handoffs.create(identity, op["id"], "main", request())
    handoffs.acknowledge(identity, asked["id"])
    store.configure(identity, DISABLED)
    queue = HandoffChecks(store)
    assert queue.claim(identity, asked["id"]) is None
    assert handoffs.list(identity)[0]["state"] == "expired"
    assert store.operation(identity, op["id"])["state"] == "awaiting_input"


def test_claim_expires_a_handoff_after_the_grant_is_revoked(store, identity):
    op = pending(store, identity)
    handoffs = HandoffStore(store)
    asked = handoffs.create(identity, op["id"], "main", request())
    handoffs.acknowledge(identity, asked["id"])
    store.revoke_grant(identity, store.operation(identity, op["id"])["grant_id"])
    assert HandoffChecks(store).claim(identity, asked["id"]) is None
    assert handoffs.list(identity)[0]["state"] == "expired"


def test_candidates_are_bound_to_one_tenant_and_owner(store, identity):
    """candidates() scanned every tenant in the database."""
    op = pending(store, identity)
    asked = HandoffStore(store).create(identity, op["id"], "main", request())
    HandoffStore(store).acknowledge(identity, asked["id"])
    queue = HandoffChecks(store)
    assert queue.candidates(identity) == [asked["id"]]
    other = identity.model_copy(update={"tenant_id": "test-" + uuid4().hex})
    assert queue.candidates(other) == []
    assert queue.candidates(identity.model_copy(update={"owner_id": "bob"})) == []


def test_a_disabled_owner_is_never_scanned(store, identity):
    op = pending(store, identity)
    asked = HandoffStore(store).create(identity, op["id"], "main", request())
    HandoffStore(store).acknowledge(identity, asked["id"])
    oldest_work(store, identity)
    queue = HandoffChecks(store)
    assert identity in queue.enabled_scopes()
    store.configure(identity, DISABLED)
    assert identity not in queue.enabled_scopes()


async def test_recovery_does_nothing_at_all_while_the_switch_is_off(store, identity, monkeypatch):
    from robothor.autonomy import handoff_worker

    op = pending(store, identity)
    asked = HandoffStore(store).create(identity, op["id"], "main", request())
    HandoffStore(store).acknowledge(identity, asked["id"])
    store.configure(identity, DISABLED)
    checks = []
    monkeypatch.setattr(
        handoff_worker,
        "check_one",
        lambda *args: checks.append(args) or asyncio.sleep(0),
    )

    class ThisOwner(HandoffChecks):
        # The daemon scans every enabled owner; this narrows the shared test
        # database to the one whose switch the probe just turned off.
        def enabled_scopes(self):
            return [item for item in super().enabled_scopes() if item == identity]

    assert await handoff_worker.recover_once(ThisOwner(store)) == 0
    assert checks == []
    assert HandoffStore(store).list(identity)[0]["state"] == "checking"
    assert store.operation(identity, op["id"])["state"] == "reconciling"


def test_a_restored_browser_session_is_spent_by_the_attempt_that_uses_it(store, identity):
    from robothor.autonomy.models import ResourceInput

    ref = store.put_resource(
        identity,
        ResourceInput(
            kind="browser_session",
            label="Website session",
            origin="https://shop.example",
            payload=json.dumps({"cookies": [], "origins": []}),
        ),
    )
    assert store.consume_resource(
        identity, ref["id"], "https://shop.example", kind="browser_session", one_shot=True
    ) == {"cookies": [], "origins": []}
    with pytest.raises(PermissionError, match="resource_not_authorized"):
        store.consume_resource(
            identity, ref["id"], "https://shop.example", kind="browser_session", one_shot=True
        )


def test_an_exhausted_check_is_not_byte_identical_to_never_checked(store, identity):
    op = pending(store, identity)
    handoffs = HandoffStore(store)
    asked = handoffs.create(identity, op["id"], "main", request())
    assert handoffs.list(identity)[0]["state"] == "awaiting_external_action"
    handoffs.acknowledge(identity, asked["id"])
    queue = HandoffChecks(store)
    for _ in range(3):
        assert queue.claim(identity, asked["id"])
        with store.transaction() as cur:
            cur.execute(
                "UPDATE autonomy_handoffs SET check_lease_until=now()-interval '1 second' "
                "WHERE id=%s",
                (asked["id"],),
            )
    assert queue.claim(identity, asked["id"]) is None
    assert handoffs.list(identity)[0]["state"] == "unconfirmed"
    # The owner can still ask again; acknowledging resets the attempt counter.
    handoffs.acknowledge(identity, asked["id"])
    assert handoffs.list(identity)[0]["state"] == "checking"


# --------------------------------------------------------------------------
# F4 -- any affirmative sentence anywhere on the origin completes it
# --------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.timeout(45)
async def test_reconciliation_will_not_read_a_different_page_on_the_same_origin(store, identity):
    """The probe completed from https://shop.example/help/faq?q=orders."""
    op = pending(store, identity)
    asked = HandoffStore(store).create(identity, op["id"], "main", request())
    private = HandoffStore(store).acknowledge(identity, asked["id"])

    async def merchant(route):
        # The page that answers is not the page that was pinned: it rewrites
        # its own address to a help article on the same origin.
        await route.fulfill(
            content_type="text/html",
            body="<p id='done'>Order confirmed</p>"
            "<script>history.replaceState({},'','/help/faq?q=orders')</script>",
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", merchant)
            result = await BrowserBroker(store).reconcile_on_page(
                identity,
                op["id"],
                "main",
                ExecutionPlan(
                    url=private["confirmation"]["url"],
                    submit_selector="__unused__",
                    success_selector="#done",
                    success_text="Order confirmed",
                ),
                page,
            )
            assert result["state"] == "reconciling"
            assert result.get("reason") != "confirmation_page_not_registered"
            assert store.operation(identity, op["id"])["state"] == "reconciling"
        finally:
            await browser.close()


def test_a_payment_cannot_be_reconciled_by_the_submission_classifier(store, identity):
    op = pending(store, identity)
    with pytest.raises(PermissionError, match="specific_confirmation_required"):
        HandoffStore(store).create(
            identity,
            op["id"],
            "main",
            request(confirmation={"url": "https://shop.example/status"}),
        )


@pytest.mark.e2e
@pytest.mark.timeout(45)
@pytest.mark.parametrize("selector", ["html body", "main", "p", "div", "body *", "body > *"])
async def test_a_whole_page_guess_is_rejected_at_check_time(store, identity, selector):
    """'html body' completed an operation on a page reading
    "Your order is still pending"."""
    op = pending(store, identity)
    asked = HandoffStore(store).create(
        identity,
        op["id"],
        "main",
        request(
            confirmation={
                "url": STATUS_URL,
                "selector": selector,
                "text": "Order confirmed",
            }
        ),
    )
    private = HandoffStore(store).acknowledge(identity, asked["id"])

    async def merchant(route):
        # Every probed selector matches an element whose VISIBLE text contains
        # the declared criterion; only count and length can tell them apart.
        await route.fulfill(
            content_type="text/html",
            body="<main><div id='wrap'><p>Your order is still pending</p><p>"
            + "Filler sentence. " * 40
            + "</p><p>Order confirmed</p></div></main>",
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", merchant)
            result = await BrowserBroker(store).reconcile_on_page(
                identity,
                op["id"],
                "main",
                ExecutionPlan(
                    url=private["confirmation"]["url"],
                    submit_selector="__unused__",
                    success_selector=private["confirmation"]["selector"],
                    success_text=private["confirmation"]["text"],
                ),
                page,
            )
            assert result["state"] == "reconciling", selector
            assert store.operation(identity, op["id"])["state"] == "reconciling"
        finally:
            await browser.close()


@pytest.mark.e2e
@pytest.mark.timeout(45)
async def test_reconciliation_refuses_a_confirmation_page_no_handoff_registered(store, identity):
    """reconcile_on_page accepted any same-origin URL its caller supplied."""
    navigated = []

    async def watcher(route):
        navigated.append(route.request.url)
        await route.fulfill(content_type="text/html", body='<p id="done">Order confirmed</p>')

    op = pending(store, identity)
    HandoffStore(store).create(identity, op["id"], "main", request())
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", watcher)
            result = await BrowserBroker(store).reconcile_on_page(
                identity,
                op["id"],
                "main",
                ExecutionPlan(
                    url="https://shop.example/help/faq?q=orders",
                    submit_selector="__unused__",
                    success_selector="#done",
                    success_text="Order confirmed",
                ),
                page,
            )
        finally:
            await browser.close()
    assert result["state"] == "reconciling"
    assert result.get("reason") == "confirmation_page_not_registered"
    assert navigated == []
    assert store.operation(identity, op["id"])["state"] == "reconciling"


@pytest.mark.e2e
@pytest.mark.timeout(45)
async def test_reconciliation_still_works_on_the_page_the_broker_submitted_on(store, identity):
    """The ordinary `reconcile` tool, with no handoff involved.

    Pinning the page must not cost the documented flow: an agent whose submit
    ended uncertain reconciles on the page it submitted on, which the bound
    execution plan records before the click.
    """
    from robothor.autonomy.tests.test_handoffs import reserved

    op = reserved(store, identity)
    url = "https://shop.example/checkout?cart=42"
    # The broker binds the plan just before the click and records where the
    # browser ended up; the click is what this test does not need to repeat.
    store.bind_plan(identity, op["id"], "main", {"url": url, "submit_selector": "#submit"})
    store.record_landed_pages(identity, op["id"], [url])
    store.begin_submit(identity, op["id"], "main")

    async def merchant(route):
        await route.fulfill(content_type="text/html", body='<p id="done">Order confirmed</p>')

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", merchant)
            result = await BrowserBroker(store).reconcile_on_page(
                identity,
                op["id"],
                "main",
                ExecutionPlan(
                    url=url,
                    submit_selector="__unused__",
                    success_selector="#done",
                    success_text="Order confirmed",
                ),
                page,
            )
        finally:
            await browser.close()
    assert result["state"] == "completed", result
    assert store.operation(identity, op["id"])["state"] == "completed"


# --------------------------------------------------------------------------
# F5 -- "off" means off, by default
# --------------------------------------------------------------------------


def test_the_feature_ships_disabled_and_inert(store, identity):
    from robothor.autonomy.models import RuntimeSettings as Settings

    assert Settings().enabled is False
    assert Settings().payment_processing is False
    assert Settings().managed_browser is False

    op = pending(store, identity)
    handoffs = HandoffStore(store)
    asked = handoffs.create(identity, op["id"], "main", request())
    handoffs.acknowledge(identity, asked["id"])
    store.configure(identity, DISABLED)

    oldest_work(store, identity)
    queue = HandoffChecks(store)
    assert identity not in queue.enabled_scopes()
    assert queue.claim(identity, asked["id"]) is None
    with pytest.raises(PermissionError, match="autonomous_execution_not_enabled"):
        store.check_reconcile_authority(identity, op["id"], "main")
    with pytest.raises(PermissionError, match="autonomous_execution_not_enabled"):
        handoffs.create(identity, op["id"], "main", request())
    grant = store.create_grant(identity, policy())
    second = store.reserve(
        identity, grant["id"], "main", proposal(key="kill-switch-inert", amount=100)
    )
    with pytest.raises(PermissionError, match="autonomous_execution_not_enabled"):
        store.begin_submit(identity, second["id"], "main")
    with pytest.raises(PermissionError, match="autonomous_execution_not_enabled"):
        store.check_authority(identity, second["id"], "main")


async def test_run_browser_refuses_reconciliation_while_the_switch_is_off(
    store, identity, monkeypatch
):
    from robothor.autonomy import runtime

    op = pending(store, identity)
    store.configure(identity, DISABLED)
    monkeypatch.setattr(runtime, "AutonomyStore", lambda: store)
    spawned = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", lambda *a, **k: spawned.append(a) or None
    )
    result = await runtime.run_browser(
        identity,
        op["id"],
        "main",
        ExecutionPlan(
            url=STATUS_URL,
            submit_selector="__unused__",
            success_selector="#done",
            success_text="Order confirmed",
        ),
        reconcile=True,
    )
    assert result == {
        "error": "autonomous_execution_not_enabled",
        "setup_path": "/account/autonomy",
    }
    assert spawned == []


async def test_worker_handle_refuses_reconciliation_while_the_switch_is_off(store, identity):
    import base64

    from robothor.autonomy import worker

    op = pending(store, identity)
    store.configure(identity, DISABLED)
    result = await worker.handle(
        {
            "scope": identity.model_dump(),
            "operation_id": op["id"],
            "agent_id": "main",
            "database": _dsn(),
            "keys": {"v1": base64.b64encode(b"x" * 32).decode()},
            "key_id": "v1",
            "reconcile": True,
            "plan": {
                "url": STATUS_URL,
                "submit_selector": "__unused__",
                "success_selector": "#done",
                "success_text": "Order confirmed",
            },
        }
    )
    assert result == {"error": "autonomous_execution_not_enabled"}
    assert store.operation(identity, op["id"])["state"] == "submitting"


def _dsn():
    import os

    from psycopg2.extensions import parse_dsn

    return parse_dsn(os.environ["AUTONOMY_TEST_DSN"])


# --------------------------------------------------------------------------
# F6 -- the test gap: run_browser was mocked in every async recovery test
# --------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.timeout(120)
async def test_the_real_recovery_path_refuses_and_then_works(store, identity, monkeypatch):
    """handoff_worker -> runtime.run_browser -> worker.py, unmocked.

    A real loopback merchant serves the confirmation page to real Chromium, so
    the refusal below is a refusal of a path that otherwise completes.
    """
    import base64
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import httpx

    from robothor import config
    from robothor.autonomy import handoff_worker, runtime, worker

    body = b"<p id='done'>Order confirmed</p>"

    class Merchant(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Merchant)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    seen: list[str] = []

    async def loopback(route):
        path = route.request.url.split("https://shop.example", 1)[-1]
        seen.append(path)
        async with httpx.AsyncClient() as client:
            upstream = await client.get(f"http://127.0.0.1:{port}{path}")
        await route.fulfill(
            status=upstream.status_code, content_type="text/html", body=upstream.text
        )

    try:
        op = pending(store, identity)
        handoffs = HandoffStore(store)
        asked = handoffs.create(identity, op["id"], "main", request())
        handoffs.acknowledge(identity, asked["id"])
        store.configure(identity, DISABLED)

        # The real runtime, not a mock: the subprocess must never be reached.
        await handoff_worker.check_one(HandoffChecks(store), identity, asked["id"])
        assert seen == []
        assert store.operation(identity, op["id"])["state"] == "awaiting_input"
        assert handoffs.list(identity)[0]["state"] == "expired"

        # The wiring itself, unmocked: a second handoff on a live switch goes
        # handoff_worker -> runtime.run_browser -> a real subprocess running
        # robothor.autonomy.worker, which launches real Chromium. It cannot
        # reach shop.example (the broker refuses non-public destinations), so
        # it comes back reconciling -- but "confirmation_not_found" is the
        # BROKER's answer. A subprocess that never started would have said
        # broker_unavailable.
        store.configure(identity, ENABLED)
        second_grant = store.create_grant(identity, policy())
        second = store.reserve(
            identity, second_grant["id"], "main", proposal(key="kill-switch-real-path", amount=100)
        )
        store.bind_plan(
            identity,
            second["id"],
            "main",
            {"url": SUBMISSION_URL, "submit_selector": "#submit"},
        )
        store.record_landed_pages(identity, second["id"], [SUBMISSION_URL, STATUS_URL])
        store.begin_submit(identity, second["id"], "main")
        live = handoffs.create(identity, second["id"], "main", request())
        handoffs.acknowledge(identity, live["id"])
        monkeypatch.setenv("ROBOTHOR_DB_HOST", _dsn().get("host", ""))
        monkeypatch.setenv("ROBOTHOR_DB_NAME", _dsn()["dbname"])
        monkeypatch.setenv("ROBOTHOR_DB_USER", _dsn().get("user", ""))
        monkeypatch.setattr(config, "_config", None)
        monkeypatch.setattr(runtime, "AutonomyStore", lambda: store)
        claim = HandoffChecks(store).claim(identity, live["id"])
        assert claim
        spawned = await runtime.run_browser(
            identity,
            second["id"],
            "main",
            ExecutionPlan(
                url=STATUS_URL,
                submit_selector="__unused__",
                success_selector="#done",
                success_text="Order confirmed",
            ),
            reconcile=True,
        )
        assert spawned["reason"] == "confirmation_not_found", spawned
        assert store.operation(identity, second["id"])["state"] == "reconciling"

        # And the same refusal through the same real subprocess entry point.
        store.configure(identity, DISABLED)
        assert await runtime.run_browser(
            identity,
            second["id"],
            "main",
            ExecutionPlan(
                url=STATUS_URL,
                submit_selector="__unused__",
                success_selector="#done",
                success_text="Order confirmed",
            ),
            reconcile=True,
        ) == {"error": "autonomous_execution_not_enabled", "setup_path": "/account/autonomy"}

        # The positive control runs worker.handle -- the module holding the
        # skip -- against real Chromium and the loopback merchant.
        store.configure(identity, ENABLED)
        with store.transaction() as cur:
            cur.execute(
                "UPDATE autonomy_operations SET state='reconciling',input_reason=NULL WHERE id=%s",
                (op["id"],),
            )
        monkeypatch.setattr(worker, "public_request", loopback)
        payload = {
            "scope": identity.model_dump(),
            "operation_id": op["id"],
            "agent_id": "main",
            "database": _dsn(),
            "keys": {"v1": base64.b64encode(b"x" * 32).decode()},
            "key_id": "v1",
            "reconcile": True,
            "plan": {
                "url": STATUS_URL,
                "submit_selector": "__unused__",
                "success_selector": "#done",
                "success_text": "Order confirmed",
            },
        }
        result = await worker.handle(payload)
        assert result["state"] == "completed", result
        assert seen, "the loopback merchant was never reached"
        assert store.operation(identity, op["id"])["state"] == "completed"
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------
# Second review round. The bypasses above refuse, but the agent could still
# choose the page it was judged against, one raw state UPDATE survived the
# centralisation, and the daemon's owner scan was unbounded.
# --------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.timeout(45)
async def test_an_agent_cannot_register_the_page_it_wants_to_be_judged_against(store, identity):
    """The residual after the first round: `handoffs.create` pinned only the
    origin, and the registered set unioned every handoff's own URL, so the
    agent satisfied the pin with its own declaration -- and completed a
    purchase on "Order confirmed? You have 30 days to return it."
    """
    from robothor.autonomy.tests.test_handoffs import reserved

    op = reserved(store, identity)
    store.bind_plan(
        identity,
        op["id"],
        "main",
        {"url": "https://shop.example/checkout", "submit_selector": "#submit"},
    )
    store.begin_submit(identity, op["id"], "main")
    with pytest.raises(PermissionError, match="confirmation_page_not_observed"):
        HandoffStore(store).create(
            identity,
            op["id"],
            "main",
            request(
                confirmation={
                    "url": "https://shop.example/help/article/refund-policy?v=2",
                    "selector": "#q3",
                    "text": "Order confirmed",
                }
            ),
        )
    assert HandoffStore(store).list(identity) == []


def test_a_handoff_needs_a_page_the_broker_actually_submitted_on(store, identity):
    """With no bound plan there is no observed page, so nothing is registrable."""
    from robothor.autonomy.tests.test_handoffs import reserved

    op = reserved(store, identity)
    store.begin_submit(identity, op["id"], "main")
    with pytest.raises(PermissionError, match="confirmation_page_not_observed"):
        HandoffStore(store).create(
            identity,
            op["id"],
            "main",
            request(
                confirmation={
                    "url": "https://shop.example/status",
                    "selector": "#d",
                    "text": "Order confirmed",
                }
            ),
        )


@pytest.mark.e2e
@pytest.mark.timeout(45)
async def test_a_padded_element_is_not_a_specific_confirmation(store, identity):
    """A 277-character node stayed inside a flat 300-character bound. The
    declared criterion has to be the substance of the element, not a needle."""
    from robothor.autonomy.tests.test_handoffs import reserved

    op = reserved(store, identity)
    url = "https://shop.example/checkout"
    store.bind_plan(identity, op["id"], "main", {"url": url, "submit_selector": "#submit"})
    store.record_landed_pages(identity, op["id"], [url])
    store.begin_submit(identity, op["id"], "main")

    async def merchant(route):
        await route.fulfill(
            content_type="text/html",
            body="<p id='done'>Order confirmed. " + "x" * 260 + "</p>",
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", merchant)
            result = await BrowserBroker(store).reconcile_on_page(
                identity,
                op["id"],
                "main",
                ExecutionPlan(
                    url=url,
                    submit_selector="__unused__",
                    success_selector="#done",
                    success_text="Order confirmed",
                ),
                page,
            )
        finally:
            await browser.close()
    assert result["state"] == "reconciling", result
    assert result["reason"] == "confirmation_not_found"
    assert store.operation(identity, op["id"])["state"] != "completed"


def test_losing_a_workflow_cannot_invent_uncertainty_for_a_reserved_operation(store, identity):
    """`close(..., "lost")` did the exact raw UPDATE the transition table
    forbids, with no operation event -- and expire_unauthorized calls it, so
    revoking a grant pushed a never-submitted operation into reconciling."""
    from robothor.autonomy.tests.test_handoffs import reserved
    from robothor.autonomy.workflows.store import WorkflowStore

    op = reserved(store, identity)
    repository = WorkflowStore(store)
    workflow = repository.open(
        identity, "main", op["id"], str(uuid4()), {"url": "https://shop.example/checkout"}
    )
    with pytest.raises(ValueError, match="invalid_transition"):
        store.finish(identity, op["id"], "reconciling")
    repository.close(identity, "main", workflow["id"], "lost")
    assert store.operation(identity, op["id"])["state"] == "failed"
    with store.transaction() as cur:
        cur.execute(
            "SELECT event FROM autonomy_events WHERE tenant_id=%s AND owner_id=%s AND subject_id=%s",
            (identity.tenant_id, identity.owner_id, op["id"]),
        )
        assert "failed" in [row["event"] for row in cur.fetchall()]


def test_losing_a_workflow_after_submission_still_means_uncertainty(store, identity):
    from robothor.autonomy.tests.test_handoffs import reserved
    from robothor.autonomy.workflows.store import WorkflowStore

    op = reserved(store, identity)
    repository = WorkflowStore(store)
    workflow = repository.open(
        identity, "main", op["id"], str(uuid4()), {"url": "https://shop.example/checkout"}
    )
    store.begin_submit(identity, op["id"], "main", workflow_id=workflow["id"])
    repository.close(identity, "main", workflow["id"], "lost")
    assert store.operation(identity, op["id"])["state"] == "reconciling"


def test_the_owner_scan_is_bounded_and_driven_by_pending_work(store, identity):
    """SELECT tenant_id, owner_id, settings FROM autonomy_settings had no
    WHERE, no LIMIT and no sharding, in every bridge worker every 60 seconds."""
    queue = HandoffChecks(store)
    # Settings exist for this owner (the fixture enables them) but there is no
    # handoff to check, so the daemon has no reason to visit them at all.
    assert identity not in queue.enabled_scopes()

    op = pending(store, identity)
    asked = HandoffStore(store).create(identity, op["id"], "main", request())
    HandoffStore(store).acknowledge(identity, asked["id"])
    oldest_work(store, identity)
    assert identity in queue.enabled_scopes()
    assert len(queue.enabled_scopes()) <= HandoffChecks.SCAN_LIMIT


def test_an_expired_handoff_still_reaches_the_sweep(store, identity):
    """Releasing a lapsed handoff is work too, so its owner must stay visible."""
    op = pending(store, identity)
    asked = HandoffStore(store).create(identity, op["id"], "main", request())
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_handoffs SET expires_at=now()-interval '1 second' WHERE id=%s",
            (asked["id"],),
        )
    oldest_work(store, identity)
    assert identity in HandoffChecks(store).enabled_scopes()


def test_same_page_answers_rather_than_raising_on_a_url_it_cannot_parse():
    from robothor.autonomy.broker import same_page

    assert same_page("https://shop.example/a?b=1", "https://shop.example/a?b=1")
    assert not same_page("http://shop.example/a", "https://shop.example/a")
    assert not same_page("https://shop.example/a", "not a url at all")
    assert not same_page("https://alice:secret@shop.example/a", "https://shop.example/a")


def test_the_one_gate_refuses_a_terminal_operation(store, identity):
    op = pending(store, identity)
    store.finish(
        identity,
        op["id"],
        "completed",
        {"origin": "https://shop.example", "confirmation_sha256": "a" * 64},
    )
    with pytest.raises(PermissionError, match="operation_not_pending"):
        store.check_reconcile_authority(identity, op["id"], "main")


# --------------------------------------------------------------------------
# Third review round. The pin was right but the recorded set was the agent's
# declared plan URL, so the shape the feature exists to serve -- submit here,
# confirm there -- became unrepresentable, and the element bound scaled with
# the declared phrase and refused ordinary merchants.
# --------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.timeout(60)
async def test_a_status_page_the_browser_was_redirected_to_is_registrable(store, identity):
    """POST -> redirect to /order/12345/confirmation, the commonest checkout.

    The broker records where it actually LANDED, so a handoff may name that
    page even though the agent never submitted on it.
    """
    from robothor.autonomy.tests.test_handoffs import reserved

    op = reserved(store, identity)
    posts = []

    async def merchant(route):
        if route.request.method == "POST":
            posts.append(route.request.url)
            await route.fulfill(body="ok")
        elif "/order/12345/confirmation" in route.request.url:
            await route.fulfill(
                content_type="text/html", body="<p id='done'>Awaiting your device approval</p>"
            )
        else:
            await route.fulfill(
                content_type="text/html",
                body="""<p id="amount">$6.00</p>
                <button id="submit" onclick="fetch('/pay',{method:'POST'}).then(()=>{
                location.href='/order/12345/confirmation';})">Buy</button><p id="done"></p>""",
            )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", merchant)
            result = await BrowserBroker(store).execute_on_page(
                identity,
                op["id"],
                "main",
                ExecutionPlan(
                    url="https://shop.example/checkout",
                    submit_selector="#submit",
                    amount_selector="#amount",
                    success_selector="#never",
                    success_text="Order confirmed",
                ),
                page,
            )
            assert result["state"] == "reconciling", result
        finally:
            await browser.close()
    assert len(posts) == 1
    landed = store.operation(identity, op["id"])["execution_plan"]["landed_urls"]
    assert "https://shop.example/order/12345/confirmation" in landed
    # The agent may now name the page the browser was actually sent to.
    asked = HandoffStore(store).create(
        identity,
        op["id"],
        "main",
        request(
            confirmation={
                "url": "https://shop.example/order/12345/confirmation",
                "selector": "#done",
                "text": "Order confirmed",
            }
        ),
    )
    assert asked["state"] == "awaiting_external_action"


def test_a_page_the_browser_never_reached_is_still_refused(store, identity):
    """The property the landed-URL record must not give away."""
    from robothor.autonomy.tests.test_handoffs import reserved

    op = reserved(store, identity)
    store.bind_plan(
        identity,
        op["id"],
        "main",
        {"url": "https://shop.example/checkout", "submit_selector": "#submit"},
    )
    store.record_landed_pages(identity, op["id"], ["https://shop.example/checkout"])
    store.begin_submit(identity, op["id"], "main")
    with pytest.raises(PermissionError, match="confirmation_page_not_observed"):
        HandoffStore(store).create(
            identity,
            op["id"],
            "main",
            request(
                confirmation={
                    "url": "https://shop.example/help/article/refund-policy?v=2",
                    "selector": "#q3",
                    "text": "Order confirmed",
                }
            ),
        )


def test_a_declared_plan_url_the_browser_never_loaded_is_not_an_observation(store, identity):
    """bind_plan writes the AGENT's value. Only the landing record counts."""
    from robothor.autonomy.handoffs import observed_submission_pages
    from robothor.autonomy.tests.test_handoffs import reserved

    op = reserved(store, identity)
    store.bind_plan(
        identity,
        op["id"],
        "main",
        {"url": "https://shop.example/help/article/refund-policy?v=2", "submit_selector": "#x"},
    )
    assert observed_submission_pages(store.operation(identity, op["id"])) == set()
    store.begin_submit(identity, op["id"], "main")
    with pytest.raises(PermissionError, match="confirmation_page_not_observed"):
        HandoffStore(store).create(
            identity,
            op["id"],
            "main",
            request(
                confirmation={
                    "url": "https://shop.example/help/article/refund-policy?v=2",
                    "selector": "#q3",
                    "text": "Order confirmed",
                }
            ),
        )


def test_a_landing_record_does_not_look_like_a_changed_plan(store, identity):
    """bind_plan's guard compares the agent's plan, not the broker's notes."""
    from robothor.autonomy.tests.test_handoffs import reserved

    op = reserved(store, identity)
    plan = {"url": "https://shop.example/checkout", "submit_selector": "#submit"}
    store.bind_plan(identity, op["id"], "main", plan)
    store.record_landed_pages(identity, op["id"], ["https://shop.example/order/7"])
    store.bind_plan(identity, op["id"], "main", plan)
    with pytest.raises(PermissionError, match="execution_plan_changed"):
        store.bind_plan(identity, op["id"], "main", {**plan, "submit_selector": "#other"})
    assert store.operation(identity, op["id"])["execution_plan"]["landed_urls"] == [
        "https://shop.example/order/7"
    ]


def test_the_landing_record_is_bounded_and_same_origin(store, identity):
    from robothor.autonomy.tests.test_handoffs import reserved

    op = reserved(store, identity)
    store.bind_plan(
        identity,
        op["id"],
        "main",
        {"url": "https://shop.example/checkout", "submit_selector": "#submit"},
    )
    store.record_landed_pages(
        identity,
        op["id"],
        [
            "https://other.example/x",
            "about:blank",
            *[f"https://shop.example/{n}" for n in range(20)],
        ],
    )
    landed = store.operation(identity, op["id"])["execution_plan"]["landed_urls"]
    assert all(item.startswith("https://shop.example/") for item in landed)
    assert len(landed) <= 10


@pytest.mark.e2e
@pytest.mark.timeout(45)
async def test_an_ordinary_merchant_confirmation_panel_is_accepted(store, identity):
    """Measured refusal at 121 characters: "Thank you, Alice! Order confirmed
    -- order #ABC-123456789. We emailed a receipt. Estimated delivery:
    Tuesday 24 September." The bound also scaled with the declared phrase, so
    the only workaround was to guess more future wording -- the behaviour
    #621 removed, and impossible anyway without knowing the order number."""
    from robothor.autonomy.tests.test_handoffs import reserved

    op = reserved(store, identity)
    url = "https://shop.example/checkout"
    store.bind_plan(identity, op["id"], "main", {"url": url, "submit_selector": "#submit"})
    store.record_landed_pages(identity, op["id"], [url])
    store.begin_submit(identity, op["id"], "main")
    panel = (
        "Thank you, Alice! Order confirmed \u2014 order #ABC-123456789. "
        "We emailed a receipt. Estimated delivery: Tuesday 24 September."
    )
    assert len(" ".join(panel.split())) > 95

    async def merchant(route):
        await route.fulfill(content_type="text/html", body=f"<p id='done'>{panel}</p>")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", merchant)
            result = await BrowserBroker(store).reconcile_on_page(
                identity,
                op["id"],
                "main",
                ExecutionPlan(
                    url=url,
                    submit_selector="__unused__",
                    success_selector="#done",
                    success_text="Order confirmed",
                ),
                page,
            )
        finally:
            await browser.close()
    assert result["state"] == "completed", result


# --------------------------------------------------------------------------
# Fourth review round: the landing record kept the wrong ten, and the
# verification-link path records nothing at all -- deliberately, it turns out.
# --------------------------------------------------------------------------


def test_the_landing_record_keeps_the_final_page_not_the_first_ten(store, identity):
    """The cap dropped the page that matters.

    Truncating to the FIRST ten landings throws away the confirmation page of
    any merchant that redirects more than ten times -- the one page a check
    almost always needs. The page the commitment was made on is worth keeping
    too, so the record keeps the first landing and the most recent ones.
    """
    from robothor.autonomy.broker import MAX_LANDED_PAGES
    from robothor.autonomy.tests.test_handoffs import reserved

    op = reserved(store, identity)
    store.bind_plan(
        identity,
        op["id"],
        "main",
        {"url": "https://shop.example/checkout", "submit_selector": "#submit"},
    )
    chain = [
        "https://shop.example/checkout",
        *[f"https://shop.example/hop/{n}" for n in range(14)],
        "https://shop.example/order/final",
    ]
    store.record_landed_pages(identity, op["id"], chain)
    landed = store.operation(identity, op["id"])["execution_plan"]["landed_urls"]
    assert len(landed) == MAX_LANDED_PAGES
    assert landed[0] == "https://shop.example/checkout", "the commitment page"
    assert landed[-1] == "https://shop.example/order/final", "the page that matters"

    store.begin_submit(identity, op["id"], "main")
    asked = HandoffStore(store).create(
        identity,
        op["id"],
        "main",
        request(
            confirmation={
                "url": "https://shop.example/order/final",
                "selector": "#done",
                "text": "Order confirmed",
            }
        ),
    )
    assert asked["state"] == "awaiting_external_action"


@pytest.mark.e2e
@pytest.mark.timeout(90)
async def test_a_long_redirect_chain_still_registers_where_it_ended(store, identity):
    """Twelve hops, then the confirmation page. The browser went there."""
    from robothor.autonomy.tests.test_handoffs import reserved

    op = reserved(store, identity)
    hops = 12

    async def merchant(route):
        url = route.request.url
        if route.request.method == "POST":
            await route.fulfill(body="ok")
            return
        if "/hop/" in url:
            step = int(url.rsplit("/", 1)[1])
            target = f"/hop/{step + 1}" if step + 1 < hops else "/order/final"
            await route.fulfill(
                content_type="text/html",
                body=f"<script>location.replace('{target}')</script>",
            )
            return
        if "/order/final" in url:
            await route.fulfill(
                content_type="text/html", body="<p id='done'>Awaiting your device approval</p>"
            )
            return
        await route.fulfill(
            content_type="text/html",
            body="""<p id="amount">$6.00</p>
            <button id="submit" onclick="fetch('/pay',{method:'POST'}).then(()=>{
            location.href='/hop/0';})">Buy</button><p id="done"></p>""",
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", merchant)
            result = await BrowserBroker(store).execute_on_page(
                identity,
                op["id"],
                "main",
                ExecutionPlan(
                    url="https://shop.example/checkout",
                    submit_selector="#submit",
                    amount_selector="#amount",
                    success_selector="#done",
                    success_text="Order confirmed",
                ),
                page,
            )
            assert result["state"] == "reconciling", result
        finally:
            await browser.close()
    from robothor.autonomy.broker import MAX_LANDED_PAGES

    landed = store.operation(identity, op["id"])["execution_plan"]["landed_urls"]
    # More hops than the cap, so this only holds if the record drops from the
    # middle: keeping the oldest ten would have lost the final page.
    assert len(landed) == MAX_LANDED_PAGES, landed
    assert landed[0] == "https://shop.example/checkout", landed
    assert "https://shop.example/order/final" in landed, landed
    asked = HandoffStore(store).create(
        identity,
        op["id"],
        "main",
        request(
            confirmation={
                "url": "https://shop.example/order/final",
                "selector": "#done",
                "text": "Order confirmed",
            }
        ),
    )
    assert asked["state"] == "awaiting_external_action"


@pytest.mark.e2e
@pytest.mark.timeout(45)
async def test_a_verification_link_is_never_recorded_as_a_landing(store, identity):
    """Why the verification-link path deliberately records nothing.

    On that path the page the broker navigates to IS the credential: a
    one-time sign-in link, consumed from the vault and masked through
    ``protected_values``. ``execution_plan`` is plaintext and the agent can
    read it back through ``operation``, so recording that landing would hand
    the agent the magic link. The cost is that a login completed this way can
    never be handoff-checked afterwards; the alternative is disclosing a
    credential, so the gap is intentional and documented.
    """
    from datetime import UTC, datetime, timedelta

    from robothor.autonomy.handoffs import observed_submission_pages
    from robothor.autonomy.models import Delegation, ResourceInput, WebOperation

    secret_link = "https://shop.example/magic?token=MagicLinkCanary9"
    grant = store.create_grant(
        identity,
        Delegation(
            agent_ids={"main"},
            origins={"https://shop.example"},
            actions={"login"},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    op = store.reserve(
        identity,
        grant["id"],
        "main",
        WebOperation(
            origin="https://shop.example",
            action="login",
            purpose="Sign in",
            idempotency_key="verification-link-landing",
        ),
    )
    link = store.put_resource(
        identity,
        ResourceInput(
            kind="credential",
            label="Sign-in link",
            origin="https://shop.example",
            payload=json.dumps({"username": "alice", "password": secret_link}),
        ),
    )

    async def merchant(route):
        await route.fulfill(content_type="text/html", body="<p id='done'>You are signed in</p>")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", merchant)
            result = await BrowserBroker(store).execute_on_page(
                identity,
                op["id"],
                "main",
                ExecutionPlan(
                    url="https://shop.example/signin",
                    submit_selector="__unused__",
                    success_selector="#done",
                    success_text="You are signed in",
                    verification_link_id=link["id"],
                ),
                page,
            )
            assert result["state"] == "completed", result
        finally:
            await browser.close()
    row = store.operation(identity, op["id"])
    assert "MagicLinkCanary9" not in json.dumps(row["execution_plan"])
    assert (row["execution_plan"] or {}).get("landed_urls") is None
    # No landing on record means no page a handoff could ever name: the gate
    # has nothing to match against, so it refuses rather than guessing.
    assert observed_submission_pages(row) == set()
