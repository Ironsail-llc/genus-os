"""Task recovery needs positive evidence and preserves validation failures."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from robothor.engine.runtime import effects, task_recovery
from robothor.engine.tests.test_runtime_effects import (  # noqa: F401
    context,
    effect_db,
    private_database,
)
from robothor.engine.tools import dispatch


@pytest.mark.parametrize("case", ["missing", "deleted", "foreign-tenant", "other-task"])
def test_task_readback_never_interprets_absence_as_nonapplication(effect_db, case):  # noqa: F811
    ctx = context()
    row = effects.begin(ctx, "worker", "main", "create_task", {"title": "Once"})
    effects.mark_dispatched(ctx, row["id"], "worker")
    effects.finish(ctx, row["id"], "worker", uncertain=True)
    if case != "missing":
        with effect_db() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crm_tasks(id,tenant_id,title,deleted_at) VALUES (%s,%s,'Once',CASE WHEN %s THEN now() END)",
                (
                    str(uuid4()) if case == "other-task" else row["id"],
                    str(uuid4()) if case == "foreign-tenant" else ctx.tenant_id,
                    case == "deleted",
                ),
            )
    assert task_recovery.recover(ctx, row["id"]) is None
    assert effects.read(ctx, row["id"])["state"] == "uncertain"
    with pytest.raises(effects.EffectPendingError):
        effects.begin(ctx, "replacement", "main", "create_task", {"title": "Once"})


def test_task_identity_cannot_be_substituted_by_arguments(effect_db):  # noqa: F811
    ctx = context()
    args = {"title": "Once", "task_id": "model-supplied-id"}
    row = effects.begin(ctx, "worker", "main", "create_task", args)
    tool_context = SimpleNamespace(tenant_id=ctx.tenant_id, run_id="worker")
    token = effects.active_effect.set(row)
    try:
        assert task_recovery.task_options(tool_context, args) == {"task_id": str(row["id"])}
        with pytest.raises(ValueError):
            task_recovery.task_options(tool_context, {"title": "Different"})
    finally:
        effects.active_effect.reset(token)


async def test_task_validation_error_is_not_returned_as_successful_task_id(monkeypatch):
    from robothor.crm import dal

    monkeypatch.setattr(
        dal, "create_task", lambda **kwargs: {"error": "synthetic validation failure"}
    )
    monkeypatch.setattr(dispatch, "_audit_tool_call", lambda *a, **k: None)
    result = await dispatch._execute_tool("create_task", {"title": "Once"}, user_role="service")
    assert result == {"error": "synthetic validation failure"}
