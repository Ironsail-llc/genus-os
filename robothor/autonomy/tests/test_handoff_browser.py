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
