"""Actual Chromium + PostgreSQL through a controlled merchant application."""

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.broker import BrowserBroker, Challenge, ExecutionPlan, FieldBinding
from robothor.autonomy.models import Delegation, ResourceInput, WebOperation

SIGNUP = """<!doctype html><title>Application</title><form id="application">
<label>Email<input id="email" type="email" required></label>
<label>Password<input id="password" type="password" required></label>
<label>Photo<input id="photo" type="file" required></label>
<label><input id="terms" type="checkbox" required>Accept terms</label>
<button id="submit">Create account</button></form><div id="confirmation" hidden></div>
<script>document.querySelector('form').onsubmit=async e=>{
e.preventDefault();
await fetch('/submit',{method:'POST',body:JSON.stringify({
 email:document.querySelector('#email').value,password:document.querySelector('#password').value,
 uploaded:document.querySelector('#photo').files[0].name})});
document.cookie='signed_in=yes; Secure; SameSite=Strict';
document.querySelector('#confirmation').hidden=false;
document.querySelector('#confirmation').textContent='Application received: ORDER-123';
};</script>"""


@pytest.mark.timeout(60)
async def test_account_upload_confirmation_and_session_survive_browser_restart(store, identity):
    credential = store.put_resource(
        identity,
        ResourceInput(
            kind="credential",
            label="Website login",
            origin="https://shop.example",
            payload=json.dumps(
                {"username": "alice@example.com", "password": "private-fixture-password"}
            ),
        ),
    )
    photo = store.put_resource(
        identity,
        ResourceInput(
            kind="document",
            label="Profile picture",
            payload=json.dumps(
                {
                    "name": "portrait.png",
                    "mime_type": "image/png",
                    "base64": base64.b64encode(b"fixture photo").decode(),
                }
            ),
        ),
    )
    grant = store.create_grant(
        identity,
        Delegation(
            agent_ids={"main"},
            origins={"https://shop.example"},
            actions={"account"},
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    op = store.reserve(
        identity,
        grant["id"],
        "main",
        WebOperation(
            origin="https://shop.example",
            action="account",
            purpose="Requested application",
            idempotency_key="signup-1",
        ),
    )
    plan = ExecutionPlan(
        url="https://shop.example/signup",
        submit_selector="#submit",
        success_selector="#confirmation",
        success_text="Application received",
        check_selectors=["#terms"],
        fields=[
            FieldBinding(
                selector="#email", resource_id=credential["id"], kind="credential", field="username"
            ),
            FieldBinding(
                selector="#password",
                resource_id=credential["id"],
                kind="credential",
                field="password",
            ),
            FieldBinding(
                selector="#photo",
                resource_id=photo["id"],
                kind="document",
                field="file",
                method="upload",
            ),
        ],
    )
    submissions = []

    async def merchant(route):
        if route.request.url.endswith("/submit"):
            submissions.append(json.loads(route.request.post_data))
            await route.fulfill(body="ok")
        else:
            await route.fulfill(body=SIGNUP, content_type="text/html")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.route("https://shop.example/**", merchant)
        page = await context.new_page()
        result = await BrowserBroker(store).execute_on_page(identity, op["id"], "main", plan, page)
        assert result["state"] == "completed", result
        assert submissions == [
            {
                "email": "alice@example.com",
                "password": "private-fixture-password",
                "uploaded": "portrait.png",
            }
        ]
        assert "private-fixture-password" not in json.dumps(result)
        state = store.consume_resource(
            identity, result["session_resource_id"], "https://shop.example", kind="browser_session"
        )
        await browser.close()

        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=state)
        assert any(cookie["name"] == "signed_in" for cookie in await context.cookies())
        again = await BrowserBroker(store).execute_on_page(
            identity, op["id"], "main", plan, await context.new_page()
        )
        assert again["state"] == "completed"
        assert len(submissions) == 1
        await browser.close()


@pytest.mark.timeout(60)
async def test_personal_checkout_resumes_with_transient_code_and_one_charge(store, identity):
    card = store.put_resource(
        identity,
        ResourceInput(
            kind="payment_card",
            label="Personal card",
            payload=json.dumps(
                {
                    "number": "4242424242424242",
                    "name": "Alice Example",
                    "expiry_month": 12,
                    "expiry_year": 2030,
                }
            ),
        ),
    )
    grant = store.create_grant(
        identity,
        Delegation(
            agent_ids={"main"},
            origins={"https://shop.example"},
            actions={"purchase"},
            expires_at=datetime.now(UTC) + timedelta(days=1),
            per_purchase_minor=1000,
            monthly_minor=1000,
        ),
    )
    operation = store.reserve(
        identity,
        grant["id"],
        "main",
        WebOperation(
            origin="https://shop.example",
            action="purchase",
            purpose="Requested purchase",
            amount_minor=500,
            idempotency_key="checkout-1",
        ),
    )
    plan = ExecutionPlan(
        url="https://shop.example/checkout",
        submit_selector="#submit",
        success_selector="#confirmation",
        success_text="Order received",
        amount_selector="#total",
        challenge=Challenge(selector="#code", kind="card_code"),
        fields=[
            FieldBinding(
                selector="#number", resource_id=card["id"], kind="payment_card", field="number"
            )
        ],
    )
    checkout = """<div id='total'>$5.00 USD</div><form><input id='number' required><input id='code' required>
    <button id='submit'>Pay</button></form><div id='confirmation' hidden></div>
    <script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();await fetch('/charge',{method:'POST',
    body:JSON.stringify({number:document.querySelector('#number').value,code:document.querySelector('#code').value})});
    document.querySelector('#confirmation').hidden=false;document.querySelector('#confirmation').textContent='Order received';};</script>"""
    charges = []

    async def merchant(route):
        if route.request.url.endswith("/charge"):
            charges.append(json.loads(route.request.post_data))
            await route.fulfill(body="ok")
        else:
            await route.fulfill(body=checkout, content_type="text/html")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.route("https://shop.example/**", merchant)
        page = await context.new_page()
        broker = BrowserBroker(store)
        result = await broker.execute_on_page(identity, operation["id"], "main", plan, page)
        assert result["state"] == "awaiting_input"
        assert charges == []
        store.resume_with_code(identity, operation["id"])
        result = await broker.execute_on_page(
            identity, operation["id"], "main", plan, page, verification_code="739"
        )
        assert result["state"] == "completed"
        assert charges == [{"number": "4242424242424242", "code": "739"}]
        assert "739" not in json.dumps(store.operation(identity, operation["id"]))
        assert "739" not in json.dumps(result)
        assert "739" not in str(
            store.consume_resource(identity, card["id"], "https://shop.example")
        )
        await broker.execute_on_page(identity, operation["id"], "main", plan, page)
        assert len(charges) == 1
        await browser.close()
