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
