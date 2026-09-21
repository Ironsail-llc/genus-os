"""Recovered effects supplement run status without inventing whole-request success."""

from dataclasses import replace
from uuid import uuid4

import pytest
from psycopg2.extras import Json

from robothor.engine import chat_recovery
from robothor.engine.runtime import ExecutionContext, effects
from robothor.engine.tests.test_chat_recovery import (  # noqa: F401
    identity,
    insert,
    private_database,
    records,
)


def effect(records, monkeypatch, auth, run, *, principal=None):  # noqa: F811
    monkeypatch.setattr(effects, "get_connection", records)
    ctx = ExecutionContext(auth.tenant_id, principal or auth.user_id, str(uuid4()))
    row = effects.begin(ctx, run, "main", "create_note", {"body": "synthetic"})
    effects.mark_dispatched(ctx, row["id"], run)
    effects.finish(ctx, row["id"], run, uncertain=True)
    return ctx, row


def test_polling_changes_from_uncertain_to_verified_without_reexecuting(records, monkeypatch):  # noqa: F811
    auth, client = identity(), str(uuid4())
    run = insert(records, auth, client, status="failed", verified_status=None)
    ctx, row = effect(records, monkeypatch, auth, run)
    pending = chat_recovery.read_outcome(auth, "web:main", client)
    assert pending["reconciliation_pending"] and not pending["verified"]
    assert "outcome is still unresolved" in pending["text"]
    effects.resolve(
        ctx,
        row["id"],
        lambda _: effects.Verification("applied", True, "synthetic-note", {"id": str(row["id"])}),
    )
    for _ in range(2):
        result = chat_recovery.read_outcome(auth, "web:main", client)
        assert result["state"] == "failed" and result["terminal"]
        assert not result["verified"] and not result["reconciliation_pending"]
        assert result["effects"][0]["verified"]
        assert "The CRM note was created" in result["text"]
        assert "without creating another note" in result["text"]
    with records() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_runs WHERE tenant_id=%s", (auth.tenant_id,))
        assert cur.fetchone() == (1,)


@pytest.mark.parametrize("foreign", ["principal", "tenant", "unrelated"])
def test_receipts_cannot_cross_audit_ownership(records, monkeypatch, foreign):  # noqa: F811
    auth, client = identity(), str(uuid4())
    root = insert(records, auth, client)
    owner = replace(auth, tenant_id=str(uuid4())) if foreign == "tenant" else auth
    run = root
    if foreign in {"tenant", "unrelated"}:
        run = insert(
            records, owner, str(uuid4()), parent_run_id=root if foreign == "tenant" else None
        )
    effect(records, monkeypatch, owner, run, principal="other" if foreign == "principal" else None)
    assert chat_recovery.read_outcome(auth, "web:main", client)["effects"] == []


def test_delegated_uncertainty_overrides_parent_success_claim(records, monkeypatch):  # noqa: F811
    auth, client = identity(), str(uuid4())
    root = insert(records, auth, client, output_text="Everything is done.")
    child = insert(records, auth, str(uuid4()), parent_run_id=root, runtime_context=Json({}))
    effect(records, monkeypatch, auth, child)
    result = chat_recovery.read_outcome(auth, "web:main", client)
    assert not result["verified"] and result["reconciliation_pending"]
    assert "Everything is done" not in result["text"]
    assert len(result["effects"]) == 1
