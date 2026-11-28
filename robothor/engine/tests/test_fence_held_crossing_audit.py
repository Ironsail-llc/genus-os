"""A write the effect ledger holds still leaves its cross-tenant audit row.

When an earlier write's outcome is unknown, the fence (``effects.begin``)
refuses a later one before its handler runs. The handler is where a crossing
is normally audited, so a held ``org_tool_write`` (or a held ``create_task``
with ``tenant``) left no row under the home tenant: only the transcript
showed the attempt. ``effect_dispatch.invoke`` now audits the held call, once,
as ``denied`` with ``details.outcome = held_uncertain``, naming the uncertain
call by its effect id and digest, never by its arguments.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.runtime import effects

PARENT, CHILD_A = "acme-parent", "acme-child-a"
SECRET = "patient-name-Alice-Example"
DIGEST = "d" * 64


@dataclass
class _EffectContext:
    tenant_id: str
    principal_id: str


def _held(tool: str, target: dict[str, Any]) -> effects.EffectPendingError:
    return effects.EffectPendingError(
        "e-uncertain",
        record={"tool_name": tool, "target": target, "fingerprint": "f" * 64},
    )


def _invoke(name: str, args: dict[str, Any], held: effects.EffectPendingError):
    from robothor.engine.runtime import effect_dispatch

    ctx = SimpleNamespace(
        tenant_id=PARENT, run_id="run-1", agent_id="main", user_id="alice", is_benchmark=False
    )
    context = _EffectContext(tenant_id=PARENT, principal_id="alice")
    dispatch = AsyncMock()
    with (
        patch.object(effect_dispatch, "active_context", MagicMock(get=lambda: context)),
        patch.object(effect_dispatch, "_reserve", AsyncMock(side_effect=held)),
        patch("robothor.engine.tools.cross_tenant.audit_cross_tenant") as audit,
    ):
        result = asyncio.run(effect_dispatch.invoke(name, args, ctx, dispatch))
    dispatch.assert_not_awaited()
    return result, audit


def test_a_held_remote_write_is_audited_once_as_denied() -> None:
    held = _held(
        "org_tool_write", {"tenant": CHILD_A, "tool": "example_write", "args_digest": DIGEST}
    )
    args = {"tenant": CHILD_A, "tool": "example_write", "args": {"name": SECRET}}

    result, audit = _invoke("org_tool_write", args, held)

    assert result["not_sent"] is True
    audit.assert_called_once()
    kw = audit.call_args.kwargs
    assert (kw["home_tenant"], kw["target_tenant"]) == (PARENT, CHILD_A)
    assert kw["tool"] == "org_tool_write"
    assert kw["outcome"] == "denied"
    assert kw["code"] == "held_uncertain"
    assert kw["remote_tool"] == "example_write"
    assert kw["held"] == {"effect_id": "e-uncertain", "digest": DIGEST}
    assert SECRET not in repr(kw)


def test_a_held_tenant_argument_write_is_audited_once_as_denied() -> None:
    held = _held("create_task", {"tenant": CHILD_A, "assignee": SECRET})
    args = {"tenant": CHILD_A, "title": "Renew the lease", "assignee": SECRET}

    _result, audit = _invoke("create_task", args, held)

    audit.assert_called_once()
    kw = audit.call_args.kwargs
    assert kw["target_tenant"] == CHILD_A
    assert (kw["outcome"], kw["code"]) == ("denied", "held_uncertain")
    assert kw["held"] == {"effect_id": "e-uncertain", "digest": "f" * 64}
    assert SECRET not in repr(kw)


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("create_task", {"title": "own tenant"}),  # crosses nothing
        ("create_task", {"tenant": PARENT, "title": "own tenant, named"}),
    ],
)
def test_a_held_write_that_crosses_nothing_is_not_audited_as_a_crossing(name, args) -> None:
    _result, audit = _invoke(name, args, _held(name, {}))

    audit.assert_not_called()


def test_a_dry_run_never_reaches_the_fence() -> None:
    from robothor.engine.runtime import effect_dispatch

    ctx = SimpleNamespace(is_benchmark=False)
    assert effect_dispatch._bypass(
        "org_tool_write", {"tenant": CHILD_A, "tool": "t", "dry_run": True}, ctx
    )


def test_the_held_code_fits_the_audit_columns() -> None:
    from robothor.audit.logger import COLUMN_LIMITS
    from robothor.tenancy.remote_contract import AUDIT_STATUSES, OUTCOME_HELD_UNCERTAIN

    assert "denied" in AUDIT_STATUSES
    assert len(OUTCOME_HELD_UNCERTAIN) <= COLUMN_LIMITS["status"]
