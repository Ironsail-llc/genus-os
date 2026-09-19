"""Completed operation plans are reusable hints, never renewed authority."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from robothor.autonomy.broker import ExecutionPlan, FieldBinding
from robothor.autonomy.models import Delegation, ResourceInput, WebOperation
from robothor.autonomy.procedures import ProcedureQuery
from robothor.autonomy.store import AutonomyStore


@pytest.mark.timeout(60)
async def test_reused_checkout_template_still_checks_price_and_revocation(store, identity):
    from playwright.async_api import async_playwright

    from robothor.autonomy.broker import BrowserBroker

    grant = store.create_grant(
        identity,
        Delegation(
            agent_ids={"main"},
            origins={"https://shop.example"},
            actions={"purchase"},
            per_purchase_minor=1000,
            monthly_minor=1000,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    proposal = WebOperation(
        origin="https://shop.example",
        action="purchase",
        purpose="Requested purchase",
        amount_minor=500,
        idempotency_key="first-purchase",
    )
    first = store.reserve(identity, grant["id"], "main", proposal)
    plan = ExecutionPlan(
        url="https://shop.example/buy", submit_selector="#buy", amount_selector="#total"
    )
    price = "5.00"
    charges = []

    async def respond(route):
        if route.request.url == "https://shop.example/charge":
            charges.append(route.request.url)
            await route.fulfill(body="ok")
        else:
            await route.fulfill(
                content_type="text/html",
                body=f"""<div id="total">${price} USD</div>
            <button id="buy" onclick="fetch('/charge',{{method:'POST'}}).then(()=>{{document.querySelector('p').textContent='Order confirmed';}})">Buy</button><p></p>""",
            )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.context.route("**/*", respond)
        result = await BrowserBroker(store).execute_on_page(
            identity, first["id"], "main", plan, page
        )
        assert result["state"] == "completed", result
        procedure = store.procedures(
            identity, "main", ProcedureQuery(origin=proposal.origin, action=proposal.action)
        )[0]
        reused = ExecutionPlan.model_validate(procedure["plan_template"])
        second = store.reserve(
            identity,
            grant["id"],
            "main",
            proposal.model_copy(update={"idempotency_key": "second-purchase"}),
        )
        price = "6.00"
        result = await BrowserBroker(store).execute_on_page(
            identity, second["id"], "main", reused, page
        )
        assert result["state"] == "reserved", result
        assert len(charges) == 1
        price = "5.00"
        store.revoke_grant(identity, grant["id"])
        result = await BrowserBroker(store).execute_on_page(
            identity, second["id"], "main", reused, page
        )
        assert result["state"] == "reserved", result
        assert len(charges) == 1
        await browser.close()


async def test_procedure_tool_uses_verified_actor_and_current_agent(store, identity, monkeypatch):
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import autonomy

    completed(store, identity)
    monkeypatch.setattr(autonomy, "AutonomyStore", lambda: store)
    monkeypatch.setattr(
        autonomy,
        "scope_for_actor",
        lambda tenant, actor: identity.model_copy(update={"owner_id": actor, "tenant_id": tenant}),
    )
    args = {
        "kind": "procedures",
        "origin": "https://shop.example",
        "action": "account",
        "agent_id": "main",
    }
    result = await autonomy.handle(
        args, ToolContext(tenant_id=identity.tenant_id, user_id="alice", agent_id="main")
    )
    assert len(result["procedures"]) == 1
    result = await autonomy.handle(
        args, ToolContext(tenant_id=identity.tenant_id, user_id="alice", agent_id="other")
    )
    assert result == {"procedures": []}
    result = await autonomy.handle(
        args, ToolContext(tenant_id=identity.tenant_id, user_id="bob", agent_id="main")
    )
    assert result == {"procedures": []}


def completed(store, scope, *, key="original", url="https://shop.example/join"):
    resource = store.put_resource(
        scope,
        ResourceInput(
            kind="credential",
            label="Website login",
            origin="https://shop.example",
            payload=json.dumps({"username": "alice", "password": "private-password"}),
        ),
    )
    grant = store.create_grant(
        scope,
        Delegation(
            agent_ids={"main"},
            origins={"https://shop.example"},
            actions={"account"},
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    proposal = WebOperation(
        origin="https://shop.example",
        action="account",
        purpose="Requested account",
        idempotency_key=key,
    )
    op = store.reserve(scope, grant["id"], "main", proposal)
    plan = ExecutionPlan(
        url=url,
        submit_selector="#submit",
        success_selector="#done",
        success_text="Account created",
        fields=[
            FieldBinding(
                selector="#password",
                resource_id=resource["id"],
                kind="credential",
                field="password",
            )
        ],
    )
    store.bind_plan(scope, op["id"], "main", plan.model_dump(mode="json"))
    store.begin_submit(scope, op["id"], "main")
    store.finish(
        scope,
        op["id"],
        "completed",
        {
            "origin": proposal.origin,
            "kind": "merchant_confirmation",
            "confirmation_sha256": "a" * 64,
            "verified_at": datetime.now(UTC).isoformat(),
        },
    )
    return op, grant, resource


def test_completed_procedure_survives_restart_and_is_owner_agent_origin_scoped(store, identity):
    op, grant, resource = completed(store, identity)
    restarted = AutonomyStore(store._connect, keys={"v1": b"x" * 32}, key_id="v1")
    query = ProcedureQuery(origin="https://shop.example", action="account")
    results = restarted.procedures(identity, "main", query)
    assert len(results) == 1
    assert results[0]["source_operation_id"] == op["id"]
    assert results[0]["plan_template"]["fields"][0]["resource_id"] == resource["id"]
    assert "private-password" not in json.dumps(results)
    assert (
        restarted.procedures(identity.model_copy(update={"owner_id": "bob"}), "main", query) == []
    )
    assert (
        restarted.procedures(identity.model_copy(update={"tenant_id": "other"}), "main", query)
        == []
    )
    assert restarted.procedures(identity, "other-agent", query) == []
    assert (
        restarted.procedures(
            identity, "main", query.model_copy(update={"origin": "https://other.example"})
        )
        == []
    )
    assert (
        restarted.procedures(identity, "main", query.model_copy(update={"action": "login"})) == []
    )
    store.revoke_grant(identity, grant["id"])
    assert restarted.procedures(identity, "main", query)
    with pytest.raises(PermissionError):
        restarted.reserve(
            identity,
            grant["id"],
            "main",
            WebOperation(
                origin=query.origin,
                action=query.action,
                purpose="New request",
                idempotency_key="new-request",
            ),
        )


def test_procedures_strip_one_off_url_state_and_do_not_reuse_old_receipt_text(store, identity):
    op, _, _ = completed(
        store, identity, url="https://shop.example/join?token=private-one-off#private-fragment"
    )
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_operations SET execution_plan=jsonb_set(execution_plan,'{success_text}',to_jsonb(%s::text)) WHERE id=%s",
            ("Account created: ORIGINAL-123", op["id"]),
        )
    result = store.procedures(
        identity, "main", ProcedureQuery(origin="https://shop.example", action="account")
    )[0]
    assert result["plan_template"]["url"] == "https://shop.example/join"
    assert result["plan_template"]["session_resource_id"] is None
    assert result["plan_template"]["success_selector"] is None
    assert result["plan_template"]["success_text"] is None
    assert "private-one-off" not in json.dumps(result)
    assert "private-fragment" not in json.dumps(result)
    assert "ORIGINAL-123" not in json.dumps(result)
    assert result["inspect_before_execution"] is True


def test_stale_or_revoked_resource_procedures_are_not_offered(store, identity):
    first, _, resource = completed(store, identity, key="first-request")
    second, _, _ = completed(store, identity, key="second", url="https://shop.example/other-form")
    store.revoke_resource(identity, resource["id"])
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_operations SET updated_at=now()-interval '91 days' WHERE id=%s",
            (second["id"],),
        )
    assert (
        store.procedures(
            identity, "main", ProcedureQuery(origin="https://shop.example", action="account")
        )
        == []
    )
