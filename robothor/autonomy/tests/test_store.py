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


def test_resource_descriptors_expose_available_fields_and_provenance_without_values(
    store, identity
):
    ref = store.put_resource(
        identity,
        ResourceInput(
            kind="profile",
            label="Owner profile",
            payload=json.dumps(
                {
                    "first_name": "Alice",
                    "email": "alice@example.com",
                    "phone": "",
                    "answers": {"membership_reason": "Private application answer"},
                }
            ),
        ),
        source="linked_contact",
    )
    expected = ["answers.membership_reason", "email", "first_name"]
    assert ref["descriptor"]["fields"] == expected
    assert ref["descriptor"]["source"] == "linked_contact"
    listed = store.resources(identity)
    assert listed[0]["descriptor"] == ref["descriptor"]
    for value in ("Alice", "alice@example.com", "Private application answer"):
        assert value not in json.dumps(listed)


def test_legacy_descriptor_backfill_is_scoped_and_does_not_claim_unknown_provenance(
    store, identity
):
    ref = store.put_resource(
        identity,
        ResourceInput(
            kind="profile", label="Old profile", payload=json.dumps({"email": "alice@example.com"})
        ),
    )
    with store.transaction() as cur:
        cur.execute(
            "UPDATE vault_resources SET descriptor='{}'::jsonb,created_at='2001-01-01T00:00:00Z' WHERE id=%s",
            (ref["id"],),
        )
    assert store.refresh_resource_descriptors(identity.model_copy(update={"owner_id": "bob"})) == 0
    assert store.refresh_resource_descriptors(identity) == 1
    assert store.refresh_resource_descriptors(identity) == 0
    descriptor = store.resources(identity)[0]["descriptor"]
    assert descriptor["fields"] == ["email"]
    assert descriptor["source"] == "legacy_enrollment"
    assert descriptor["recorded_at"].startswith("2001-01-01")


def submitting(store, identity, key="evidence-1"):
    grant = store.create_grant(identity, policy())
    operation = store.reserve(identity, grant["id"], "main", proposal(key))
    store.begin_submit(identity, operation["id"], "main")
    return operation["id"]


def test_completion_without_evidence_is_refused(store, identity):
    operation = submitting(store, identity)
    for evidence in (None, {}):
        with pytest.raises(ValueError, match="completion_requires_evidence"):
            store.finish(identity, operation, "completed", evidence)
    assert store.operation(identity, operation)["state"] == "submitting"
    store.finish(identity, operation, "completed", {"confirmation_sha256": "a" * 64})
    assert store.operation(identity, operation)["state"] == "completed"


@pytest.mark.parametrize(
    "extra",
    [
        {"screenshot": "data:image/png;base64,AAAA"},
        {"page_text": "Thanks Alice, your card ending 4242 was charged"},
        {"note": "trust me"},
        {"Origin": "https://shop.example"},
    ],
)
def test_completion_evidence_is_limited_to_the_declared_keys(store, identity, extra):
    operation = submitting(store, identity)
    evidence = {"origin": "https://shop.example", "confirmation_sha256": "a" * 64, **extra}
    with pytest.raises(ValueError, match="unsafe_evidence"):
        store.finish(identity, operation, "completed", evidence)
    assert store.operation(identity, operation)["state"] == "submitting"
    assert store.operation(identity, operation)["evidence"] is None


def test_every_allowlisted_evidence_key_is_still_accepted(store, identity):
    operation = submitting(store, identity)
    store.finish(
        identity,
        operation,
        "completed",
        {
            "origin": "https://shop.example",
            "confirmation_sha256": "a" * 64,
            "verified_at": datetime.now(UTC).isoformat(),
            "kind": "merchant_confirmation",
            "confirmation_rule": "order_confirmed",
        },
    )
    assert store.operation(identity, operation)["state"] == "completed"


@pytest.mark.parametrize(
    "rule,accepted",
    [
        ("account_created", True),
        ("application_received", True),
        ("order_confirmed", True),
        ("membership_active", True),
        ("login_confirmed", True),
        ("looks_done", False),
        ("order_confirmed ", False),
        ("Order_confirmed", False),
        ("", False),
    ],
)
def test_confirmation_rule_must_be_one_the_platform_defined(store, identity, rule, accepted):
    operation = submitting(store, identity, key="rule-" + (rule.strip().lower() or "blank"))
    evidence = {"confirmation_sha256": "a" * 64, "confirmation_rule": rule}
    if accepted:
        store.finish(identity, operation, "completed", evidence)
        assert store.operation(identity, operation)["state"] == "completed"
    else:
        with pytest.raises(ValueError, match="unsafe_confirmation_rule"):
            store.finish(identity, operation, "completed", evidence)
        assert store.operation(identity, operation)["state"] == "submitting"


def test_expired_resource_is_neither_listed_nor_released(store, identity):
    ref = store.put_resource(
        identity,
        ResourceInput(
            kind="credential",
            label="One-time login",
            origin="https://shop.example",
            payload=json.dumps({"username": "alice", "password": "private"}),
        ),
        lifetime_seconds=600,
    )
    assert [row["id"] for row in store.resources(identity)] == [ref["id"]]
    assert store.consume_resource(identity, ref["id"], "https://shop.example")["password"] == (
        "private"
    )
    with store.transaction() as cur:
        cur.execute(
            "UPDATE vault_resources SET expires_at=now() - interval '1 second' WHERE id=%s",
            (ref["id"],),
        )
    with pytest.raises(PermissionError, match="resource_not_authorized"):
        store.consume_resource(identity, ref["id"], "https://shop.example")
    assert store.resources(identity) == []


@pytest.mark.parametrize(
    "lifetime,accepted",
    [(None, True), (1, True), (600, True), (0, False), (-1, False), (601, False)],
)
def test_verification_resource_lifetime_is_bounded(store, identity, lifetime, accepted):
    def enrol():
        return store.put_resource(
            identity,
            ResourceInput(
                kind="credential",
                label="Emailed sign-in code",
                origin="https://shop.example",
                payload=json.dumps({"username": "alice", "password": "739215"}),
            ),
            lifetime_seconds=lifetime,
        )

    if accepted:
        assert enrol()["id"]
        assert len(store.resources(identity)) == 1
    else:
        with pytest.raises(ValueError, match="invalid_verification_lifetime"):
            enrol()
        assert store.resources(identity) == []


@pytest.mark.parametrize(
    "label",
    [
        "Loyalty 1234-5678-9012-3456",
        "Card 4242 4242 4242 4242",
        "ghp_" + "A" * 36,
        "Bearer abcdefghijklmnopqrstuvwx",
    ],
)
def test_resource_label_may_not_carry_the_secret_it_names(store, identity, label):
    with pytest.raises(ValueError, match="resource_label_contains_secret"):
        store.put_resource(
            identity,
            ResourceInput(
                kind="credential",
                label=label,
                origin="https://shop.example",
                payload=json.dumps({"username": "alice", "password": "private"}),
            ),
        )
    assert store.resources(identity) == []
