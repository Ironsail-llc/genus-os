"""Device handoff rechecks survive a new browser and never click checkout again."""

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.broker import BrowserBroker, ExecutionPlan
from robothor.autonomy.handoffs import HandoffStore
from robothor.autonomy.tests.test_handoffs import request, reserved

pytestmark = pytest.mark.e2e


async def test_external_approval_reconciles_with_no_second_post_or_script_mutation(store, identity):
    op = reserved(store, identity)
    submitted = []
    status_reads = []
    approved = False

    async def merchant(route):
        if route.request.method == "POST":
            submitted.append(route.request.url)
            await route.fulfill(body="Approve on your device")
        elif "/status" in route.request.url:
            status_reads.append(route.request.url)
            await route.fulfill(
                content_type="text/html",
                body='<p id="done">'
                + ("Order confirmed" if approved else "Verification pending")
                + """</p><script>fetch('/retry-checkout',{method:'POST'}).catch(()=>{});</script>""",
            )
        else:
            await route.fulfill(
                content_type="text/html",
                body="""<p id="amount">$6.00</p><button id="submit" onclick="fetch('/pay',{method:'POST'}).then(r=>r.text()).then(t=>{document.querySelector('#done').textContent=t;location.href='/status';})">Buy</button><p id="done"></p>""",
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
            assert result["state"] == "reconciling"
            assert "https://shop.example/pay" in submitted
            # The merchant redirected the browser to its own status page, and
            # that page's script fired while the submission window was still
            # open. What must not happen is another request DURING the
            # read-only check below.
            committed = list(submitted)
            reads_before = len(status_reads)
        finally:
            await browser.close()
        # The broker was redirected here after the commitment, so this page
        # -- not the checkout it submitted on -- is what the handoff names.
        assert (
            "https://shop.example/status"
            in (store.operation(identity, op["id"])["execution_plan"]["landed_urls"])
        )
        handoff = HandoffStore(store).create(
            identity,
            op["id"],
            "main",
            request(
                confirmation={
                    "url": "https://shop.example/status",
                    "selector": "#done",
                    "text": "Order confirmed",
                }
            ),
        )
        private = HandoffStore(store).acknowledge(identity, handoff["id"])
        confirmation = private["confirmation"]
        plan = ExecutionPlan(
            url=confirmation["url"],
            submit_selector="__unused__",
            success_selector=confirmation["selector"],
            success_text=confirmation["text"],
        )
        # An acknowledgment alone cannot confirm the pending charge.
        for approved in (False, True):
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/*", merchant)
                result = await BrowserBroker(store).reconcile_on_page(
                    identity, op["id"], "main", plan, page
                )
                await page.wait_for_timeout(100)
                assert result["state"] == ("completed" if approved else "reconciling")
                assert submitted == committed
            finally:
                await browser.close()
        assert len(status_reads) == reads_before + 2
        assert HandoffStore(store).list(identity)[0]["state"] == "resolved"


async def test_automatic_handoff_confirmation_uses_affirmative_observation_not_guess(
    store, identity, monkeypatch
):
    """Automatic outcome detection stays available for non-payment operations.

    Rewritten 2026-09-20: this used a $6 purchase. Reusing the submission
    classifier to reconcile money is exactly the F4 defect, so the same
    behaviour is now proved on an account operation; the payment case is
    refused by test_a_payment_cannot_be_reconciled_by_the_submission_classifier.
    """
    from datetime import UTC, datetime, timedelta

    from robothor.autonomy import confirmation, handoff_worker
    from robothor.autonomy.handoff_recovery import HandoffChecks
    from robothor.autonomy.models import Delegation, WebOperation

    original_wait = confirmation.wait_for_confirmation

    async def short_wait(page, destination, action, **kwargs):
        return await original_wait(page, destination, action, timeout_seconds=0.2)

    monkeypatch.setattr(confirmation, "wait_for_confirmation", short_wait)
    grant = store.create_grant(
        identity,
        Delegation(
            agent_ids={"main"},
            origins={"https://account.example"},
            actions={"account"},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    op = store.reserve(
        identity,
        grant["id"],
        "main",
        WebOperation(
            origin="https://account.example",
            action="account",
            purpose="Requested account",
            idempotency_key="automatic-handoff-account",
        ),
    )
    store.bind_plan(
        identity,
        op["id"],
        "main",
        {"url": "https://account.example/register", "submit_selector": "#submit"},
    )
    store.record_landed_pages(
        identity,
        op["id"],
        ["https://account.example/register", "https://account.example/status"],
    )
    store.begin_submit(identity, op["id"], "main")
    asked = HandoffStore(store).create(
        identity,
        op["id"],
        "main",
        request(confirmation={"url": "https://account.example/status"}),
    )
    posts = []
    async with async_playwright() as pw:
        for message, expected in [
            ("Your account is not created.", "reconciling"),
            ("Your account has been created.", "completed"),
        ]:
            HandoffStore(store).acknowledge(identity, asked["id"])

            async def run(scope, operation, agent, plan, *, reconcile, message=message):
                # Playwright may pass (route, request) to a two-argument handler.
                # Keep the merchant handler unary; bind scenario text here.
                async def merchant(route):
                    if route.request.method == "POST":
                        posts.append("unexpected")
                    await route.fulfill(
                        content_type="text/html",
                        body="<p>"
                        + message
                        + '</p><script>fetch("/submit",{method:"POST"}).catch(()=>{});</script>',
                    )

                assert reconcile and plan.success_selector is None and plan.success_text is None
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.route("**/*", merchant)
                    return await BrowserBroker(store).reconcile_on_page(
                        scope, operation, agent, plan, page
                    )
                finally:
                    await browser.close()

            monkeypatch.setattr(handoff_worker, "run_browser", run)
            await handoff_worker.check_one(HandoffChecks(store), identity, asked["id"])
            assert store.operation(identity, op["id"])["state"] == expected
        assert posts == []
        assert HandoffStore(store).list(identity)[0]["state"] == "resolved"
