"""Readiness inherits the authenticated browser tool owner/agent boundary."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from robothor.autonomy.models import Scope
from robothor.autonomy.tests.test_store import proposal
from robothor.engine.tools.handlers import autonomy


@pytest.mark.asyncio
async def test_readiness_uses_verified_scope_without_reserving(monkeypatch):
    from robothor.autonomy import readiness

    scope = Scope(tenant_id="test", owner_id="owner")
    store = MagicMock()
    monkeypatch.setattr(autonomy, "scope_for_actor", lambda tenant, actor: scope)
    monkeypatch.setattr(autonomy, "AutonomyStore", lambda: store)
    check = MagicMock(return_value={"ready_to_prepare": True, "reservation_created": False})
    monkeypatch.setattr(readiness, "task_readiness", check)
    ctx = SimpleNamespace(tenant_id="test", user_id="verified", agent_id="main", is_benchmark=False)
    result = await autonomy.handle(
        {
            "kind": "readiness",
            "grant_id": str(uuid4()),
            "proposal": proposal().model_dump(mode="json"),
        },
        ctx,
    )
    assert result["ready_to_prepare"]
    assert check.call_args.args[:3] == (store, scope, "main")
    store.reserve.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_readiness_input_never_echoes_values(monkeypatch):
    monkeypatch.setattr(
        autonomy, "scope_for_actor", lambda *args: Scope(tenant_id="test", owner_id="owner")
    )
    ctx = SimpleNamespace(tenant_id="test", user_id="verified", agent_id="main", is_benchmark=False)
    result = await autonomy.handle({"kind": "readiness", "grant_id": "private-canary"}, ctx)
    assert result["error"] == "autonomy_request_failed"
    assert "private-canary" not in str(result)
    ctx.user_id = None
    assert (await autonomy.handle({"kind": "readiness"}, ctx))["error"] == "verified_owner_required"
