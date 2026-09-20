"""External verification never turns an acknowledgment into a new submission."""

from uuid import uuid4

import pytest

from robothor.autonomy.handoffs import HandoffRequest, HandoffStore
from robothor.autonomy.tests.test_store import policy, proposal


def request(**changes):
    return HandoffRequest.model_validate(
        {
            "request_id": str(uuid4()),
            "kind": "push",
            "confirmation": {
                "url": "https://shop.example/status?receipt=PrivateLinkCanary",
                "selector": "#done",
                "text": "Order confirmed",
            },
            **changes,
        }
    )


def pending(store, identity):
    grant = store.create_grant(identity, policy())
    return store.reserve(identity, grant["id"], "main", proposal())


def test_acknowledgment_preserves_uncertainty_and_budget_and_does_not_reexecute(store, identity):
    op = pending(store, identity)
    before = store.spending_projection(identity)
    handoffs = HandoffStore(store)
    asked = handoffs.create(identity, op["id"], "main", request())
    assert asked["state"] == "awaiting_external_action"
    assert store.operation(identity, op["id"])["state"] == "reconciling"
    private = handoffs.acknowledge(identity, asked["id"])
    assert private["confirmation"]["selector"] == "#done"
    assert store.operation(identity, op["id"])["state"] == "reconciling"
    assert store.spending_projection(identity) == before
    with pytest.raises(PermissionError):
        store.begin_submit(identity, op["id"], "main")
    with pytest.raises(ValueError):
        store.finish(identity, op["id"], "cancelled")
    assert handoffs.list(identity)[0]["state"] == "checking"


def test_replay_is_idempotent_and_plan_is_private_and_owner_scoped(store, identity):
    op = pending(store, identity)
    handoffs = HandoffStore(store)
    spec = request()
    first = handoffs.create(identity, op["id"], "main", spec)
    assert handoffs.create(identity, op["id"], "main", spec) == first
    assert "PrivateLinkCanary" not in str(first) + str(handoffs.list(identity))
    with store.transaction() as cur:
        cur.execute("SELECT encrypted_value FROM autonomy_handoffs WHERE id=%s", (first["id"],))
        assert b"PrivateLinkCanary" not in bytes(cur.fetchone()["encrypted_value"])
    other = identity.model_copy(update={"owner_id": "other"})
    assert handoffs.list(other) == []
    with pytest.raises(PermissionError):
        handoffs.acknowledge(other, first["id"])
    with pytest.raises(PermissionError):
        handoffs.create(identity, op["id"], "other-agent", spec)
    with pytest.raises(PermissionError, match="handoff_request_changed"):
        handoffs.create(identity, op["id"], "main", spec.model_copy(update={"kind": "passkey"}))


def test_confirmed_completion_resolves_handoff_and_records_submission(store, identity):
    from robothor.autonomy.payment_journal import PaymentJournal

    op = pending(store, identity)
    handoffs = HandoffStore(store)
    asked = handoffs.create(identity, op["id"], "main", request())
    store.finish(
        identity,
        op["id"],
        "completed",
        {"origin": "https://shop.example", "confirmation_sha256": "a" * 64},
    )
    assert handoffs.list(identity)[0]["state"] == "resolved"
    assert PaymentJournal(store).read(identity, op["id"])["position"]["state"] == "submitted"
    with pytest.raises(PermissionError):
        handoffs.acknowledge(identity, asked["id"])


def test_expiration_does_not_free_budget_or_reenable_submission(store, identity):
    op = pending(store, identity)
    handoffs = HandoffStore(store)
    asked = handoffs.create(identity, op["id"], "main", request())
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_handoffs SET expires_at=now()-interval '1 second' WHERE id=%s",
            (asked["id"],),
        )
    assert handoffs.list(identity)[0]["state"] == "expired"
    with pytest.raises(PermissionError, match="handoff_expired"):
        handoffs.acknowledge(identity, asked["id"])
    assert store.operation(identity, op["id"])["state"] == "reconciling"
    assert sum(store.spending_projection(identity)["months"]["USD"].values()) == 600


def test_foreign_confirmation_and_revoked_grant_cannot_start_handoff(store, identity):
    op = pending(store, identity)
    handoffs = HandoffStore(store)
    with pytest.raises(PermissionError):
        handoffs.create(
            identity,
            op["id"],
            "main",
            request(
                confirmation={
                    "url": "https://other.example/status",
                    "selector": "#done",
                    "text": "Order confirmed",
                }
            ),
        )
    store.revoke_grant(identity, store.operation(identity, op["id"])["grant_id"])
    with pytest.raises(PermissionError):
        handoffs.create(identity, op["id"], "main", request())
    assert store.operation(identity, op["id"])["state"] == "reserved"


def test_fresh_process_can_resume_encrypted_handoff_without_secret_output(store, identity):
    import json
    import subprocess
    import sys

    op = pending(store, identity)
    handoff = HandoffStore(store).create(identity, op["id"], "main", request())
    script = """
import json,os,sys
import psycopg2
from robothor.autonomy.store import AutonomyStore
from robothor.autonomy.models import Scope
from robothor.autonomy.handoffs import HandoffStore
args=json.load(sys.stdin)
store=AutonomyStore(lambda: psycopg2.connect(os.environ['AUTONOMY_TEST_DSN']),keys={'v1':b'x'*32},key_id='v1')
scope=Scope.model_validate(args['scope'])
private=HandoffStore(store).acknowledge(scope,args['handoff'])
print(json.dumps({'confirmation_recovered':private['confirmation']['selector']=='#done','state':store.operation(scope,private['operation_id'])['state']}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps({"scope": identity.model_dump(), "handoff": handoff["id"]}),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"confirmation_recovered": True, "state": "reconciling"}
    assert "PrivateLinkCanary" not in result.stdout + result.stderr


def test_existing_handoff_check_survives_revocation_and_disabled_execution(store, identity):
    from robothor.autonomy.models import RuntimeSettings

    op = pending(store, identity)
    handoffs = HandoffStore(store)
    asked = handoffs.create(identity, op["id"], "main", request())
    store.revoke_grant(identity, store.operation(identity, op["id"])["grant_id"])
    store.configure(identity, RuntimeSettings(enabled=False))
    assert handoffs.acknowledge(identity, asked["id"])["operation_id"] == op["id"]
    assert store.operation(identity, op["id"])["state"] == "reconciling"
