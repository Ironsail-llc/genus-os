# ruff: noqa: F811 - pytest fixtures arrive as parameters named like their imports
"""One uncertain remote write fences that call only, against the real effect ledger.

QA saw one dropped ``org_tool_write`` hold every later remote write by that
person: a dry run to another company and a call to an undeclared tool were
both refused with "This write was NOT sent". The fence is now the child and
its tool: another company or another tool goes ahead and a dry run is no write
at all, but any later write to the same tool in the same company waits,
however it is worded, since a reworded retry carries a new request id the
child would run again. The refusal names the uncertain call by child, tool
and argument digest, never by its arguments.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from robothor.engine.runtime import effects
from robothor.engine.runtime.effect_dispatch import _bypass
from robothor.engine.tests.test_runtime_effects import (  # noqa: F401
    context,
    effect_db,
    private_database,
)

TOOL = "org_tool_write"
SECRET = "patient-name-Alice-Example"


def _args(**over: Any) -> dict[str, Any]:
    return {"tenant": "acme-child-a", "tool": "example_write", "args": {"name": SECRET}, **over}


def _uncertain(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    row = effects.begin(ctx, "worker", "main", TOOL, args)
    assert effects.mark_dispatched(ctx, row["id"], "worker")
    assert effects.finish(ctx, row["id"], "worker", uncertain=True)
    return row


def _later(ctx: Any) -> Any:
    return replace(ctx, request_id="later-request")


def _begin(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    return effects.begin(_later(ctx), "worker", "main", TOOL, args)


@pytest.mark.parametrize(
    "other",
    [
        _args(tenant="acme-child-b"),  # another company
        _args(tool="example_other"),  # another tool, declared or not
    ],
)
def test_another_remote_write_is_not_fenced(effect_db, other) -> None:
    ctx = context()
    _uncertain(ctx, _args())

    assert _begin(ctx, other)["state"] == "prepared"


def test_the_same_remote_write_is_still_fenced_and_named_by_digest_only(effect_db) -> None:
    ctx = context()
    _uncertain(ctx, _args())

    with pytest.raises(effects.EffectPendingError) as refused:
        _begin(ctx, _args())

    text = str(refused.value) + repr(refused.value.blocked_by())
    assert SECRET not in text
    assert "acme-child-a" in text and "example_write" in text and "args_digest" in text


def test_the_stored_target_holds_no_arguments(effect_db) -> None:
    ctx = context()
    row = _uncertain(ctx, _args())

    stored = effects.read(ctx, row["id"])["target"]
    assert SECRET not in repr(stored)
    assert set(stored) == {"tenant", "tool", "args_digest"}


def test_a_dry_run_is_not_journalled_at_all() -> None:
    ctx = type("Ctx", (), {"is_benchmark": False})()
    assert _bypass(TOOL, {**_args(), "dry_run": True}, ctx) is True
    assert _bypass(TOOL, _args(), ctx) is False


@pytest.mark.parametrize(
    "reworded",
    [
        _args(args={"name": "someone else"}),  # other arguments
        _args(args={"name": SECRET, "note": ""}),  # an added blank optional field
        _args(args={"name": SECRET, "count": "10"}),  # "10" for 10
        _args(tenant="  acme-child-a "),  # the same company, spelled with spaces
    ],
)
def test_a_reworded_retry_of_the_same_remote_write_is_held(effect_db, reworded) -> None:
    ctx = context()
    _uncertain(ctx, _args(args={"name": SECRET, "count": 10}))

    with pytest.raises(effects.EffectPendingError):
        _begin(ctx, reworded)
