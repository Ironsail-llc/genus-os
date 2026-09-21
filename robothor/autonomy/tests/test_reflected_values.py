"""Entered secrets stay private even when a merchant echoes them into metadata."""

import json
from uuid import uuid4

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.broker import ExecutionPlan, FieldBinding
from robothor.autonomy.models import ResourceInput
from robothor.autonomy.tests.test_workflow_store import prepared
from robothor.autonomy.workflows.manager import WorkflowManager


@pytest.mark.timeout(40)
@pytest.mark.parametrize("prefix", ["", "x" * 95])
async def test_reflected_credential_not_returned_or_journaled_after_step(store, identity, prefix):
    secret = "VaultCanaryQ7x9!"
    operation = prepared(store, identity)
    credential = store.put_resource(
        identity,
        ResourceInput(
            kind="credential",
            label="Website login",
            origin="https://form.example",
            payload=json.dumps({"username": "alice", "password": secret}),
        ),
    )
    html = """<form id="first"><input id="password" type="password"><button id="next">Next</button></form>
<form id="second" hidden><label id="label"><input id="occupation"></label><select id="choices"><option></option></select><button>Apply</button></form>
<script>document.querySelector('#first').onsubmit=e=>{e.preventDefault();let value=document.querySelector('#password').value;document.querySelector('#first').hidden=true;document.querySelector('#second').hidden=false;document.querySelector('#label').append(value);document.querySelector('#occupation').id=value;document.querySelector('#choices').name=value;document.querySelector('option').textContent=value;};</script>"""
    html = html.replace(".append(value)", ".append(" + json.dumps(prefix) + "+value)")

    async def respond(route):
        await route.fulfill(content_type="text/html", body=html)

    async with async_playwright() as pw:

        async def factory():
            return await pw.chromium.launch(headless=True)

        manager = WorkflowManager(store, factory, request_router=respond)
        try:
            opened = await manager.open(
                identity, "main", operation["id"], "https://form.example/apply"
            )
            wid = opened["workflow_id"]
            plan = ExecutionPlan(
                url="https://form.example/apply",
                submit_selector="#next",
                fields=[
                    FieldBinding(
                        selector="#password",
                        resource_id=credential["id"],
                        kind="credential",
                        field="password",
                    )
                ],
            )
            result = await manager.execute(
                identity, "main", wid, str(uuid4()), 0, plan, advance=True
            )
            assert result["reason"] == "workflow_step_completed", result
            inspected = await manager.inspect(identity, "main", wid)
            assert secret not in json.dumps(result)
            assert secret not in json.dumps(inspected)
            assert "Vault" not in json.dumps(result), "mask before shortening labels"
            assert "VaultCanaryQ7x9" not in json.dumps(result), (
                "CSS escaping must not reveal the secret either"
            )
            with store.transaction() as cur:
                cur.execute(
                    "SELECT request,result FROM autonomy_workflow_commands WHERE workflow_id=%s",
                    (wid,),
                )
                assert secret not in json.dumps([dict(row) for row in cur.fetchall()])
            page = manager._live[wid].page
            for field in inspected["fields"]:
                assert await page.locator(field["selector"]).count() == 1
        finally:
            await manager.shutdown()


async def test_card_code_requires_a_payment_operation_before_browser_entry(store, identity):
    from unittest.mock import AsyncMock, MagicMock

    from robothor.autonomy.broker import BrowserBroker

    operation = prepared(store, identity)
    page = AsyncMock()
    # Playwright's event registration is synchronous; an AsyncMock would hand
    # back un-awaited coroutines and hide a real call behind a warning.
    page.on = MagicMock()
    page.remove_listener = MagicMock()
    plan = ExecutionPlan.model_validate(
        {
            "url": "https://form.example/apply",
            "submit_selector": "#submit",
            "challenge": {"selector": "#code", "kind": "card_code"},
        }
    )
    result = await BrowserBroker(store).execute_on_page(
        identity, operation["id"], "main", plan, page, verification_code="739"
    )
    assert result["state"] == "reserved"
    page.goto.assert_not_awaited()
    assert store.operation(identity, operation["id"])["execution_plan"] is None


