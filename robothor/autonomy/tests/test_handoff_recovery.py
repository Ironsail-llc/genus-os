"""A requested read-only check survives interruption without duplicate workers."""

from robothor.autonomy.handoff_recovery import HandoffChecks
from robothor.autonomy.handoffs import HandoffStore
from robothor.autonomy.tests.test_handoffs import pending, request


def queued(store, identity):
    op = pending(store, identity)
    asked = HandoffStore(store).create(identity, op["id"], "main", request())
    HandoffStore(store).acknowledge(identity, asked["id"])
    return op, asked


def test_claim_survives_restart_and_old_worker_cannot_finish_new_lease(store, identity):
    op, asked = queued(store, identity)
    queue = HandoffChecks(store)
    first = queue.claim(identity, asked["id"])
    assert first and first["operation_id"] == op["id"]
    assert queue.claim(identity, asked["id"]) is None
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_handoffs SET check_lease_until=now()-interval '1 second' WHERE id=%s",
            (asked["id"],),
        )
    second = HandoffChecks(store).claim(identity, asked["id"])
    assert second and second["token"] != first["token"]
    queue.finish(identity, asked["id"], first["token"])
    assert HandoffStore(store).list(identity)[0]["state"] == "checking"
    queue.finish(identity, asked["id"], second["token"])
    assert HandoffStore(store).list(identity)[0]["state"] == "awaiting_external_action"
    assert store.operation(identity, op["id"])["state"] == "reconciling"


def test_only_acknowledged_unexpired_owner_scoped_checks_are_claimable(store, identity):
    op = pending(store, identity)
    asked = HandoffStore(store).create(identity, op["id"], "main", request())
    queue = HandoffChecks(store)
    assert queue.claim(identity, asked["id"]) is None
    HandoffStore(store).acknowledge(identity, asked["id"])
    assert queue.claim(identity.model_copy(update={"owner_id": "other"}), asked["id"]) is None
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_handoffs SET expires_at=now()-interval '1 second' WHERE id=%s",
            (asked["id"],),
        )
    assert queue.claim(identity, asked["id"]) is None
    # An expired handoff releases the operation to the owner rather than
    # pinning it in reconciling forever with its reservation still counted.
    assert store.operation(identity, op["id"])["state"] == "awaiting_input"


def test_crash_retries_are_bounded_and_owner_click_does_not_steal_active_lease(store, identity):
    _, asked = queued(store, identity)
    queue = HandoffChecks(store)
    for _ in range(3):
        claimed = queue.claim(identity, asked["id"])
        assert claimed
        HandoffStore(store).acknowledge(identity, asked["id"])
        assert queue.claim(identity, asked["id"]) is None
        with store.transaction() as cur:
            cur.execute(
                "UPDATE autonomy_handoffs SET check_lease_until=now()-interval '1 second' WHERE id=%s",
                (asked["id"],),
            )
    assert queue.claim(identity, asked["id"]) is None
    # Exhausted is not byte-identical to never-checked: the owner must be able
    # to tell "we looked three times and could not tell" from "not looked at".
    assert HandoffStore(store).list(identity)[0]["state"] == "unconfirmed"
    HandoffStore(store).acknowledge(identity, asked["id"])
    assert queue.claim(identity, asked["id"]) is not None


async def test_worker_is_read_only_and_cancellation_leaves_durable_retry(
    store, identity, monkeypatch
):
    import asyncio
    from unittest.mock import AsyncMock

    from robothor.autonomy import handoff_worker

    op, asked = queued(store, identity)
    browser = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(handoff_worker, "run_browser", browser)
    queue = HandoffChecks(store)
    try:
        await handoff_worker.check_one(queue, identity, asked["id"])
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("Cancellation must propagate")
    assert HandoffStore(store).list(identity)[0]["state"] == "checking"
    assert queue.claim(identity, asked["id"]) is None
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_handoffs SET check_lease_until=now()-interval '1 second' WHERE id=%s",
            (asked["id"],),
        )
    browser.side_effect = None
    browser.return_value = {"state": "reconciling"}
    await handoff_worker.check_one(HandoffChecks(store), identity, asked["id"])
    assert browser.await_count == 2
    assert browser.call_args.kwargs == {"reconcile": True}
    plan = browser.call_args.args[3]
    assert plan.fields == [] and plan.check_selectors == []
    assert HandoffStore(store).list(identity)[0]["state"] == "awaiting_external_action"
    assert store.operation(identity, op["id"])["state"] == "reconciling"


