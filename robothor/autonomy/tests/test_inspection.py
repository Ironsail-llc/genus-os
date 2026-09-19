"""Real DOM inspection gives usable selectors without returning form values."""

import json

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.inspection import inspect_page


@pytest.mark.timeout(60)
async def test_inspection_discovers_fields_terms_and_authorized_frames_without_values():
    html = """<form><label>Email<input name="email" type="email" value="private@example.com" required></label>
    <label>Password<input id="password" type="password" value="private-password" minlength="12"></label>
    <label>Country<select id="country"><option value="opaque-private-token">United States</option></select></label>
    <div><span>Annual cost</span><span id="annual">$72.00 USD</span></div>
    <div><span>Billing interval</span><span id="interval">Monthly</span></div>
    <div><span>Next charge</span><span id="next">October 31, 2026</span></div>
    <div hidden>$999.00 USD</div><input value="$888.00 USD">
    <div contenteditable="true">$777.00 USD</div>
    <div>CVV: 739</div><div>4242 4242 4242 4242</div>
    <button>Submit application</button></form>
    <iframe id="payment" src="https://payments.example/card"></iframe>
    <iframe id="other" src="https://other.example/card"></iframe>"""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        async def respond(route):
            await route.fulfill(
                content_type="text/html",
                body=html
                if route.request.url == "https://shop.example/join"
                else '<label>Card number<input name="card" value="4242424242424242"></label>',
            )

        await page.route("**/*", respond)
        await page.goto("https://shop.example/join")
        result = await inspect_page(
            page,
            destination="https://shop.example",
            allowed_frames=frozenset({"https://payments.example"}),
        )
        encoded = json.dumps(result)
        for private in (
            "private@example.com",
            "private-password",
            "opaque-private-token",
            "4242",
            "739",
            "999.00",
            "888.00",
            "777.00",
        ):
            assert private not in encoded
        field = next(f for f in result["fields"] if f.get("name") == "email")
        assert await page.locator(field["selector"]).count() == 1
        assert field["required"]
        assert next(f for f in result["fields"] if f.get("id") == "password")["min_length"] == 12
        assert next(f for f in result["fields"] if f.get("id") == "country")["options"] == [
            "United States"
        ]
        assert any(
            t["kind"] == "amount" and t["text"] == "$72.00 USD" and "annual" in t["labels"]
            for t in result["terms"]
        )
        assert any(t["kind"] == "date" and t["text"] == "October 31, 2026" for t in result["terms"])
        assert any(t["kind"] == "interval" and t["text"] == "Monthly" for t in result["terms"])
        frame_field = next(f for f in result["fields"] if f.get("name") == "card")
        assert frame_field["frame_origin"] == "https://payments.example"
        assert frame_field["frame_selector"] == "#payment"
        assert (
            await page.frame_locator(frame_field["frame_selector"])
            .locator(frame_field["selector"])
            .count()
            == 1
        )
        assert any(
            f["origin"] == "https://other.example" and f["state"] == "authority_required"
            for f in result["frames"]
        )
        assert len([f for f in result["fields"] if f.get("name") == "card"]) == 1
        await browser.close()


@pytest.mark.timeout(60)
async def test_protected_frame_cannot_navigate_to_another_origin_before_fill():
    from unittest.mock import MagicMock

    from playwright.async_api import Error

    from robothor.autonomy.broker import BrowserBroker

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        observed = []

        async def respond(route):
            observed.append(route.request.url)
            html = (
                '<iframe src="https://payments.example/card"></iframe>'
                if route.request.url == "https://shop.example/"
                else '<input id="number">'
            )
            await route.fulfill(content_type="text/html", body=html)

        await page.context.route("**/*", respond)
        await page.goto("https://shop.example/")
        frame = next(f for f in page.frames if f.url == "https://payments.example/card")
        await BrowserBroker(MagicMock())._guard_origin(page, frame, "https://payments.example")
        with pytest.raises(Error):
            await frame.goto("https://shop.example/credential-trap")
        assert "https://shop.example/credential-trap" not in observed
        await browser.close()


@pytest.mark.timeout(60)
async def test_discovered_provider_terms_can_be_used_to_validate_checkout():
    from unittest.mock import MagicMock

    from robothor.autonomy.broker import BrowserBroker, ExecutionPlan
    from robothor.autonomy.models import WebOperation

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        async def respond(route):
            html = (
                '<iframe id="provider" src="https://payments.example/checkout"></iframe>'
                if route.request.url == "https://shop.example/"
                else '<div>Total <span id="total">$6.00 USD</span></div>'
            )
            await route.fulfill(content_type="text/html", body=html)

        await page.context.route("**/*", respond)
        await page.goto("https://shop.example/")
        found = await inspect_page(
            page,
            destination="https://shop.example",
            allowed_frames=frozenset({"https://payments.example"}),
        )
        term = next(t for t in found["terms"] if t["kind"] == "amount")
        plan = ExecutionPlan(
            url="https://shop.example/",
            submit_selector="#submit",
            success_selector="#receipt",
            success_text="Order received",
            amount_selector=term["selector"],
            terms_frame_selector=term["frame_selector"],
            terms_frame_origin=term["frame_origin"],
        )
        proposal = WebOperation(
            origin="https://shop.example",
            action="purchase",
            purpose="Requested checkout",
            idempotency_key="framed-checkout",
            amount_minor=600,
        )
        broker = BrowserBroker(MagicMock())
        with pytest.raises(PermissionError, match="frame_not_authorized"):
            await broker._prices(page, proposal, plan)
        await broker._prices(
            page, proposal, plan, allowed_frames=frozenset({"https://payments.example"})
        )
        await browser.close()
