"""Exclusive receipt capture never turns a confirmed payment into a retry."""

import hashlib

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.broker import BrowserBroker, ExecutionPlan
from robothor.autonomy.receipt_capture import capture_receipt
from robothor.autonomy.terms_audit import TermsAudit
from robothor.autonomy.tests.test_payment_journal import purchase

pytestmark = pytest.mark.e2e


async def test_capture_masks_private_values_and_keeps_only_metadata_in_result(store, identity):
    op = purchase(store, identity)
    broker = BrowserBroker(store)
    broker.protected_values.add("ReceiptPrivateCanary")
    phrase = "Order confirmed"
    store.finish(
        identity,
        op,
        "completed",
        {
            "origin": "https://shop.example",
            "confirmation_sha256": hashlib.sha256(phrase.encode()).hexdigest(),
        },
    )
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route(
                "**/*",
                lambda r: r.fulfill(
                    content_type="text/html",
                    body='<h1 id="done">Order confirmed</h1><p>Item: book. ReceiptPrivateCanary</p>',
                ),
            )
            await page.goto("https://shop.example/receipt")
            result = await capture_receipt(
                broker,
                identity,
                op,
                "main",
                page,
                plan=ExecutionPlan(
                    url=page.url,
                    submit_selector="#unused",
                    success_selector="#done",
                    success_text=phrase,
                ),
            )
            assert result["capture_status"] == "captured"
            assert "ReceiptPrivateCanary" not in str(result) and "Item: book" not in str(result)
            saved = TermsAudit(store).read(identity, op, result["id"])["snapshot"]
            assert saved["phase"] == "after_confirmation"
            assert "Item: book" in saved["documents"][0]["text"]
            assert "ReceiptPrivateCanary" not in str(saved)
        finally:
            await browser.close()


async def test_after_code_never_reads_page_and_storage_failure_does_not_raise(
    store, identity, monkeypatch
):
    op = purchase(store, identity)
    store.finish(
        identity,
        op,
        "completed",
        {"origin": "https://shop.example", "confirmation_sha256": "a" * 64},
    )
    broker = BrowserBroker(store)
    broker._used_transient_code = True

    class NoPageAccess:
        def __getattr__(self, name):
            pytest.fail("Receipt capture touched page after code entry")

    result = await capture_receipt(broker, identity, op, "main", NoPageAccess())
    assert result["capture_status"] == "withheld_after_code"
    saved = TermsAudit(store).read(identity, op, result["id"])["snapshot"]
    assert saved["documents"][0]["text"] == ""

    def fail(*args, **kwargs):
        raise RuntimeError("private exception canary")

    monkeypatch.setattr(TermsAudit, "record", fail)
    result = await capture_receipt(broker, identity, op, "main", NoPageAccess())
    assert result == {"capture_status": "unavailable"}
    assert store.operation(identity, op)["state"] == "completed"


@pytest.mark.parametrize("capture_fails", [False, True])
async def test_broker_receipt_capture_never_repeats_purchase(
    store, identity, monkeypatch, capture_fails
):
    from robothor.autonomy.tests.test_store import policy, proposal

    grant = store.create_grant(identity, policy())
    op = store.reserve(identity, grant["id"], "main", proposal())
    original = TermsAudit.record

    def record(self, scope, operation_id, agent_id, snapshot):
        if capture_fails and snapshot.phase == "after_confirmation":
            raise RuntimeError("private storage error")
        return original(self, scope, operation_id, agent_id, snapshot)

    monkeypatch.setattr(TermsAudit, "record", record)
    posts = []

    async def merchant(route):
        if route.request.method == "POST":
            posts.append(route.request.url)
            await route.fulfill(body="Order confirmed")
        else:
            await route.fulfill(
                content_type="text/html",
                body="""<p id="amount">$6.00</p>
            <button id="buy" onclick="fetch('/buy',{method:'POST'}).then(r=>r.text()).then(t=>document.querySelector('#done').textContent=t)">Buy</button>
            <h1 id="done"></h1><p>Receipt details: test book</p>""",
            )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", merchant)
            broker = BrowserBroker(store)
            plan = ExecutionPlan(
                url="https://shop.example/checkout",
                amount_selector="#amount",
                submit_selector="#buy",
                success_selector="#done",
                success_text="Order confirmed",
            )
            result = await broker.execute_on_page(identity, op["id"], "main", plan, page)
            assert result["state"] == "completed"
            assert result["receipt"]["capture_status"] == (
                "unavailable" if capture_fails else "captured"
            )
            assert "Receipt details" not in str(result)
            await broker.execute_on_page(identity, op["id"], "main", plan, page)
            assert len(posts) == 1
            assert store.operation(identity, op["id"])["state"] == "completed"
        finally:
            await browser.close()


