"""Request attribution is trusted context, never model/page-supplied authority."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from robothor.autonomy.models import RequestContext
from robothor.autonomy.tests.test_store import policy, proposal


def test_request_context_is_durable_scoped_and_preserved_on_retry(store, identity):
    grant = store.create_grant(identity, policy())
    original = RequestContext(run_id=uuid4(), actor_id="operator")
    operation = store.reserve(identity, grant["id"], "main", proposal(), request_context=original)
    retry = store.reserve(
        identity,
        grant["id"],
        "main",
        proposal(),
        request_context=RequestContext(run_id=uuid4(), actor_id="linked-operator"),
    )
    assert retry["id"] == operation["id"]
    assert store.operation(identity, operation["id"])["request_context"] == original.model_dump(
        mode="json"
    )
    with pytest.raises(PermissionError):
        store.operation(identity.model_copy(update={"owner_id": "another-person"}), operation["id"])


def test_retry_does_not_invent_origin_for_legacy_operation(store, identity):
    grant = store.create_grant(identity, policy())
    operation = store.reserve(identity, grant["id"], "main", proposal())
    store.reserve(
        identity,
        grant["id"],
        "main",
        proposal(),
        request_context=RequestContext(run_id=uuid4(), actor_id="operator"),
    )
    assert store.operation(identity, operation["id"])["request_context"] is None


@pytest.mark.parametrize(
    "value",
    [
        {"run_id": "private non-run value", "actor_id": "operator"},
        {"run_id": str(uuid4()), "actor_id": "operator", "password": "private value"},
    ],
)
def test_request_context_contains_only_identifier_fields(value):
    with pytest.raises(ValidationError):
        RequestContext.model_validate(value)


async def test_handler_attributes_operation_to_authenticated_run_not_model_args(
    store, identity, monkeypatch
):
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import autonomy

    monkeypatch.setattr(autonomy, "AutonomyStore", lambda: store)
    monkeypatch.setattr(autonomy, "scope_for_actor", lambda tenant, actor: identity)
    grant = store.create_grant(identity, policy())
    ctx = ToolContext(
        agent_id="main",
        run_id=str(uuid4()),
        user_id="verified-actor",
        user_role="owner",
        tenant_id=identity.tenant_id,
    )
    result = await autonomy.handle(
        {
            "kind": "prepare",
            "grant_id": grant["id"],
            "proposal": proposal().model_dump(mode="json"),
            "request_context": {"run_id": str(uuid4()), "actor_id": "forged-actor"},
        },
        ctx,
    )
    assert "id" in result, result
    assert store.operation(identity, result["id"])["request_context"] == {
        "run_id": ctx.run_id,
        "actor_id": ctx.user_id,
    }


def test_request_context_migration_is_in_canonical_discovery():
    from robothor.db.migrate import _discover

    migrations = _discover()
    ids = [item.migration_id for item in migrations]
    assert "130_autonomy_request_context" in ids
    assert ids.index("129_autonomy_workflows") < ids.index("130_autonomy_request_context")
