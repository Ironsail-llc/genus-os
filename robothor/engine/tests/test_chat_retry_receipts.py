"""A verified retry satisfies the same intent without deleting its failed attempt."""

from dataclasses import replace
from uuid import uuid4

import pytest

from robothor.engine import chat_recovery
from robothor.engine.runtime import ExecutionContext, effects, task_recovery
from robothor.engine.runtime.chat_effect_recovery import reconcile_record
from robothor.engine.tests.test_chat_recovery import (  # noqa: F401
    identity,
    insert,
    private_database,
    records,
)


@pytest.mark.parametrize("root_status", ["completed", "failed"])
@pytest.mark.parametrize(
    "case", ["same", "request", "arguments", "lineage", "principal", "tenant", "invalid-proof"]
)
def test_only_verified_retry_of_same_scoped_intent_satisfies_nonapplication(
    records,  # noqa: F811
    monkeypatch,
    case,
    root_status,
):
    monkeypatch.setattr(effects, "get_connection", records)
    auth, client = identity(), str(uuid4())
    root = insert(records, auth, client, status=root_status, output_text="Requested task created.")
    first_run = insert(records, auth, str(uuid4()), status="failed", parent_run_id=root)
    ctx = ExecutionContext(auth.tenant_id, auth.user_id, str(uuid4()))
    args = {"title": "Once"}
    first = effects.begin(ctx, first_run, "main", "create_task", args)
    reconcile_record(first["id"], auth)
    assert effects.read(ctx, first["id"])["state"] == "not_applied"

    # A separate permitted attempt, not an automatic replay by chat recovery.
    owner = (
        replace(auth, tenant_id=str(uuid4()))
        if case == "tenant"
        else (replace(auth, user_id="other") if case == "principal" else auth)
    )
    retry_run = insert(
        records, owner, str(uuid4()), parent_run_id=None if case == "lineage" else root
    )
    retry_ctx = replace(
        ctx,
        tenant_id=owner.tenant_id,
        principal_id=owner.user_id,
        request_id=str(uuid4()) if case == "request" else ctx.request_id,
    )
    retry = effects.begin(
        retry_ctx,
        retry_run,
        "main",
        "create_task",
        {"title": "Different"} if case == "arguments" else args,
    )
    assert effects.mark_dispatched(retry_ctx, retry["id"], retry_run)
    assert effects.finish(retry_ctx, retry["id"], retry_run, uncertain=True)
    with records() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tasks(id,tenant_id,title) VALUES (%s,%s,'Once')",
            (retry["id"], retry_ctx.tenant_id),
        )
    assert task_recovery.recover(retry_ctx, retry["id"])["id"] == str(retry["id"])
    if case == "invalid-proof":
        with records() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_runtime_effects SET resolution='{}' WHERE id=%s", (retry["id"],)
            )

    result = chat_recovery.read_outcome(auth, "web:main", client)
    old = next(row for row in result["effects"] if row["operation_id"] == str(first["id"]))
    assert old["status"] == "not_applied" and not old["verified"]
    assert not result["reconciliation_pending"]
    assert result["verified"] is (case == "same" and root_status == "completed")
    if case == "same":
        assert old["superseded_by"] == str(retry["id"])
        assert "later attempt of the same action is confirmed" in result["text"]
        if root_status == "completed":
            assert "Requested task created." in result["text"]
    else:
        assert not old.get("superseded_by")
        assert "Requested task created." not in result["text"]
    assert effects.read(ctx, first["id"])["state"] == "not_applied"
