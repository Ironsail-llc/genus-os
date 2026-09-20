"""Workflow ownership and durable commands protect persistent browser actions."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from robothor.autonomy.models import Delegation, WebOperation
from robothor.autonomy.workflows.store import WorkflowStore


def prepared(store, identity):
    grant = store.create_grant(
        identity,
        Delegation(
            agent_ids={"main"},
            origins={"https://form.example"},
            actions={"application"},
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    return store.reserve(
        identity,
        grant["id"],
        "main",
        WebOperation(
            origin="https://form.example",
            action="application",
            purpose="Requested application",
            idempotency_key="persistent-application",
        ),
    )


def test_workflow_open_is_idempotent_and_owner_bound(store, identity):
    op = prepared(store, identity)
    repository = WorkflowStore(store)
    instance = str(uuid4())
    row = repository.open(
        identity, "main", op["id"], instance, {"url": "https://form.example/apply"}
    )
    assert (
        repository.open(
            identity, "main", op["id"], instance, {"url": "https://form.example/apply"}
        )["id"]
        == row["id"]
    )
    assert store.operation(identity, op["id"])["workflow_id"] == row["id"]
    with pytest.raises(PermissionError):
        repository.get(identity.model_copy(update={"owner_id": "bob"}), "main", row["id"])
    with pytest.raises(PermissionError):
        repository.get(identity, "other", row["id"])
    with pytest.raises(PermissionError, match="workflow_open_changed"):
        repository.open(identity, "main", op["id"], instance, {"url": "https://form.example/other"})
    with pytest.raises(PermissionError, match="workflow_required"):
        store.begin_submit(identity, op["id"], "main")


def test_commands_replay_results_and_reject_changed_or_stale_actions(store, identity):
    op = prepared(store, identity)
    repository = WorkflowStore(store)
    instance = str(uuid4())
    row = repository.open(
        identity, "main", op["id"], instance, {"url": "https://form.example/apply"}
    )
    command = str(uuid4())
    request = {"kind": "advance", "revision": 0, "plan": {"submit_selector": "#next"}}
    assert repository.begin(identity, "main", row["id"], instance, command, request) is None
    assert (
        repository.begin(identity, "main", row["id"], instance, command, request)["reason"]
        == "command_in_progress"
    )
    result = {"state": "open", "revision": 1, "reason": "workflow_step_completed"}
    repository.finish(identity, "main", row["id"], command, result, advance=True)
    assert repository.begin(identity, "main", row["id"], instance, command, request) == result
    with pytest.raises(PermissionError, match="command_changed"):
        repository.begin(
            identity, "main", row["id"], instance, command, {**request, "kind": "submit"}
        )
    with pytest.raises(PermissionError, match="workflow_revision_changed"):
        repository.begin(identity, "main", row["id"], instance, str(uuid4()), request)
    with pytest.raises(PermissionError, match="workflow_lost"):
        repository.begin(
            identity, "main", row["id"], str(uuid4()), str(uuid4()), {**request, "revision": 1}
        )


def test_transient_code_is_never_retained_in_command_record(store, identity):
    op = prepared(store, identity)
    repository = WorkflowStore(store)
    instance = str(uuid4())
    row = repository.open(
        identity, "main", op["id"], instance, {"url": "https://form.example/apply"}
    )
    command = str(uuid4())
    request = {"kind": "submit", "revision": 0, "verification_code": "739"}
    repository.begin(identity, "main", row["id"], instance, command, request)
    with store.transaction() as cur:
        cur.execute(
            "SELECT request,fingerprint FROM autonomy_workflow_commands WHERE workflow_id=%s",
            (row["id"],),
        )
        record = dict(cur.fetchone())
    assert record["request"] == {"kind": "submit", "revision": 0}
    from robothor.autonomy.workflows.store import fingerprint

    assert record["fingerprint"] == fingerprint(record["request"])


def test_only_one_command_may_run_against_a_workflow_at_a_time(store, identity):
    op = prepared(store, identity)
    repository = WorkflowStore(store)
    instance = str(uuid4())
    row = repository.open(
        identity, "main", op["id"], instance, {"url": "https://form.example/apply"}
    )
    request = {"kind": "advance", "revision": 0, "plan": {"submit_selector": "#next"}}
    assert repository.begin(identity, "main", row["id"], instance, str(uuid4()), request) is None
    second = str(uuid4())
    with pytest.raises(PermissionError, match="command_in_progress"):
        repository.begin(identity, "main", row["id"], instance, second, request)
    with store.transaction() as cur:
        cur.execute(
            "SELECT count(*) AS total FROM autonomy_workflow_commands WHERE workflow_id=%s",
            (row["id"],),
        )
        assert cur.fetchone()["total"] == 1


def paid(store, identity):
    grant = store.create_grant(
        identity,
        Delegation(
            agent_ids={"main"},
            origins={"https://form.example"},
            actions={"purchase"},
            expires_at=datetime.now(UTC) + timedelta(days=1),
            per_purchase_minor=1000,
            monthly_minor=1000,
        ),
    )
    return store.reserve(
        identity,
        grant["id"],
        "main",
        WebOperation(
            origin="https://form.example",
            action="purchase",
            purpose="Requested order",
            amount_minor=600,
            idempotency_key="persistent-purchase",
        ),
    )


def test_a_paid_step_is_never_rolled_back_to_reserved_as_an_intermediate_step(store, identity):
    op = paid(store, identity)
    repository = WorkflowStore(store)
    row = repository.open(
        identity, "main", op["id"], str(uuid4()), {"url": "https://form.example/checkout"}
    )
    store.begin_submit(identity, op["id"], "main", workflow_id=row["id"])
    with pytest.raises(PermissionError, match="intermediate_step_not_allowed"):
        repository.checkpoint(identity, "main", row["id"])
    assert store.operation(identity, op["id"])["state"] == "submitting"


def test_checkpoint_requires_a_claimed_operation_and_an_open_workflow(store, identity):
    op = prepared(store, identity)
    repository = WorkflowStore(store)
    row = repository.open(
        identity, "main", op["id"], str(uuid4()), {"url": "https://form.example/apply"}
    )
    with pytest.raises(PermissionError, match="intermediate_step_not_allowed"):
        repository.checkpoint(identity, "main", row["id"])
    store.begin_submit(identity, op["id"], "main", workflow_id=row["id"])
    repository.checkpoint(identity, "main", row["id"])
    assert store.operation(identity, op["id"])["state"] == "reserved"
    repository.close(identity, "main", row["id"], "closed")
    store.begin_submit(identity, op["id"], "main", workflow_id=row["id"])
    with pytest.raises(PermissionError, match="intermediate_step_not_allowed"):
        repository.checkpoint(identity, "main", row["id"])
