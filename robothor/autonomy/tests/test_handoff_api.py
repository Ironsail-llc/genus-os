"""Only the authenticated person can request checking an external handoff."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from crm.bridge.routers import autonomy
from robothor.autonomy.handoffs import HandoffStore
from robothor.autonomy.tests.test_handoffs import pending, request


def test_owner_check_uses_stored_read_only_confirmation_without_returning_it(
    store, identity, monkeypatch
):
    op = pending(store, identity)
    asked = HandoffStore(store).create(identity, op["id"], "main", request())
    monkeypatch.setattr(autonomy, "AutonomyStore", lambda: store)
    monkeypatch.setattr(autonomy, "scope_for_actor", lambda *args: identity)
    browser = AsyncMock(return_value={"state": "reconciling"})
    from robothor.autonomy import handoff_worker

    monkeypatch.setattr(handoff_worker, "run_browser", browser)
    auth = SimpleNamespace(
        tenant_id=identity.tenant_id, actor_id=identity.owner_id, role="owner", is_service=False
    )
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(req: Request, call_next):
        req.state.auth = auth
        return await call_next(req)

    app.include_router(autonomy.router)
    with TestClient(app) as client:
        response = client.post(f"/api/autonomy/handoffs/{asked['id']}/check")
        assert response.status_code == 200 and response.json()["state"] == "checking"
        assert response.headers["cache-control"] == "no-store"
        assert "PrivateLinkCanary" not in response.text

        # Drain the real scheduled task on the server's event loop.
        async def drain():
            if autonomy._resumes:
                await asyncio.gather(*tuple(autonomy._resumes))

        client.portal.call(drain)
        assert browser.await_count == 1
        assert HandoffStore(store).list(identity)[0]["state"] == "awaiting_external_action"
        assert browser.call_args.kwargs == {"reconcile": True}
        plan = browser.call_args.args[3]
        assert plan.fields == [] and plan.check_selectors == []
        assert plan.success_selector == "#done"
        assert store.operation(identity, op["id"])["state"] == "reconciling"
        auth.is_service = True
        assert client.post(f"/api/autonomy/handoffs/{asked['id']}/check").status_code == 403


async def test_agent_can_request_but_cannot_acknowledge_owner_handoff(store, identity, monkeypatch):
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import autonomy as handler

    op = pending(store, identity)
    monkeypatch.setattr(handler, "AutonomyStore", lambda: store)
    monkeypatch.setattr(handler, "scope_for_actor", lambda *args: identity)
    ctx = ToolContext(agent_id="main", tenant_id=identity.tenant_id, user_id="owner")
    result = await handler.handle(
        {"kind": "handoff", "operation_id": op["id"], "handoff": request().model_dump(mode="json")},
        ctx,
    )
    assert result["state"] == "awaiting_external_action"
    assert "PrivateLinkCanary" not in str(result)
    listed = await handler.handle({"kind": "handoffs", "operation_id": op["id"]}, ctx)
    assert listed["handoffs"][0]["id"] == result["id"]
    assert (await handler.handle({"kind": "handoff_check", "operation_id": op["id"]}, ctx))[
        "error"
    ] == "unknown_autonomy_action"
    denied = await handler.handle(
        {"kind": "handoffs", "operation_id": op["id"]},
        ToolContext(agent_id="other", tenant_id=identity.tenant_id, user_id="owner"),
    )
    assert denied["error"] == "agent_not_allowed"
