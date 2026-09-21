"""Reviewed attribution repair changes both customer states atomically."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from robothor.operations.store import Conflict
from robothor.sales.business import BusinessObservations
from robothor.sales.tests.test_business import order, practice, prospect


def setup(sales):
    business = BusinessObservations(sales)
    old, target = prospect(sales, "old"), prospect(sales, "target")
    identity = practice(business)
    business.bind(old, identity, "v1", "operator:test", "Reviewed original practice ownership")
    order(business)
    return business, old, target, identity


def repair(business, old, target, identity, **changes):
    values = {
        "expected_revision": "v1",
        "expected_prospect_id": old,
        "target_prospect_id": target,
        "expected_binding_version": 1,
        "actor": "operator:test",
        "reason": "Corrected ownership after reviewing company records",
    }
    values.update(changes)
    return business.reassign(identity, **values)


def test_reassignment_moves_current_orders_and_preserves_incomplete_history(sales):
    business, old, target, identity = setup(sales)
    before = {p: sales.get(p)["outcome_version"] for p in (old, target)}
    actions = [sales.ops.propose("sales.email", p, {"prospect_id": p}) for p in (old, target)]
    repair(business, old, target, identity)
    assert sales.retention(old)["completed_orders"] is None
    assert sales.retention(old)["coverage_complete"] is False
    assert sales.retention(old)["repeat_within_30_days"] is None
    assert sales.retention(target)["completed_orders"] == 1
    for p in (old, target):
        assert sales.get(p)["owner"] == "human_review"
        assert sales.get(p)["status"] == "customer_review"
        assert sales.get(p)["outcome_version"] == before[p] + 1
        with sales.ops.transaction() as cur:
            cur.execute(
                "SELECT actor,detail FROM operation_audit WHERE tenant_id=%s AND entity_id=%s AND event='business.customer_reassigned'",
                (sales.tenant, p),
            )
            audit = cur.fetchone()
            assert audit["actor"] == "operator:test"
            assert audit["detail"]["from_prospect_id"] == old
            assert audit["detail"]["to_prospect_id"] == target
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT status FROM operation_actions WHERE tenant_id=%s AND id=ANY(%s::uuid[])",
            (sales.tenant, actions),
        )
        assert {row["status"] for row in cur.fetchall()} == {"cancelled"}
    row = business.records()["items"][0]
    assert str(row["prospect_id"]) == target and row["binding_version"] == 2
    with pytest.raises(Conflict):
        sales.bind_customer(old, "legacy-company", "operator:test")


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_revision": "stale"},
        {"expected_binding_version": 9},
        {"actor": "agent:worker"},
        {"expected_prospect_id": "00000000-0000-4000-8000-000000000009"},
    ],
)
def test_stale_or_nonhuman_reassignment_is_refused(sales, changes):
    business, old, target, identity = setup(sales)
    with pytest.raises(Conflict):
        repair(business, old, target, identity, **changes)
    assert sales.retention(old)["completed_orders"] == 1
    assert sales.retention(target)["completed_orders"] == 0


def test_binding_version_refuses_aba_reassignment(sales):
    business, old, target, identity = setup(sales)
    repair(business, old, target, identity)
    repair(business, target, old, identity, expected_binding_version=2)
    with pytest.raises(Conflict):
        repair(business, old, target, identity)  # Same identity/customer, newer review.
    assert business.records()["items"][0]["binding_version"] == 3


def test_concurrent_reassignments_have_one_winner(sales):
    business, old, target, identity = setup(sales)
    other = prospect(sales, "other")

    def attempt(customer):
        try:
            repair(business, old, customer, identity)
            return True
        except Conflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt, [target, other])) == 1
    assert business.records()["items"][0]["binding_version"] == 2


def test_reassignment_failure_rolls_back_binding_and_both_customer_updates(sales, monkeypatch):
    business, old, target, identity = setup(sales)
    before = {p: sales.get(p)["outcome_version"] for p in (old, target)}
    original = sales.escalate
    count = 0

    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("synthetic failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(sales, "escalate", fail_second)
    with pytest.raises(RuntimeError):
        repair(business, old, target, identity)
    assert str(business.records()["items"][0]["prospect_id"]) == old
    assert all(sales.get(p)["outcome_version"] == before[p] for p in (old, target))


def test_reassignment_rejects_legacy_target_and_cross_tenant_observations(sales):
    from robothor.sales.service import Sales

    business, old, target, identity = setup(sales)
    sales.bind_customer(target, "legacy", "operator:test")
    with pytest.raises(Conflict):
        repair(business, old, target, identity)
    with pytest.raises(Conflict):
        repair(BusinessObservations(Sales("foreign-tenant")), old, target, identity)


def test_new_binding_after_detachment_restores_observation_attribution(sales):
    business, old, target, identity = setup(sales)
    repair(business, old, target, identity)
    assert sales.retention(old)["requires_review"] is True
    repair(business, target, old, identity, expected_binding_version=2)
    assert sales.retention(old)["requires_review"] is False
    assert sales.retention(old)["completed_orders"] == 1
    assert sales.retention(target)["completed_orders"] is None


def test_identity_hold_and_reconfirmation_advance_the_binding_review_version(sales):
    business, old, target, identity = setup(sales)
    practice(business, name="Updated Name", revision="v2")
    assert business.records()["items"][0]["binding_version"] == 2
    business.bind(old, identity, "v2", "operator:test", "Reviewed changed practice name")
    assert business.records()["items"][0]["binding_version"] == 3
    with pytest.raises(Conflict):
        repair(business, old, target, identity, expected_revision="v2")


def test_reassignment_cannot_merge_different_source_accounts(sales):
    business, old, target, identity = setup(sales)
    data = business.records()["items"][0]["data"]
    other = business.observe(
        "orders_app",
        "another-account",
        "practice",
        "another-practice",
        "v1",
        datetime.now(UTC),
        data,
    )
    business.bind(target, other, "v1", "operator:test", "Reviewed separate account identity")
    with pytest.raises(Conflict, match="authoritative"):
        repair(business, old, target, identity)
    assert sales.retention(old)["completed_orders"] == 1


def test_remaining_reviewed_practice_keeps_its_own_order_attribution(sales):
    business, old, target, identity = setup(sales)
    data = business.records()["items"][0]["data"]
    other = business.observe(
        "orders_app", "account-1", "practice", "practice-2", "v1", datetime.now(UTC), data
    )
    business.bind(
        old, other, "v1", "operator:test", "Reviewed another practice owned by this customer"
    )
    order_data = business.records(kind="order")["items"][0]["data"]
    business.observe(
        "orders_app",
        "account-1",
        "order",
        "order-2",
        "v1",
        datetime.now(UTC),
        {**order_data, "practice_id": "practice-2"},
    )
    repair(business, old, target, identity)
    assert sales.retention(old)["completed_orders"] == 1
    assert sales.retention(old)["requires_review"] is False
    assert sales.retention(target)["completed_orders"] == 1
