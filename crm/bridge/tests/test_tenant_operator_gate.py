"""The tenant-scoped operator gate.

``require_operator`` is platform-only: owner/admin AND the platform tenant. The
Inbox needs a narrower, tenant-local authority instead — an owner or admin of
the tenant the session acts in may decide THAT tenant's approvals, questions
and proposed actions, and nothing of anyone else's. These tests pin the four
conditions of that gate, and that the tenant it returns is the verified
session's, never one a caller could name.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from routers._operator import PLATFORM_TENANT, require_tenant_operator


def _request_with_auth(auth):
    return Request({"type": "http", "state": {"auth": auth}})


def _auth(*, role="admin", tenant="acme-child-a", is_service=False, actor="alice"):
    return SimpleNamespace(role=role, tenant_id=tenant, is_service=is_service, actor_id=actor)


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_an_operator_of_a_child_tenant_passes_with_that_tenant(role):
    who = require_tenant_operator(_request_with_auth(_auth(role=role)))
    assert who.actor == "operator:alice"
    assert who.tenant_id == "acme-child-a"


def test_the_platform_operator_still_passes_with_the_platform_tenant():
    who = require_tenant_operator(_request_with_auth(_auth(role="owner", tenant=PLATFORM_TENANT)))
    assert who.actor == "operator:alice"
    assert who.tenant_id == PLATFORM_TENANT


def test_missing_auth_is_rejected():
    with pytest.raises(HTTPException) as e:
        require_tenant_operator(_request_with_auth(None))
    assert e.value.status_code == 403


def test_a_service_token_is_rejected_even_as_admin():
    with pytest.raises(HTTPException) as e:
        require_tenant_operator(_request_with_auth(_auth(is_service=True)))
    assert e.value.status_code == 403


@pytest.mark.parametrize("role", ["member", "user", "viewer", "auditor", ""])
def test_non_operator_roles_of_the_tenant_are_rejected(role):
    with pytest.raises(HTTPException) as e:
        require_tenant_operator(_request_with_auth(_auth(role=role)))
    assert e.value.status_code == 403


@pytest.mark.parametrize("tenant", ["", None, "   "])
def test_a_session_with_no_tenant_is_rejected(tenant):
    with pytest.raises(HTTPException) as e:
        require_tenant_operator(_request_with_auth(_auth(tenant=tenant)))
    assert e.value.status_code == 403
