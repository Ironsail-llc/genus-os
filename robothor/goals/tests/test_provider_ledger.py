"""Durable request accounting against a disposable PostgreSQL cluster."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from robothor.goals.provider_ledger import DurableAttemptBudget

from robothor.goals import store
from robothor.goals.tests import test_store

create = test_store.create
db = test_store.db
private_database = test_store.private_database


def ledger(tenant, *, budget=100):
    goal = create(tenant, token_budget=budget)
    _, attempt = store.claim(tenant)
    return DurableAttemptBudget(tenant, goal["id"], attempt)


def test_separate_workers_and_reconstruction_share_allowance(db):
    first = ledger(db)
    second = DurableAttemptBudget(db, first.goal_id, first.attempt)

    def reserve(pair):
        worker, key = pair
        try:
            worker.reserve(key, 70)
            return key
        except ValueError as error:
            assert "budget" in str(error)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, [(first, "one"), (second, "two")]))
    admitted = [key for key in results if key]
    assert len(admitted) == 1
    restored = DurableAttemptBudget(db, first.goal_id, first.attempt)
    assert restored.charged == 70
    with pytest.raises(ValueError, match="already reserved"):
        restored.reserve(admitted[0], 1)
    restored.settle(admitted[0], None)
    assert restored.charged == 70
    restored.settle(admitted[0], 20)
    restored.settle(admitted[0], 20)
    assert restored.charged == 20
    with pytest.raises(ValueError, match="conflicting"):
        restored.settle(admitted[0], 19)
    restored.reserve("next", 80)
    assert first.charged == 100


def test_overrun_is_durable_and_denies_future_spending(db):
    budget = ledger(db)
    budget.reserve("one", 20)
    with pytest.raises(ValueError, match="exceeded"):
        budget.settle("one", 30)
    restored = DurableAttemptBudget(db, budget.goal_id, budget.attempt)
    assert restored.charged == 30
    with pytest.raises(ValueError, match="overrun"):
        restored.reserve("two", 1)


def test_expired_worker_reservation_survives_recovery(db):
    budget = ledger(db)
    budget.reserve("interrupted", 70)
    with store.transaction() as cur:
        cur.execute(
            "UPDATE pursuit_goals SET lease_until=now()-interval '1 second' WHERE tenant_id=%s",
            (db,),
        )
    with pytest.raises(ValueError, match="lease"):
        budget.reserve("late", 1)
    store.claim(db)  # existing recovery charges the interrupted attempt to the goal
    goal = store.get(db, budget.goal_id)
    assert goal["tokens_used"] == 70
    with pytest.raises(ValueError, match="reconciliation"):
        budget.settle("interrupted", 10)


def test_parent_budget_and_tenant_authority_apply(db):
    parent = create(db, token_budget=50, kind="long")
    child = create(db, parent_goal_id=parent["id"], token_budget=100, priority=10)
    claimed, attempt = store.claim(db)
    assert claimed["id"] == child["id"]
    budget = DurableAttemptBudget(db, child["id"], attempt)
    with pytest.raises(ValueError, match="family budget"):
        budget.reserve("too-much", 51)
    other = DurableAttemptBudget("other-tenant", child["id"], attempt)
    with pytest.raises(ValueError, match="not found"):
        other.reserve("unauthorized", 1)
    assert budget.charged == 0