async def test_retained_reconciliation_captures_receipt_before_page_closes(
    store, identity, monkeypatch
):
    from uuid import uuid4

    from robothor.autonomy.tests.test_store import policy, proposal
    from robothor.autonomy.workflows import outcome
    from robothor.autonomy.workflows.manager import WorkflowManager

    monkeypatch.setattr(outcome, "OUTCOME_TIMEOUT_SECONDS", 0.1)
    grant = store.create_grant(identity, policy())
    op = store.reserve(identity, grant["id"], "main", proposal())
    posts = []

    async def merchant(route):
        if route.request.method == "POST":
            posts.append(route.request.url)
            await route.fulfill(body="Your order has been confirmed.")
        else:
            await route.fulfill(
                content_type="text/html",
                body="""<p id="amount">$6.00</p>
            <button id="buy" onclick="fetch('/buy',{method:'POST'}).then(r=>r.text()).then(t=>setTimeout(()=>{document.querySelector('#done').textContent=t;},1500))">Buy</button><p id="done"></p><p>Receipt ready</p>""",
            )

    async with async_playwright() as pw:

        async def factory():
            return await pw.chromium.launch(headless=True)

        manager = WorkflowManager(store, factory, request_router=merchant)
        try:
            opened = await manager.open(identity, "main", op["id"], "https://shop.example/checkout")
            wid = opened["workflow_id"]
            result = await manager.execute(
                identity,
                "main",
                wid,
                str(uuid4()),
                0,
                ExecutionPlan(
                    url="https://shop.example/checkout",
                    submit_selector="#buy",
                    amount_selector="#amount",
                ),
            )
            assert result["state"] == "reconciling"
            await manager._live[wid].page.wait_for_timeout(2000)
            observed = await manager.inspect(identity, "main", wid)
            message = next(
                c for c in observed["confirmations"] if "has been confirmed" in c["text"]
            )
            result = await manager.reconcile(
                identity, "main", wid, str(uuid4()), 0, message["selector"], message["text"]
            )
            assert (
                result["state"] == "completed" and result["receipt"]["capture_status"] == "captured"
            )
            saved = TermsAudit(store).read(identity, op["id"], result["receipt"]["id"])["snapshot"]
            assert "Receipt ready" in saved["documents"][0]["text"]
            assert manager.active_count == 0 and len(posts) == 1
        finally:
            await manager.shutdown()


@pytest.mark.parametrize("page_state", ["automatic", "changed", "foreign"])
async def test_capture_rechecks_confirmation_and_origin(store, identity, page_state):
    op = purchase(store, identity)
    digest = hashlib.sha256(b"Order confirmed").hexdigest()
    store.finish(
        identity, op, "completed", {"origin": "https://shop.example", "confirmation_sha256": digest}
    )
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            text = "Order confirmed" if page_state != "changed" else "Unrelated private page"
            await page.route(
                "**/*", lambda r: r.fulfill(content_type="text/html", body="<h1>" + text + "</h1>")
            )
            origin = "https://other.example" if page_state == "foreign" else "https://shop.example"
            await page.goto(origin + "/receipt")
            result = await capture_receipt(BrowserBroker(store), identity, op, "main", page)
            expected = "captured" if page_state == "automatic" else "unavailable"
            assert result["capture_status"] == expected
            saved = TermsAudit(store).read(identity, op, result["id"])["snapshot"]
            if expected == "unavailable":
                assert saved["documents"][0]["text"] == ""
            assert store.operation(identity, op)["state"] == "completed"
        finally:
            await browser.close()
