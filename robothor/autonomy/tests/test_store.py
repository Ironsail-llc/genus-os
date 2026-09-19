"""Real transactions on a dedicated disposable PostgreSQL database."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from robothor.autonomy.models import Delegation, ResourceInput, WebOperation


def policy():
    return Delegation(
        agent_ids={"main"},
        origins={"https://shop.example"},
        actions={"purchase"},
        expires_at=datetime.now(UTC) + timedelta(days=1),
        per_purchase_minor=1000,
        monthly_minor=1000,
    )


def proposal(key="purchase-1", amount=600):
    return WebOperation(
        origin="https://shop.example",
        action="purchase",
        purpose="Test order",
        amount_minor=amount,
        idempotency_key=key,
    )


def test_resource_is_scoped_and_references_never_disclose_values(store, identity):
    ref = store.put_resource(
        identity,
        ResourceInput(
            kind="credential",
            label="Shop login",
            origin="https://shop.example",
            payload=json.dumps({"username": "alice", "password": "private"}),
        ),
    )
    assert "private" not in str(ref)
    assert "private" not in str(store.resources(identity))
    assert (
        store.consume_resource(identity, ref["id"], "https://shop.example")["password"] == "private"
    )
    with pytest.raises(PermissionError):
        store.consume_resource(
            identity.model_copy(update={"owner_id": "bob"}), ref["id"], "https://shop.example"
        )
    with pytest.raises(PermissionError):
        store.consume_resource(identity, ref["id"], "https://shop.example.evil.test")


def test_idempotency_and_uncertain_submit_are_not_reexecuted(store, identity):
    grant = store.create_grant(identity, policy())
    first = store.reserve(identity, grant["id"], "main", proposal())
    assert store.reserve(identity, grant["id"], "main", proposal())["id"] == first["id"]
    with pytest.raises(PermissionError, match="idempotency_conflict"):
        store.reserve(identity, grant["id"], "main", proposal(amount=700))
    store.begin_submit(identity, first["id"], "main")
    with pytest.raises(PermissionError, match="reconciliation_required"):
        store.begin_submit(identity, first["id"], "main")
    store.finish(identity, first["id"], "reconciling")
    assert store.operation(identity, first["id"])["state"] == "reconciling"


def test_concurrent_spend_reservations_cannot_exceed_budget(store, identity):
    grant = store.create_grant(identity, policy())

    def reserve(key):
        try:
            return store.reserve(identity, grant["id"], "main", proposal(key))["state"]
        except PermissionError as exc:
            return str(exc)

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(reserve, ["purchase-a", "purchase-b"]))
    assert sorted(outcomes) == ["monthly_limit", "reserved"]


def test_revocation_between_reserve_and_submit_is_enforced(store, identity):
    grant = store.create_grant(identity, policy())
    op = store.reserve(identity, grant["id"], "main", proposal())
    store.revoke_grant(identity, grant["id"])
    with pytest.raises(PermissionError, match="grant_revoked"):
        store.begin_submit(identity, op["id"], "main")


def test_card_cvv_never_reaches_storage(store, identity):
    with pytest.raises(ValueError):
        store.put_resource(
            identity,
            ResourceInput(
                kind="payment_card",
                label="Personal card",
                payload=json.dumps(
                    {
                        "number": "4242424242424242",
                        "name": "Alice",
                        "expiry_month": 12,
                        "expiry_year": 2030,
                        "cvv": "123",
                    }
                ),
            ),
        )
    assert store.resources(identity) == []


def test_disable_before_submit_and_before_click_is_enforced(store, identity):
    from robothor.autonomy.models import RuntimeSettings

    grant = store.create_grant(identity, policy())
    operation = store.reserve(identity, grant["id"], "main", proposal())
    store.configure(identity, RuntimeSettings(enabled=False))
    with pytest.raises(PermissionError, match="autonomous_execution_not_enabled"):
        store.begin_submit(identity, operation["id"], "main")
    store.configure(
        identity,
        RuntimeSettings(
            enabled=True, payment_processing=True, payment_assessment_reference="synthetic-test"
        ),
    )
    store.begin_submit(identity, operation["id"], "main")
    store.configure(identity, RuntimeSettings(enabled=True, payment_processing=False))
    with pytest.raises(PermissionError, match="payment_processing_not_enabled"):
        store.check_authority(identity, operation["id"], "main")


def test_profile_accepts_reusable_application_answers(store, identity):
    resource = store.put_resource(
        identity,
        ResourceInput(
            kind="profile",
            label="Application profile",
            payload=json.dumps(
                {"first_name": "Alice", "answers": {"membership_reason": "Meet other designers"}}
            ),
        ),
    )
    saved = store.consume_resource(identity, resource["id"], "https://shop.example", kind="profile")
    assert saved["answers"]["membership_reason"] == "Meet other designers"


def recurring_policy():
    return policy().model_copy(
        update={
            "actions": frozenset({"purchase", "subscription"}),
            "recurring_minor": 1000,
            "annual_minor": 12000,
        }
    )


def subscription(key, *, days=40):
    return WebOperation(
        origin="https://shop.example",
        action="subscription",
        purpose="Requested membership",
        idempotency_key=key,
        amount_minor=0,
        recurring_minor=600,
        annual_commitment_minor=7200,
        recurrence={
            "interval_months": 1,
            "next_charge_on": (datetime.now(UTC) + timedelta(days=days)).date(),
        },
    )


def test_free_trials_cannot_overbook_a_future_month_across_grants(store, identity):
    first = store.create_grant(identity, recurring_policy())
    second = store.create_grant(identity, recurring_policy())
    op = store.reserve(identity, first["id"], "main", subscription("trial-first"))
    store.begin_submit(identity, op["id"], "main")
    store.finish(identity, op["id"], "completed", {"confirmation_sha256": "a" * 64})
    with pytest.raises(PermissionError, match="monthly_commitment_limit"):
        store.reserve(identity, second["id"], "main", subscription("trial-second"))
    assert (
        store.reserve(identity, second["id"], "main", proposal("current-one-off", 1000))["state"]
        == "reserved"
    )


def test_current_month_renewal_reduces_new_purchase_budget(store, identity):
    grant = store.create_grant(identity, recurring_policy())
    store.reserve(identity, grant["id"], "main", subscription("current-renewal", days=0))
    with pytest.raises(PermissionError, match="monthly_limit"):
        store.reserve(identity, grant["id"], "main", proposal("new-one-off", 500))


def test_new_recurring_commitment_requires_renewal_dates(store, identity):
    grant = store.create_grant(identity, recurring_policy())
    with pytest.raises(PermissionError, match="renewal_schedule_missing"):
        store.reserve(
            identity,
            grant["id"],
            "main",
            subscription("missing-schedule").model_copy(update={"recurrence": None}),
        )


def test_cancelling_unsubmitted_trial_releases_future_commitment(store, identity):
    grant = store.create_grant(identity, recurring_policy())
    op = store.reserve(identity, grant["id"], "main", subscription("trial-cancelled"))
    store.finish(identity, op["id"], "cancelled")
    assert (
        store.reserve(identity, grant["id"], "main", subscription("trial-replacement"))["state"]
        == "reserved"
    )


def test_concurrent_free_trial_reservations_cannot_overbook_renewals(store, identity):
    grant = store.create_grant(identity, recurring_policy())

    def reserve(key):
        try:
            return store.reserve(identity, grant["id"], "main", subscription(key))["state"]
        except PermissionError as exc:
            return str(exc)

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(reserve, ["trial-concurrent-a", "trial-concurrent-b"]))
    assert sorted(outcomes) == ["monthly_commitment_limit", "reserved"]
    forecast = store.spending_projection(identity)
    month = (datetime.now(UTC) + timedelta(days=40)).strftime("%Y-%m")
    assert forecast["months"]["USD"][month] == 600
    assert (
        store.spending_projection(identity.model_copy(update={"owner_id": "bob"}))["months"] == {}
    )