async def test_restored_http_only_session_value_is_masked_on_initial_inspection(store, identity):
    secret = "SessionCanaryP3a8"
    operation = prepared(store, identity)
    session = store.put_resource(
        identity,
        ResourceInput(
            kind="browser_session",
            label="Session",
            origin="https://form.example",
            payload=json.dumps(
                {
                    "cookies": [
                        {
                            "name": "session",
                            "value": secret,
                            "domain": "form.example",
                            "path": "/",
                            "httpOnly": True,
                            "secure": True,
                            "sameSite": "Strict",
                            "expires": -1,
                        }
                    ],
                    "origins": [],
                }
            ),
        ),
    )

    async def respond(route):
        assert "session=" + secret in route.request.headers.get("cookie", "")
        await route.fulfill(
            content_type="text/html",
            body="<label>Session " + secret + '<input id="' + secret + '"></label>',
        )

    async with async_playwright() as pw:

        async def factory():
            return await pw.chromium.launch(headless=True)

        manager = WorkflowManager(store, factory, request_router=respond)
        try:
            result = await manager.open(
                identity,
                "main",
                operation["id"],
                "https://form.example/apply",
                session_resource_id=session["id"],
            )
            assert secret not in json.dumps(result)
            field = result["fields"][0]
            assert (
                await manager._live[result["workflow_id"]].page.locator(field["selector"]).count()
                == 1
            )
        finally:
            await manager.shutdown()


@pytest.mark.parametrize("rendering", ["plain", "transformed"])
async def test_verification_code_not_retained_as_confirmation_digest(store, identity, rendering):
    import hashlib

    operation = prepared(store, identity)
    code = "836214"

    async def respond(route):
        expression = (
            "document.querySelector('#code').value"
            if rendering == "plain"
            else "String(Number(document.querySelector('#code').value)+1)"
        )
        await route.fulfill(
            content_type="text/html",
            body="""<input id="code"><button id="submit" onclick="document.querySelector('#confirmation').hidden=false;document.querySelector('#confirmation').textContent='Application received: '+EXPRESSION;">Apply</button><p id="confirmation" hidden></p>""".replace(
                "EXPRESSION", expression
            ),
        )

    async with async_playwright() as pw:

        async def factory():
            return await pw.chromium.launch(headless=True)

        manager = WorkflowManager(store, factory, request_router=respond)
        try:
            result = await manager.open(
                identity, "main", operation["id"], "https://form.example/apply"
            )
            wid = result["workflow_id"]
            plan = ExecutionPlan.model_validate(
                {
                    "url": "https://form.example/apply",
                    "submit_selector": "#submit",
                    "success_selector": "#confirmation",
                    "success_text": "Application received",
                    "challenge": {"selector": "#code", "kind": "one_time_code"},
                }
            )
            waiting = await manager.execute(identity, "main", wid, str(uuid4()), 0, plan)
            assert waiting["state"] == "awaiting_input"
            store.resume_with_code(identity, operation["id"])
            broker = manager._live[wid].broker
            result = await manager.execute(
                identity, "main", wid, str(uuid4()), 0, plan, verification_code=code
            )
            assert result["state"] == "completed"
            assert (
                result["evidence"]["confirmation_sha256"]
                == hashlib.sha256(b"Application received").hexdigest()
            )
            assert not broker.protected_values
            with store.transaction() as cur:
                cur.execute(
                    "SELECT request,result FROM autonomy_workflow_commands WHERE workflow_id=%s",
                    (wid,),
                )
                records = json.dumps([dict(row) for row in cur.fetchall()])
            assert code not in records
            assert (
                hashlib.sha256(("Application received: " + code).encode()).hexdigest()
                not in records
            )
        finally:
            await manager.shutdown()