async def test_transient_broker_failure_retries_but_missing_confirmation_does_not(
    store, identity, monkeypatch
):
    from unittest.mock import AsyncMock

    from robothor.autonomy import handoff_worker

    _, asked = queued(store, identity)
    browser = AsyncMock(return_value={"state": "reconciling", "reason": "broker_unavailable"})
    monkeypatch.setattr(handoff_worker, "run_browser", browser)
    queue = HandoffChecks(store)
    await handoff_worker.check_one(queue, identity, asked["id"])
    assert HandoffStore(store).list(identity)[0]["state"] == "checking"
    assert queue.claim(identity, asked["id"]) is None
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_handoffs SET check_lease_until=now()-interval '1 second' WHERE id=%s",
            (asked["id"],),
        )
    browser.return_value = {"state": "reconciling", "reason": "confirmation_missing"}
    await handoff_worker.check_one(queue, identity, asked["id"])
    assert HandoffStore(store).list(identity)[0]["state"] == "awaiting_external_action"


def test_concurrent_service_workers_share_one_durable_claim(store, identity):
    from concurrent.futures import ThreadPoolExecutor

    _, asked = queued(store, identity)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(lambda _: HandoffChecks(store).claim(identity, asked["id"]), range(8))
        )
    assert sum(claim is not None for claim in claims) == 1


def test_confirmed_completion_cannot_be_undone_by_retry_cleanup(store, identity):
    op, asked = queued(store, identity)
    queue = HandoffChecks(store)
    claim = queue.claim(identity, asked["id"])
    store.finish(
        identity,
        op["id"],
        "completed",
        {"origin": "https://shop.example", "confirmation_sha256": "a" * 64},
    )
    queue.finish(identity, asked["id"], claim["token"], retry=True)
    assert HandoffStore(store).list(identity)[0]["state"] == "resolved"
    assert queue.claim(identity, asked["id"]) is None


def test_new_process_claims_interrupted_check_without_returning_private_plan(store, identity):
    import json
    import os
    import subprocess
    import sys

    op, asked = queued(store, identity)
    first = HandoffChecks(store).claim(identity, asked["id"])
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_handoffs SET check_lease_until=now()-interval '1 second' WHERE id=%s",
            (asked["id"],),
        )
    script = """
import json,os,sys,psycopg2
from robothor.autonomy.models import Scope
from robothor.autonomy.store import AutonomyStore
from robothor.autonomy.handoff_recovery import HandoffChecks
payload=json.load(sys.stdin)
store=AutonomyStore(lambda:psycopg2.connect(os.environ['AUTONOMY_TEST_DSN']), keys={'v1':b'x'*32},key_id='v1')
queue=HandoffChecks(store);scope=Scope.model_validate(payload['scope'])
claim=queue.claim(scope,payload['id'])
assert claim and claim['token']!=payload['old_token']
confirmation=queue.confirmation(scope,payload['id'],claim['token'])
assert confirmation['selector']=='#done'
queue.finish(scope,payload['id'],claim['token'])
print(json.dumps({'resumed':True}))
"""
    child = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(
            {"scope": identity.model_dump(), "id": asked["id"], "old_token": first["token"]}
        ),
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(child.stdout) == {"resumed": True}
    assert "PrivateLinkCanary" not in child.stdout + child.stderr
    assert store.operation(identity, op["id"])["state"] == "reconciling"
