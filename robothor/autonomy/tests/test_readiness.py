"""Task readiness explains missing prerequisites without reserving or decrypting."""

import json

import pytest

from robothor.autonomy.models import ResourceInput, RuntimeSettings
from robothor.autonomy.readiness import ReadinessRequest, task_readiness
from robothor.autonomy.tests.test_store import policy, proposal


def request(grant_id, **updates):
    return ReadinessRequest.model_validate(
        {"grant_id": grant_id, "proposal": proposal().model_dump(mode="json"), **updates}
    )


def test_reports_missing_fields_without_mutation_or_decryption(store, identity, monkeypatch):
    grant = store.create_grant(identity, policy())
    resource = store.put_resource(
        identity,
        ResourceInput(
            kind="profile", label="Contact", payload=json.dumps({"email": "private@example.com"})
        ),
    )
    monkeypatch.setattr(
        store, "consume_resource", lambda *a, **kw: pytest.fail("decrypted resource")
    )
    result = task_readiness(
        store,
        identity,
        "main",
        request(
            grant["id"],
            requirements=[
                {
                    "resource_id": resource["id"],
                    "kind": "profile",
                    "fields": ["email", "first_name"],
                }
            ],
        ),
    )
    assert not result["ready_to_prepare"]
    assert result["requirements"][0]["missing_fields"] == ["first_name"]
    assert "private@example.com" not in str(result)
    assert store.recent_operations(identity) == []


def test_scopes_references_and_explains_revocation_and_disabled_payments(store, identity):
    grant = store.create_grant(identity, policy())
    foreign = identity.model_copy(update={"owner_id": "other"})
    resource = store.put_resource(
        foreign,
        ResourceInput(kind="profile", label="Contact", payload='{"email":"other@example.com"}'),
    )
    store.revoke_grant(identity, grant["id"])
    store.configure(identity, RuntimeSettings(enabled=True))
    result = task_readiness(
        store,
        identity,
        "main",
        request(
            grant["id"],
            requirements=[{"resource_id": resource["id"], "kind": "profile", "fields": ["email"]}],
        ),
    )
    assert not result["ready_to_prepare"]
    assert set(result["blockers"]) == {
        "grant_revoked",
        "payment_processing_not_enabled",
        "resource_requirements_missing",
    }
    assert result["requirements"][0]["status"] == "unavailable"
    assert resource["id"] not in str(result) and "other@example.com" not in str(result)


def test_preview_reuses_budget_authority_and_does_not_hold_funds(store, identity):
    grant = store.create_grant(identity, policy())
    first = task_readiness(store, identity, "main", request(grant["id"]))
    assert first["ready_to_prepare"]
    store.reserve(identity, grant["id"], "main", proposal())
    result = task_readiness(
        store,
        identity,
        "main",
        request(grant["id"], proposal=proposal(key="purchase-2").model_dump(mode="json")),
    )
    assert not result["ready_to_prepare"] and "monthly_limit" in result["blockers"]
    assert len(store.recent_operations(identity)) == 1


def test_origin_bound_resources_and_agent_authority(store, identity):
    grant = store.create_grant(identity, policy())
    resource = store.put_resource(
        identity,
        ResourceInput(
            kind="credential",
            label="Login",
            origin="https://other.example",
            payload='{"username":"alice","password":"private"}',
        ),
    )
    result = task_readiness(
        store,
        identity,
        "other-agent",
        request(
            grant["id"],
            requirements=[
                {
                    "resource_id": resource["id"],
                    "kind": "credential",
                    "fields": ["username", "password"],
                }
            ],
        ),
    )
    assert "agent_not_allowed" in result["blockers"]
    assert result["requirements"][0]["status"] == "unavailable"
    assert not result["ready_to_prepare"]


def test_existing_intent_directs_to_status_without_counting_it_twice(store, identity):
    grant = store.create_grant(identity, policy())
    op = store.reserve(identity, grant["id"], "main", proposal())
    result = task_readiness(store, identity, "main", request(grant["id"]))
    assert result["existing_operation"] == op
    assert result["blockers"] == ["operation_already_prepared"]
    assert not result["ready_to_prepare"]
    other = task_readiness(store, identity, "other-agent", request(grant["id"]))
    assert other["existing_operation"] is None and "idempotency_conflict" in other["blockers"]
    assert op["id"] not in str(other)
