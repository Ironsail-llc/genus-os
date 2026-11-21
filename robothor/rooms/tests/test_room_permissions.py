# ruff: noqa: F811 - pytest fixtures are imported and requested by name
"""G6: the default role matrix for the workspace tools, and tuning it.

Against the real database-backed evaluator (``check_tool_permission``) and the
seed of migration 169, which is why it lives here and not under
``robothor/engine/tests/``, whose conftest replaces the evaluator with a stub.
Members and users may use ``room_*`` but not ``room_admin_*``; viewers may list
and read; owners and admins everything. A tenant row revokes a tool for a
role, and a per-user row overrides both.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from robothor.engine.tools.handlers import rooms as room_tools
from robothor.rooms.tests.conftest import committed_connection, rooms_db  # noqa: F401
from robothor.rooms.tests.staff import staff_db  # noqa: F401

ROOM_TOOL_NAMES = set(room_tools.HANDLERS)


@pytest.fixture
def rbac(staff_db, monkeypatch):
    from robothor.db import connection
    from robothor.engine import permissions

    monkeypatch.setattr(connection, "get_connection", committed_connection)
    permissions.invalidate_permission_cache()
    yield staff_db
    permissions.invalidate_permission_cache()


def _denied(role: str, tenant: str, tool: str, user_id: str | None = None) -> bool:
    from robothor.engine.permissions import check_tool_permission

    return check_tool_permission(role, tenant, tool, user_id=user_id) is not None


@pytest.mark.integration
@pytest.mark.parametrize("role", ["member", "user"])
def test_staff_may_use_the_room_tools_but_not_the_admin_ones(rbac, role):
    for tool in sorted(ROOM_TOOL_NAMES - {"room_admin_reassign"}):
        assert not _denied(role, rbac.tenant, tool), tool
    assert _denied(role, rbac.tenant, "room_admin_reassign")


@pytest.mark.integration
def test_a_viewer_may_only_list_and_read(rbac):
    for tool in sorted(ROOM_TOOL_NAMES):
        assert _denied("viewer", rbac.tenant, tool) is (tool not in {"room_list", "room_read"})


@pytest.mark.integration
@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_may_administer(rbac, role):
    for tool in sorted(ROOM_TOOL_NAMES):
        assert not _denied(role, rbac.tenant, tool), tool


@pytest.mark.integration
def test_revoking_room_create_for_members_blocks_it_and_a_user_override_restores_it(rbac):
    from robothor.engine import permissions

    db = rbac
    db.cur.execute(
        "INSERT INTO role_permissions (tenant_id, role, tool_pattern, access) "
        "VALUES (%s, 'member', 'room_create', 'deny')",
        (db.tenant,),
    )
    permissions.invalidate_permission_cache()

    assert _denied("member", db.tenant, "room_create", db.alice)
    assert not _denied("member", db.tenant, "room_read", db.alice)
    assert not _denied("member", db.other, "room_create")

    db.cur.execute(
        "INSERT INTO user_permissions (tenant_id, user_id, tool_pattern, access) "
        "VALUES (%s, %s, 'room_create', 'allow')",
        (db.tenant, db.alice),
    )
    permissions.invalidate_permission_cache()

    assert not _denied("member", db.tenant, "room_create", db.alice)
    assert _denied("member", db.tenant, "room_create", db.bob)

    db.cur.execute(
        "DELETE FROM role_permissions WHERE tenant_id = %s AND tool_pattern = 'room_create'",
        (db.tenant,),
    )
    permissions.invalidate_permission_cache()
    assert not _denied("member", db.tenant, "room_create", db.bob)


@pytest.mark.integration
def test_the_dispatch_gate_refuses_a_viewer_before_the_handler(rbac, monkeypatch):
    from robothor.engine.tools import dispatch

    called: list[Any] = []

    async def spy(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        called.append(args)
        return {}

    monkeypatch.setitem(room_tools.HANDLERS, "room_create", spy)
    refusal = asyncio.run(
        dispatch._refuse_before_handler(
            "room_create",
            agent_id="main",
            tenant_id=rbac.tenant,
            user_id=rbac.vera,
            user_role="viewer",
        )
    )
    assert refusal is not None and called == []
