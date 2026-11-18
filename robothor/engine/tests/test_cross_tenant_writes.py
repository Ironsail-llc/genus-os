"""Cross-tenant writes: an allowlist, an owner rule, a dry run, an audit row.

A write may name a ``tenant`` only when it is on ``CROSS_TENANT_WRITES``. Of
those, an unattended run (no verified human) may only create a task or send a
notification in a child; every other cross-tenant write needs the verified
owner, judged against the run's OWN context before the tenant is swapped
(``owner_gate.is_verified_owner`` checks the role in ``ctx.tenant_id``).

``dry_run: true`` authorises exactly as the real call, writes the audit row
marked as a dry run, describes what would change, and changes nothing: the
handler never runs and no durable effect is reserved.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.db.connection import current_tenant_scope
from robothor.engine.models import TriggerType
from robothor.engine.runtime import effects
from robothor.identity import IdentityContext

PARENT, CHILD_A, CHILD_B = "acme-parent", "acme-child-a", "acme-child-b"
OWNER_REACH = (PARENT, CHILD_A, CHILD_B)
TASK_ID = "11111111-1111-1111-1111-111111111111"

OWNER = IdentityContext(
    tenant_id=PARENT, channel="webchat", identifier="alice", verified=True, role="owner"
)
ADMIN = IdentityContext(
    tenant_id=PARENT, channel="webchat", identifier="bob", verified=True, role="admin"
)

WRITES = ("create_task", "update_task", "create_note", "update_note", "send_notification")


def _session(**run: Any) -> SimpleNamespace:
    base = {
        "parent_run_id": None,
        "nesting_depth": 0,
        "trigger_type": TriggerType.WEBCHAT,
        "is_benchmark": False,
    }
    base.update(run)
    return SimpleNamespace(run=SimpleNamespace(**base))


class _Seen:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def handler(self, name: str):
        async def _h(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            self.calls.append(
                {
                    "name": name,
                    "args": dict(args),
                    "tenant_id": ctx.tenant_id,
                    "scope": current_tenant_scope(),
                    "effect": effects.active_effect.get(),
                }
            )
            return {"id": "new-id", "success": True}

        return _h


@pytest.fixture
def env():
    seen = _Seen()
    handlers = {n: seen.handler(n) for n in (*WRITES, "delete_task")}
    registry = MagicMock()
    registry.get_adapter_route.return_value = None
    state = SimpleNamespace(seen=seen, session=_session())
    runtime = AsyncMock(return_value=None)
    rbac = MagicMock(return_value=None)
    with (
        patch("robothor.engine.permissions.check_tool_permission", rbac),
        patch("robothor.engine.tools.dispatch._runtime_denial", runtime),
        patch("robothor.engine.tools.dispatch._get_handlers", return_value=handlers),
        patch("robothor.engine.tools.get_registry", return_value=registry),
        patch("robothor.engine.tools.dispatch._audit_tool_call"),
        patch(
            "robothor.engine.session_registry.lookup",
            side_effect=lambda run_id: state.session,
        ),
        patch("robothor.engine.tools.cross_tenant.audit_cross_tenant") as audit,
    ):
        state.audit, state.runtime, state.rbac = audit, runtime, rbac
        yield state


def _call(name: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
    from robothor.engine.tools.dispatch import _execute_tool

    base: dict[str, Any] = {
        "agent_id": "main",
        "run_id": "run-1",
        "tenant_id": PARENT,
        "user_id": "alice",
        "user_role": "owner",
        "accessible_tenant_ids": OWNER_REACH,
        "identity": OWNER,
    }
    base.update(kw)
    return asyncio.run(_execute_tool(name, args, **base))


def _refused(result: dict[str, Any], env: Any) -> str:
    assert result.get("refused") is True, result
    assert env.seen.calls == [], "a refused write must run nothing"
    return str(result["error"])


# ── The verified owner ─────────────────────────────────────────────────────


@pytest.mark.parametrize("name", WRITES)
def test_the_verified_owner_may_make_every_allowlisted_write_in_a_child(env, name):
    result = _call(name, {"tenant": CHILD_A, "id": TASK_ID, "title": "Renew the lease"})

    assert "error" not in result, result
    (call,) = env.seen.calls
    assert call["tenant_id"] == CHILD_A
    assert call["scope"] == CHILD_A
    assert "tenant" not in call["args"]


def test_a_cross_tenant_write_is_audited_with_both_tenants(env):
    _call("create_task", {"tenant": CHILD_A, "title": "Renew the lease"})

    env.audit.assert_called_once()
    kw = env.audit.call_args.kwargs
    assert (kw["home_tenant"], kw["target_tenant"]) == (PARENT, CHILD_A)
    assert (kw["user_id"], kw["tool"], kw["outcome"]) == ("alice", "create_task", "ok")
    assert kw.get("dry_run", False) is False


def test_existing_admission_still_runs_for_a_cross_tenant_write(env):
    _call("update_task", {"tenant": CHILD_A, "id": TASK_ID, "status": "DONE"})

    env.rbac.assert_called_once()
    assert env.rbac.call_args.args[1] == PARENT, "RBAC judges the caller's own tenant role"
    env.runtime.assert_awaited_once()


def test_the_handler_sees_no_home_tenant_effect_record(env):
    token = effects.active_effect.set({"tenant_id": PARENT, "tool_name": "create_task"})
    try:
        _call("create_task", {"tenant": CHILD_A, "title": "Renew the lease"})
    finally:
        effects.active_effect.reset(token)

    assert env.seen.calls[0]["effect"] is None


# ── Off the allowlist ──────────────────────────────────────────────────────


def test_a_write_off_the_allowlist_is_refused_even_for_the_owner(env):
    _refused(_call("delete_task", {"tenant": CHILD_A, "id": TASK_ID}), env)


# ── Unattended runs ────────────────────────────────────────────────────────


def _unattended(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return _call(name, args, identity=None, user_id="service:main", user_role="service")


@pytest.mark.parametrize("name", ["create_task", "send_notification"])
def test_an_unattended_run_may_create_a_task_or_a_notification_in_a_child(env, name):
    env.session = _session(trigger_type=TriggerType.CRON)
    result = _unattended(name, {"tenant": CHILD_A, "title": "Look at this", "subject": "s"})

    assert "error" not in result, result
    assert env.seen.calls[0]["tenant_id"] == CHILD_A


@pytest.mark.parametrize("name", ["update_task", "create_note", "update_note"])
def test_an_unattended_run_may_not_make_any_other_write_in_a_child(env, name):
    env.session = _session(trigger_type=TriggerType.CRON)
    _refused(_unattended(name, {"tenant": CHILD_A, "id": TASK_ID, "title": "x"}), env)


def test_a_verified_admin_is_not_the_owner(env):
    result = _call(
        "update_task",
        {"tenant": CHILD_A, "id": TASK_ID, "status": "DONE"},
        identity=ADMIN,
        user_id="bob",
        user_role="admin",
    )
    _refused(result, env)


def test_an_admin_may_still_create_a_task_in_a_child(env):
    result = _call(
        "create_task",
        {"tenant": CHILD_A, "title": "x"},
        identity=ADMIN,
        user_id="bob",
        user_role="admin",
    )
    assert "error" not in result, result


# ── Dry run ────────────────────────────────────────────────────────────────


def test_a_dry_run_changes_nothing_and_describes_the_write(env):
    with patch("robothor.engine.runtime.effect_dispatch.invoke") as invoke:
        result = _call(
            "create_task", {"tenant": CHILD_A, "dry_run": True, "title": "Renew the lease"}
        )

    assert env.seen.calls == [], "a dry run must not run the handler"
    invoke.assert_not_called()
    assert result["dry_run"] is True
    assert result["tenant"] == CHILD_A
    assert result["tool"] == "create_task"
    assert result["would"]["title"] == "Renew the lease"
    assert "tenant" not in result["would"] and "dry_run" not in result["would"]


def test_a_dry_run_is_audited_as_a_dry_run(env):
    _call("create_task", {"tenant": CHILD_A, "dry_run": True, "title": "x"})

    env.audit.assert_called_once()
    kw = env.audit.call_args.kwargs
    assert kw["dry_run"] is True
    assert kw["target_tenant"] == CHILD_A
    assert kw["outcome"] == "ok"


def test_a_dry_run_runs_the_same_admission(env):
    _call("create_task", {"tenant": CHILD_A, "dry_run": True, "title": "x"})

    env.rbac.assert_called_once()
    env.runtime.assert_awaited_once()


def test_a_dry_run_is_refused_exactly_where_the_real_call_is(env):
    env.session = _session(trigger_type=TriggerType.CRON)
    _refused(_unattended("update_task", {"tenant": CHILD_A, "dry_run": True, "id": TASK_ID}), env)
    _refused(_call("update_task", {"tenant": "acme-nowhere", "dry_run": True, "id": TASK_ID}), env)


def test_a_dry_run_update_shows_each_field_from_and_to(env):
    seen_scope: list[str | None] = []

    def fake_get_task(task_id: str, tenant_id: str = "", **_: Any) -> dict[str, Any]:
        seen_scope.append(current_tenant_scope())
        assert tenant_id == CHILD_A
        return {"id": task_id, "status": "TODO", "title": "Renew the lease"}

    with patch("robothor.crm.dal.get_task", side_effect=fake_get_task):
        result = _call(
            "update_task", {"tenant": CHILD_A, "dry_run": True, "id": TASK_ID, "status": "DONE"}
        )

    assert env.seen.calls == []
    assert result["changes"] == {"status": {"from": "TODO", "to": "DONE"}}
    assert seen_scope == [CHILD_A], "the current row must be read in the child's scope"


def test_a_dry_run_update_of_a_missing_record_says_so(env):
    with patch("robothor.crm.dal.get_task", return_value=None):
        result = _call(
            "update_task", {"tenant": CHILD_A, "dry_run": True, "id": TASK_ID, "status": "DONE"}
        )

    assert env.seen.calls == []
    assert "error" in result
    assert result["dry_run"] is True


def test_a_dry_run_rejects_a_malformed_id_like_the_real_call(env):
    result = _call("update_task", {"tenant": CHILD_A, "dry_run": True, "id": "not-a-uuid"})

    assert env.seen.calls == []
    assert "error" in result


def test_dry_run_without_a_tenant_is_a_dry_run_in_the_own_tenant(env):
    result = _call("create_task", {"dry_run": True, "title": "x"})

    assert env.seen.calls == [], "dry_run must never fall through to a real write"
    assert result["dry_run"] is True
    assert result["tenant"] == PARENT


# ── The allowlist and the schemas ──────────────────────────────────────────


def test_the_write_allowlists_are_exactly_the_audited_sets():
    from robothor.engine.tools.constants import (
        CROSS_TENANT_READS,
        CROSS_TENANT_WRITES,
        UNATTENDED_CROSS_TENANT_WRITES,
    )

    assert frozenset(WRITES) == CROSS_TENANT_WRITES
    assert frozenset({"create_task", "send_notification"}) == UNATTENDED_CROSS_TENANT_WRITES
    assert UNATTENDED_CROSS_TENANT_WRITES <= CROSS_TENANT_WRITES
    assert not CROSS_TENANT_READS & CROSS_TENANT_WRITES


def test_every_cross_tenant_write_is_refused_to_a_benchmark_by_its_handler():
    from robothor.engine.tools.constants import CROSS_TENANT_WRITES
    from robothor.engine.tools.handlers.crm import _CRM_MUTATING_TOOLS

    assert CROSS_TENANT_WRITES <= _CRM_MUTATING_TOOLS


def test_writes_advertise_tenant_and_dry_run():
    from robothor.engine.tools.constants import CROSS_TENANT_WRITES
    from robothor.engine.tools.registry import builtin_schemas

    schemas = builtin_schemas()
    for name in CROSS_TENANT_WRITES:
        props = schemas[name]["function"]["parameters"]["properties"]
        assert props["tenant"]["type"] == "string", name
        assert props["dry_run"]["type"] == "boolean", name


# ── Post-condition verification reads back where the write landed ─────────


def test_verification_reads_back_in_the_target_tenant(env):
    seen: dict[str, Any] = {}

    async def fake_verify(name: str, args: dict[str, Any], result: Any, ctx: Any) -> Any:
        seen.update(tenant_id=ctx.tenant_id, scope=current_tenant_scope(), args=dict(args))
        return result

    with patch("robothor.engine.tools.verification.verify_tool_result", side_effect=fake_verify):
        _call("create_task", {"tenant": CHILD_A, "title": "Renew the lease"})

    assert seen["tenant_id"] == CHILD_A
    assert seen["scope"] == CHILD_A
    assert "tenant" not in seen["args"]


def test_verification_is_unchanged_without_a_tenant(env):
    seen: dict[str, Any] = {}

    async def fake_verify(name: str, args: dict[str, Any], result: Any, ctx: Any) -> Any:
        seen.update(tenant_id=ctx.tenant_id, scope=current_tenant_scope())
        return result

    with patch("robothor.engine.tools.verification.verify_tool_result", side_effect=fake_verify):
        _call("create_task", {"title": "Renew the lease"})

    assert seen == {"tenant_id": PARENT, "scope": None}
