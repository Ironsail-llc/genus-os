"""A parent's owner switched into a child is not the child's instance owner.

The webchat resolver keeps an owner's name and role when they switch into a
child tenant, so the conversation still knows who they are. That identity is
marked ``acting``: the child engine's owner-only powers -- ``config_get`` /
``config_set`` (``apply`` restarts services), the masked ``.env`` read and
host execution -- belong to the child's own owner. Reach is unchanged: the
child and the child's descendants, never the parent or a sibling.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.models import TriggerType
from robothor.engine.tools.dispatch import ToolContext
from robothor.identity import IdentityContext

PARENT, CHILD_A, CHILD_B = "acme-parent", "acme-child-a", "acme-child-b"
GRANDCHILD = "acme-child-a-east"


def _owner(tenant: str, *, acting: bool = False) -> IdentityContext:
    return IdentityContext(
        tenant_id=tenant,
        channel="webchat",
        identifier="alice",
        verified=True,
        display_name="Alice",
        role="owner",
        acting=acting,
    )


def _acting() -> IdentityContext:
    return _owner(CHILD_A, acting=True)


def _ctx(identity: IdentityContext, tenant: str) -> ToolContext:
    return ToolContext(
        agent_id="main", run_id="run-1", user_id="alice", tenant_id=tenant, identity=identity
    )


@pytest.fixture
def top_level():
    session = SimpleNamespace(
        readonly_mode=False, run=SimpleNamespace(parent_run_id=None, tools_provided=["exec"])
    )
    with patch("robothor.engine.session_registry.lookup", return_value=session):
        yield session


def test_an_acting_owner_is_not_the_verified_owner_of_the_child(top_level):
    from robothor.engine.owner_gate import is_verified_owner

    assert is_verified_owner(_ctx(_acting(), CHILD_A)) is False


def test_the_childs_own_owner_and_the_parents_owner_at_home_still_are(top_level):
    from robothor.engine.owner_gate import is_verified_owner

    assert is_verified_owner(_ctx(_owner(CHILD_A), CHILD_A)) is True
    assert is_verified_owner(_ctx(_owner(PARENT), PARENT)) is True


@pytest.mark.parametrize("tool", ["config_get", "config_set"])
def test_config_tools_refuse_an_acting_owner(top_level, tool):
    from robothor.engine.tools.handlers.config_tools import HANDLERS

    out = asyncio.run(
        HANDLERS[tool](
            {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "4", "apply": True},
            _ctx(_acting(), CHILD_A),
        )
    )
    assert out.get("denied_by") == "owner_role", out


def test_host_execution_refuses_an_acting_owner(top_level):
    from robothor.engine import host_execution

    assert host_execution.eligible(_ctx(_owner(CHILD_A), CHILD_A)) is True
    assert host_execution.eligible(_ctx(_acting(), CHILD_A)) is False


def test_the_current_user_block_still_names_them_and_their_role():
    block = _acting().prompt_block()

    assert "Alice" in block
    assert "Role: owner" in block
    assert "acting" not in block.lower()


def _tree(tenant_id: str, user_role: str | None = None, **_: Any) -> tuple[str, ...]:
    if user_role not in ("owner", "admin"):
        return (tenant_id,)
    return {
        PARENT: (PARENT, CHILD_A, CHILD_B, GRANDCHILD),
        CHILD_A: (CHILD_A, GRANDCHILD),
    }.get(tenant_id, (tenant_id,))


def test_an_acting_owner_reaches_the_childs_descendants_only():
    from robothor.engine.run_identity import attach_tenant_access

    run = SimpleNamespace(
        agent_id="main",
        user_role="owner",
        parent_run_id=None,
        nesting_depth=0,
        trigger_type=TriggerType.WEBCHAT,
        is_benchmark=False,
        accessible_tenant_ids=(),
    )
    with patch("robothor.engine.permissions.resolve_accessible_tenants", side_effect=_tree):
        asyncio.run(attach_tenant_access(run, CHILD_A, _acting()))

    assert run.accessible_tenant_ids == (CHILD_A, GRANDCHILD)


def test_an_acting_owner_cannot_make_an_owner_only_write_below_the_child(top_level):
    """Cross-tenant owner-only writes use the same gate; task and notification
    creation, open to any run that reaches the tenant, still work."""
    from robothor.engine.tools import cross_tenant

    ctx = replace(_ctx(_acting(), CHILD_A), accessible_tenant_ids=(CHILD_A, GRANDCHILD))
    registry = SimpleNamespace(
        get_adapter_route=lambda name: None, declared_parameters=lambda name: frozenset()
    )
    with (
        patch("robothor.engine.tools.get_registry", return_value=registry),
        patch("robothor.engine.tools.cross_tenant.audit_cross_tenant"),
        patch("robothor.engine.tools.cross_tenant.is_top_level_run", return_value=True),
    ):
        refused = cross_tenant.admit("update_task", {"tenant": GRANDCHILD, "id": "t"}, ctx)
        allowed = cross_tenant.admit("create_task", {"tenant": GRANDCHILD, "title": "x"}, ctx)

    assert isinstance(refused, dict) and refused.get("refused") is True
    assert isinstance(allowed, cross_tenant.Crossing) and allowed.target == GRANDCHILD
