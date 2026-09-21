"""Constraint failures are recoverable before the merchant receives protected input."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.broker import BrowserBroker, ExecutionPlan, FieldBinding
from robothor.autonomy.models import Delegation, ResourceInput, WebOperation

pytestmark = pytest.mark.e2e


@pytest.mark.timeout(60)
async def test_invalid_plan_can_be_corrected_without_filling_or_duplicate_submission(
    store, identity
):
    refs = [
        store.put_resource(
            identity,
            ResourceInput(
                kind="credential",
                label="Website login",
                origin="https://shop.example",
                payload=json.dumps({"username": value, "password": "private-password"}),
            ),
        )
        for value in ("invalid-email", "alice@example.com")
    ]
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
            idempotency_key="recover-before-fill",
        ),
    )
    html = """<form><input id="email" type="email" required oninput="fetch('/input')">
    <input id="password" type="password" minlength="12" required>
    <input id="terms" type="checkbox" required><button id="submit">Join</button></form>
    <div id="confirmation" hidden>Account created</div><script>
    document.querySelector('form').onsubmit=async e=>{e.preventDefault();
    await fetch('/submit',{method:'POST'});document.querySelector('#confirmation').hidden=false;};</script>"""
    requests = []

    async def respond(route):
        requests.append(route.request.url)
        await route.fulfill(
            body=html if route.request.url.endswith("/join") else "ok", content_type="text/html"
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.context.route("**/*", respond)
        plan = ExecutionPlan(
            url="https://shop.example/join",
            submit_selector="#submit",
            success_selector="#confirmation",
            success_text="Account created",
            fields=[
                FieldBinding(
                    selector="#email",
                    resource_id=refs[0]["id"],
                    kind="credential",
                    field="username",
                ),
                FieldBinding(
                    selector="#password",
                    resource_id=refs[0]["id"],
                    kind="credential",
                    field="password",
                ),
            ],
        )
        result = await BrowserBroker(store).execute_on_page(identity, op["id"], "main", plan, page)
        assert result["state"] == "reserved", result
        assert result["reason"] == "validation_required"
        assert any(
            f["selector"] == "#email" and "typeMismatch" in f["issues"] for f in result["fields"]
        )
        assert any(
            f["selector"] == "#terms" and "valueMissing" in f["issues"] for f in result["fields"]
        )
        assert requests == ["https://shop.example/join"]
        assert await page.locator("#email").input_value() == ""
        assert store.operation(identity, op["id"])["execution_plan"] is None
        assert "invalid-email" not in json.dumps(result)
        assert "private-password" not in json.dumps(result)
        corrected = plan.model_copy(
            update={
                "check_selectors": ["#terms"],
                "fields": [
                    field.model_copy(update={"resource_id": UUID(refs[1]["id"])})
                    for field in plan.fields
                ],
            }
        )
        result = await BrowserBroker(store).execute_on_page(
            identity, op["id"], "main", corrected, page
        )
        assert result["state"] == "completed", result
        assert requests.count("https://shop.example/submit") == 1
        await browser.close()


@pytest.mark.timeout(60)
async def test_offline_validation_ignores_merchant_setters_and_preserves_frame_binding():
    from unittest.mock import MagicMock

    from robothor.autonomy.models import Scope
    from robothor.autonomy.preflight import validate_plan

    password = "private-fixture-password"
    store = MagicMock()
    store.consume_resource.return_value = {"password": password}
    proposal = WebOperation(
        origin="https://shop.example",
        action="account",
        purpose="Requested account",
        idempotency_key="offline-frame",
    )
    plan = ExecutionPlan(
        url="https://shop.example/join",
        submit_selector="#submit",
        success_selector="#done",
        success_text="Account created",
        fields=[
            FieldBinding(
                selector="#password",
                resource_id="11111111-1111-1111-1111-111111111111",
                kind="credential",
                field="password",
                frame_selector="#provider",
                frame_origin="https://identity.example",
            )
        ],
    )
    html = """<form><input id="password" type="password" pattern="[0-9]+" minlength="50" required></form>
    <script>const d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
    Object.defineProperty(HTMLInputElement.prototype,'value',{...d,set(v){fetch('/leaked',{method:'POST',body:v});d.set.call(this,v);}});</script>"""
    observed = []

    async def respond(route):
        observed.append(route.request.url)
        await route.fulfill(
            content_type="text/html",
            body=(
                '<iframe id="provider" src="https://identity.example/form"></iframe><button id="submit">Join</button>'
                if route.request.url == "https://shop.example/join"
                else html
            ),
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.context.route("**/*", respond)
        await page.goto(plan.url)
        issues = await validate_plan(
            BrowserBroker(store),
            Scope(tenant_id="test", owner_id="alice"),
            page,
            proposal,
            plan,
            frozenset({"https://identity.example"}),
        )
        assert issues == [
            {
                "selector": "#password",
                "frame_selector": "#provider",
                "frame_origin": "https://identity.example",
                "issues": ["patternMismatch", "tooShort"],
            }
        ]
        assert password not in json.dumps(issues)
        assert not any(url.endswith("/leaked") for url in observed)
        assert store.consume_resource.call_args.args[2] == "https://identity.example"
        assert len(browser.contexts) == 1
        await browser.close()


@pytest.mark.parametrize(
    "attribute,issue", [("readonly", "fieldReadOnly"), ("disabled", "fieldDisabled")]
)
async def test_unfillable_binding_is_rejected_before_input(attribute, issue):
    from unittest.mock import MagicMock

    from robothor.autonomy.models import Scope
    from robothor.autonomy.preflight import validate_plan

    store = MagicMock()
    store.consume_resource.return_value = {"password": "private-password"}
    proposal = WebOperation(
        origin="https://shop.example",
        action="account",
        purpose="Requested account",
        idempotency_key="unfillable",
    )
    plan = ExecutionPlan(
        url="https://shop.example/join",
        submit_selector="#submit",
        success_selector="#done",
        success_text="Account created",
        fields=[
            FieldBinding(
                selector="#password",
                resource_id="11111111-1111-1111-1111-111111111111",
                kind="credential",
                field="password",
            )
        ],
    )
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route(
            "**/*",
            lambda route: route.fulfill(
                content_type="text/html",
                body=f'<form><input id="password" {attribute}><button id="submit">Join</button></form>',
            ),
        )
        await page.goto(plan.url)
        invalid = await validate_plan(
            BrowserBroker(store),
            Scope(tenant_id="test", owner_id="alice"),
            page,
            proposal,
            plan,
            frozenset(),
        )
        assert invalid == [{"selector": "#password", "issues": [issue]}]
        await browser.close()
