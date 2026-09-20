"""Device handoff rechecks survive a new browser and never click checkout again."""

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.broker import BrowserBroker, ExecutionPlan
from robothor.autonomy.handoffs import HandoffStore
from robothor.autonomy.tests.test_handoffs import pending, request

pytestmark = pytest.mark.e2e


async def test_external_approval_reconciles_with_no_second_post_or_script_mutation(store, identity):
    op = pending(store, identity)
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
                body="""<p id="amount">$6.00</p><button id="submit" onclick="fetch('/pay',{method:'POST'}).then(r=>r.text()).then(t=>document.querySelector('#done').textContent=t)">Buy</button><p id="done"></p>""",
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
            assert result["state"] == "reconciling" and len(submitted) == 1
        finally:
            await browser.close()
        handoff = HandoffStore(store).create(identity, op["id"], "main", request())
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
                assert len(submitted) == 1
            finally:
                await browser.close()
        assert len(status_reads) == 2
        assert HandoffStore(store).list(identity)[0]["state"] == "resolved"


async def test_automatic_handoff_confirmation_uses_affirmative_observation_not_guess(
    store, identity, monkeypatch
):
    from robothor.autonomy import confirmation, handoff_worker
    from robothor.autonomy.handoff_recovery import HandoffChecks

    original_wait = confirmation.wait_for_confirmation

    async def short_wait(page, destination, action, **kwargs):
        return await original_wait(page, destination, action, timeout_seconds=0.2)

    monkeypatch.setattr(confirmation, "wait_for_confirmation", short_wait)
    op = pending(store, identity)
    asked = HandoffStore(store).create(
        identity, op["id"], "main", request(confirmation={"url": "https://shop.example/status"})
    )
    posts = []
    async with async_playwright() as pw:
        for message, expected in [
            ("Your order is not confirmed.", "reconciling"),
            ("Order confirmed.", "completed"),
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
