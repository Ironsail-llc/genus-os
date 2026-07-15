"""The operator gate is the single authorization primitive for the whole Helm.
It must reject agents, non-operator humans, and cross-tenant operators identically
whether it guards Controls (Phase 1) or the accounting APIs (Phase 2)."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from routers._operator import OPERATOR_ROLES, PLATFORM_TENANT, require_operator


def _request_with_auth(auth):
    scope = {"type": "http", "state": {"auth": auth}}
    return Request(scope)


def _auth(*, role="owner", tenant=None, is_service=False, actor="u1"):
    tenant = PLATFORM_TENANT if tenant is None else tenant
    return SimpleNamespace(role=role, tenant_id=tenant, is_service=is_service, actor_id=actor)


def test_platform_operator_passes():
    assert require_operator(_request_with_auth(_auth())) == "operator:u1"


def test_missing_auth_is_rejected():
    with pytest.raises(HTTPException) as e:
        require_operator(_request_with_auth(None))
    assert e.value.status_code == 403


def test_service_token_is_rejected():
    with pytest.raises(HTTPException) as e:
        require_operator(_request_with_auth(_auth(is_service=True)))
    assert e.value.status_code == 403


@pytest.mark.parametrize("role", ["member", "user", "viewer", "auditor"])
def test_non_operator_human_roles_rejected(role):
    with pytest.raises(HTTPException):
        require_operator(_request_with_auth(_auth(role=role)))


def test_operator_of_another_tenant_rejected():
    with pytest.raises(HTTPException):
        require_operator(_request_with_auth(_auth(tenant="some-other-tenant")))


def test_operator_roles_are_owner_and_admin():
    assert frozenset({"owner", "admin"}) == OPERATOR_ROLES
