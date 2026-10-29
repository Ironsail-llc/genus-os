"""``tenant_for_email``: which tenant a brand-new SSO user is provisioned into.

The answer comes from the tenants' claimed ``settings.sso_domains``. Anything
unclear — no claim, two claims, an unreadable tenant table — lands on the
default tenant, because this runs on the sign-in path and must never turn a
configuration mistake into a failed login.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from robothor.auth import accounts
from robothor.constants import DEFAULT_TENANT

TENANTS = [
    {"id": "acme-parent", "settings": {"sso_domains": ["example.com"]}, "active": True},
    {"id": "acme-child-a", "settings": {"sso_domains": ["child.example"]}, "active": True},
    {"id": "acme-child-b", "settings": {}, "active": True},
]


def test_a_claimed_domain_maps_to_its_tenant() -> None:
    with patch("robothor.crm.dal.list_tenants", return_value=TENANTS) as lt:
        assert accounts.tenant_for_email("alice@child.example") == "acme-child-a"
    # Only active tenants may receive new accounts.
    assert lt.call_args.kwargs.get("active_only", True) is True


def test_an_unclaimed_domain_maps_to_the_default_tenant() -> None:
    with patch("robothor.crm.dal.list_tenants", return_value=TENANTS):
        assert accounts.tenant_for_email("bob@unclaimed.example") == DEFAULT_TENANT


def test_the_domain_is_matched_case_insensitively() -> None:
    with patch("robothor.crm.dal.list_tenants", return_value=TENANTS):
        assert accounts.tenant_for_email("Alice@EXAMPLE.COM") == "acme-parent"


def test_a_subdomain_is_not_its_parent_domain() -> None:
    with patch("robothor.crm.dal.list_tenants", return_value=TENANTS):
        assert accounts.tenant_for_email("alice@mail.example.com") == DEFAULT_TENANT


def test_two_tenants_claiming_one_domain_fail_safe_to_default(caplog) -> None:
    dup = [
        {"id": "acme-parent", "settings": {"sso_domains": ["example.com"]}},
        {"id": "acme-child-a", "settings": {"sso_domains": ["example.com"]}},
    ]
    with (
        patch("robothor.crm.dal.list_tenants", return_value=dup),
        caplog.at_level(logging.ERROR, logger="robothor.auth.accounts"),
    ):
        assert accounts.tenant_for_email("alice@example.com") == DEFAULT_TENANT
    assert any(
        r.levelno == logging.ERROR and "example.com" in r.getMessage() for r in caplog.records
    )


def test_an_address_without_a_domain_maps_to_default() -> None:
    with patch("robothor.crm.dal.list_tenants", return_value=TENANTS):
        assert accounts.tenant_for_email("not-an-address") == DEFAULT_TENANT
        assert accounts.tenant_for_email("") == DEFAULT_TENANT


def test_an_unreadable_tenant_table_maps_to_default() -> None:
    with patch("robothor.crm.dal.list_tenants", side_effect=RuntimeError("db down")):
        assert accounts.tenant_for_email("alice@example.com") == DEFAULT_TENANT
