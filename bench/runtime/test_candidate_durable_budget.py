"""Candidate provider calls with the host's private PostgreSQL goal ledger."""

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("deepagents")

from bench.runtime.budgeted_models import RequestBudget
from bench.runtime.candidates import FixtureGateway
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
    result = await adapter.run(FixtureGateway("fixture"), tenant="fixture")
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
