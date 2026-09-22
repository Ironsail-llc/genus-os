"""Host-owned note identity and positive readback; absence never permits replay."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from robothor.engine.runtime import effects, note_recovery
from robothor.engine.tests.test_runtime_effects import (  # noqa: F401
    begin,
    context,
    effect_db,
    private_database,
)


@pytest.fixture
def notes(effect_db):  # noqa: F811
    with effect_db() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS crm_notes(
            id UUID PRIMARY KEY,tenant_id TEXT,title TEXT,deleted_at TIMESTAMPTZ)""")

    def insert(ctx, identifier, *, deleted=False):
        with effect_db() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crm_notes VALUES (%s,%s,'Synthetic note',CASE WHEN %s THEN now() END)",
                (identifier, ctx.tenant_id, deleted),
            )

    return insert


@pytest.mark.parametrize("case", ["missing", "deleted", "other-tenant", "other-id"])
def test_inconclusive_readback_never_clears_uncertainty(effect_db, notes, case):  # noqa: F811
    ctx = context()
    row = begin(ctx)
    effects.finish(ctx, row["id"], "worker", uncertain=True)
    if case != "missing":
        owner = ctx if case != "other-tenant" else context()
        identifier = row["id"] if case != "other-id" else str(uuid4())
        notes(owner, identifier, deleted=case == "deleted")
    assert note_recovery.recover(ctx, row["id"]) is None
    assert effects.read(ctx, row["id"])["state"] == "uncertain"
    with pytest.raises(effects.EffectPendingError):
        begin(ctx, run="replacement")


def test_readback_requires_original_principal_and_returns_receipt(effect_db, notes):  # noqa: F811
    ctx = context()
    row = begin(ctx)
    effects.finish(ctx, row["id"], "worker", uncertain=True)
    notes(ctx, row["id"])
    foreign = SimpleNamespace(tenant_id=ctx.tenant_id, principal_id="different")
    assert note_recovery.recover(foreign, row["id"]) is None
    result = note_recovery.recover(ctx, row["id"])
    assert result["recovered"] and result["id"] == str(row["id"])
    assert note_recovery.recover(ctx, row["id"]) == result


def test_only_matching_dispatch_receives_reserved_note_identity(effect_db):  # noqa: F811
    ctx = context()
    args = {"body": "once", "note_id": "model-supplied-id"}
    row = effects.begin(ctx, "worker", "main", "create_note", args)
    tool_context = SimpleNamespace(tenant_id=ctx.tenant_id, run_id="worker")
    assert note_recovery.note_options(tool_context, args) == {}
    token = effects.active_effect.set(row)
    try:
        assert note_recovery.note_options(tool_context, args) == {"note_id": str(row["id"])}
        with pytest.raises(ValueError):
            note_recovery.note_options(tool_context, {"body": "different"})
        with pytest.raises(ValueError):
            note_recovery.note_options(SimpleNamespace(tenant_id="other", run_id="worker"), args)
    finally:
        effects.active_effect.reset(token)
