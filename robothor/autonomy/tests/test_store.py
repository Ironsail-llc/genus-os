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
