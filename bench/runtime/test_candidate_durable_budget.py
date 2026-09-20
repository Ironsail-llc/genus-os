"""Candidate provider calls with the host's private PostgreSQL goal ledger."""

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("deepagents")

from bench.runtime.budgeted_models import RequestBudget
from bench.runtime.candidates import FixtureGateway
from bench.runtime.goal_gateway import GoalGateway
from bench.runtime.test_candidate_boundaries import candidate
from robothor.goals import store
from robothor.goals.provider_ledger import DurableAttemptBudget
from robothor.goals.tests import test_store

private_database = test_store.private_database
db = test_store.db


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
async def test_candidate_usage_persists_and_finishes_with_goal(db, name):
    goal = test_store.create(db, token_budget=100)
    _, attempt = store.claim(db)
    ledger = DurableAttemptBudget(db, goal["id"], attempt)
    calls = []
    adapter = candidate(name, [("record", {"key": "report", "value": "delivered"})], calls)
    adapter = type(adapter)(adapter.model, request_budget=RequestBudget(ledger, 20))
    result = await adapter.run(GoalGateway(FixtureGateway(db), ledger), tenant=db)
    assert result["verified"] and len(calls) == 1
    restored = DurableAttemptBudget(db, goal["id"], attempt)
    assert restored.charged == 13
    store.finish(db, goal["id"], attempt, tokens=restored.charged)
    assert store.get(db, goal["id"])["tokens_used"] == 13
    with pytest.raises(ValueError, match="lease"):
        restored.reserve("late", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
async def test_durable_pause_denies_candidate_provider(db, name):
    goal = test_store.create(db, token_budget=100)
    _, attempt = store.claim(db)
    ledger = DurableAttemptBudget(db, goal["id"], attempt)
    test_store.change(db, store.get(db, goal["id"]), "pause")
    calls = []
    adapter = candidate(name, [("record", {"key": "report", "value": "delivered"})], calls)
    adapter = type(adapter)(adapter.model, request_budget=RequestBudget(ledger, 20))
    gateway = FixtureGateway("fixture")
    with pytest.raises(ValueError, match="lease"):
        await adapter.run(gateway, tenant="fixture")
    assert calls == []
    assert gateway.writes == gateway.dispatches == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
@pytest.mark.parametrize("change", ["pause", "cancel", "expire", "recovery"])
async def test_durable_change_during_provider_denies_returned_action(db, name, change):
    goal = test_store.create(db, token_budget=100)
    _, attempt = store.claim(db)
    ledger = DurableAttemptBudget(db, goal["id"], attempt)

    def change_authority():
        if change in {"pause", "cancel"}:
            test_store.change(db, store.get(db, goal["id"]), change)
        else:
            with store.transaction() as cur:
                if change == "expire":
                    cur.execute(
                        "UPDATE pursuit_goals SET lease_until=now()-interval '1 second' WHERE tenant_id=%s",
                        (db,),
                    )
                else:
                    current = store.locked(cur, db, goal["id"])
                    current["recovery_required"] = True
                    store.save(cur, db, current)

    calls = []
    adapter = candidate(
        name, [("record", {"key": "report", "value": "delivered"})], calls, change_authority
    )
    adapter = type(adapter)(adapter.model, request_budget=RequestBudget(ledger, 20))
    host = FixtureGateway(db)
    with pytest.raises(ValueError, match="lease|reconciliation"):
        await adapter.run(GoalGateway(host, ledger), tenant=db)
    assert len(calls) == 1
    assert host.writes == host.dispatches == 0
    assert ledger.charged == 13


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
async def test_goal_gateway_rejects_other_tenant_before_model(db, name):
    goal = test_store.create(db, token_budget=100)
    _, attempt = store.claim(db)
    ledger = DurableAttemptBudget(db, goal["id"], attempt)
    calls = []
    adapter = candidate(name, [("record", {"key": "report", "value": "delivered"})], calls)
    host = FixtureGateway("other")
    with pytest.raises(ValueError, match="tenant mismatch"):
        await adapter.run(GoalGateway(host, ledger), tenant="other")
    assert not calls and host.writes == 0
