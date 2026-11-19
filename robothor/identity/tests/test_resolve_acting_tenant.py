"""Webchat identity for an owner acting in a child tenant.

An owner who switches into a child tenant carries a token whose tenant is the
child and whose account lives in the parent. The webchat resolver compared the
account's tenant with the acting one and returned nothing, so the agent lost
the user's name and role for the whole conversation. It now resolves the
account against its home tenant, admitting the acting tenant only when the
home tenant's role may reach it (the rule ``switch-tenant`` enforces).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.identity import resolvers

ACCT = "11111111-1111-1111-1111-111111111111"
PARENT = "acme-parent"
CHILD_A = "acme-child-a"
CHILD_B = "acme-child-b"


@pytest.fixture(autouse=True)
def _clear_identity_cache():
    resolvers.clear_cache()
    yield
    resolvers.clear_cache()


def _account(role: str = "owner", tenant: str = PARENT, status: str = "active") -> dict:
    return {
        "id": ACCT,
        "tenant_id": tenant,
        "status": status,
        "display_name": "Alice",
        "role": role,
        "person_id": "33333333-3333-3333-3333-333333333333",
        "email": "agent@example.com",
    }


def _accessible(tenant_id: str, role: str | None = None) -> tuple[str, ...]:
    if tenant_id == PARENT and role in ("owner", "admin"):
        return (PARENT, CHILD_A, CHILD_B)
    return (tenant_id,)


def _resolve(account: dict | None, acting: str):
    with (
        patch("robothor.identity.resolvers.get_account_by_id", return_value=account),
        patch("robothor.identity.resolvers.resolve_accessible_tenants", side_effect=_accessible),
    ):
        return resolvers.resolve_identity("webchat", ACCT, acting)


def test_owner_acting_in_child_keeps_name_and_role() -> None:
    ctx = _resolve(_account(), CHILD_A)

    assert ctx is not None
    assert ctx.display_name == "Alice"
    assert ctx.role == "owner"
    assert ctx.verified is True
    # The run still acts in the child tenant.
    assert ctx.tenant_id == CHILD_A
    assert ctx.user_account_id == ACCT
    # The person row lives in the home tenant's CRM, not the child's.
    assert ctx.person_id is None


def test_admin_acting_in_child_keeps_name_and_role() -> None:
    ctx = _resolve(_account(role="admin"), CHILD_B)
    assert ctx is not None and ctx.role == "admin"


def test_member_of_parent_is_not_resolved_in_child() -> None:
    assert _resolve(_account(role="member"), CHILD_A) is None


def test_account_from_an_unrelated_tenant_is_not_resolved() -> None:
    assert _resolve(_account(tenant="acme-other"), CHILD_A) is None


def test_inactive_owner_is_not_resolved_in_child() -> None:
    assert _resolve(_account(status="disabled"), CHILD_A) is None


def test_unknown_account_is_not_resolved_in_child() -> None:
    assert _resolve(None, CHILD_A) is None


def test_home_tenant_resolution_is_unchanged() -> None:
    ctx = _resolve(_account(), PARENT)
    assert ctx is not None
    assert ctx.tenant_id == PARENT
    assert ctx.person_id == "33333333-3333-3333-3333-333333333333"


def test_an_owner_acting_in_a_child_is_marked_acting() -> None:
    ctx = _resolve(_account(), CHILD_A)
    assert ctx is not None and ctx.acting is True


def test_an_owner_at_home_is_not_acting() -> None:
    ctx = _resolve(_account(), PARENT)
    assert ctx is not None and ctx.acting is False


def test_the_acting_flag_survives_serialisation() -> None:
    from dataclasses import asdict

    ctx = _resolve(_account(), CHILD_A)
    assert ctx is not None and asdict(ctx)["acting"] is True
