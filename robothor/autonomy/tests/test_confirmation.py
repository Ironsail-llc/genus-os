"""Discover new affirmative confirmations without guessing merchant selectors."""

from datetime import UTC, datetime, timedelta

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.broker import BrowserBroker, ExecutionPlan
from robothor.autonomy.confirmation import classify, observe
from robothor.autonomy.models import Delegation, WebOperation


@pytest.mark.parametrize(
    "action,text,expected",
    [
        ("account", "Your account has been created!", "account_created"),
        ("application", "Application submitted successfully.", "application_received"),
        ("purchase", "Your order is confirmed", "order_confirmed"),
        ("subscription", "Your membership is now active.", "membership_active"),
        ("login", "You have successfully signed in.", "login_confirmed"),
        ("account", "Welcome back", None),
        ("account", "Your account has not been created", None),
        ("application", "Your application will be submitted", None),
        ("purchase", "Order confirmation pending", None),
        ("subscription", "Membership activation failed", None),
        ("purchase", "Account created", None),
        ("purchase", "Order confirmed, but payment failed", None),
    ],
)
def test_confirmation_requires_an_affirmative_message_for_the_requested_action(
    action, text, expected
):
    assert classify(text, action) == expected


@pytest.mark.timeout(60)
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.e2e
async def test_account_confirmation_discovered_after_click_without_known_selector(
    store, identity, existing
):
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
            purpose="Requested account",
            idempotency_key="discover-confirmation",
        ),
    )
    plan = ExecutionPlan(url="https://shop.example/join", submit_selector="#submit")
    html = f"""<button id="submit" onclick="fetch('/submit',{{method:'POST'}}).then(()=>{{
        const message=document.createElement('p');message.textContent='Your account has been created!';document.body.append(message);}})">Join</button>
        {"<p>Your account has been created!</p>" if existing else ""}"""
    requests = []

    async def respond(route):
        requests.append(route.request.url)
        await route.fulfill(
            content_type="text/html", body=html if route.request.url.endswith("/join") else "ok"
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.context.route("**/*", respond)
        result = await BrowserBroker(store).execute_on_page(identity, op["id"], "main", plan, page)
        if existing:
            assert result["state"] == "reserved", result
            assert result["reason"] == "confirmation_already_present"
            assert requests == ["https://shop.example/join"]
        else:
            assert result["state"] == "completed", result
            assert result["evidence"]["confirmation_rule"] == "account_created"
            assert result["evidence"]["confirmation_sha256"]
            assert requests.count("https://shop.example/submit") == 1
        await browser.close()


@pytest.mark.e2e
async def test_hidden_editable_and_unrelated_confirmation_text_is_not_evidence():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route(
            "**/*",
            lambda route: route.fulfill(
                content_type="text/html",
                body="""
            <div hidden>Account created</div><textarea>Account created</textarea>
            <div><span contenteditable>Account created</span></div><button>Account created</button>
            <p>Order confirmed</p><p>Account not created</p>""",
            ),
        )
        await page.goto("https://shop.example/join")
        assert await observe(page, "https://shop.example", "account") == {}
        await browser.close()


@pytest.mark.timeout(60)
@pytest.mark.parametrize("mode", ["before_click", "pending_after_click"])
@pytest.mark.e2e
async def test_discovery_does_not_repeat_an_uncertain_operation(store, identity, monkeypatch, mode):
    from robothor.autonomy import confirmation

    original_wait = confirmation.wait_for_confirmation

    async def immediate(page, destination, action):
        return await original_wait(page, destination, action, timeout_seconds=0)

    monkeypatch.setattr(confirmation, "wait_for_confirmation", immediate)
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
            purpose="Requested account",
            idempotency_key="uncertain-discovery",
        ),
    )
    plan = ExecutionPlan(
        url="https://shop.example/join", submit_selector="#submit", check_selectors=["#terms"]
    )
    status = (
        "Your account has been created!" if mode == "before_click" else "Account creation pending"
    )
    html = f"""<input id="terms" type="checkbox" onchange="document.querySelector('p').textContent='{status}'">
        <button id="submit" onclick="fetch('/submit',{{method:'POST'}})">Join</button><p></p>"""
    requests = []

    async def respond(route):
        requests.append(route.request.url)
        await route.fulfill(
            content_type="text/html", body=html if route.request.url.endswith("/join") else "ok"
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.context.route("**/*", respond)
        broker = BrowserBroker(store)
        result = await broker.execute_on_page(identity, op["id"], "main", plan, page)
        assert result["state"] == "reconciling", result
        assert requests.count("https://shop.example/submit") == (0 if mode == "before_click" else 1)
        prior = list(requests)
        again = await broker.execute_on_page(identity, op["id"], "main", plan, page)
        assert again["state"] == "reconciling"
        assert requests == prior
        assert store.operation(identity, op["id"])["state"] == "reconciling"
        await browser.close()


@pytest.mark.parametrize(
    "partial", [{"success_selector": "#done"}, {"success_text": "Account created"}]
)
def test_explicit_confirmation_requires_both_selector_and_text(partial):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="confirmation_selector_and_text_required_together"):
        ExecutionPlan(url="https://shop.example/join", submit_selector="#submit", **partial)
