"""The acting-tenant rule every memory entry point applies.

A caller that names a tenant acts as that tenant. A caller that names none
gets the deployment's default tenant only when the deployment genuinely has one
tenant; with more than one it is refused, because picking one would write (or
read) another tenant's memory.
"""

from __future__ import annotations

import pytest

from robothor.constants import DEFAULT_TENANT


def _tenants(monkeypatch, ids):
    import robothor.crm.dal as dal

    monkeypatch.setattr(
        dal, "list_tenants", lambda parent_id=None, active_only=True: [{"id": i} for i in ids]
    )


def test_an_explicit_tenant_is_the_acting_tenant(monkeypatch):
    from robothor.tenancy.acting import resolve_acting_tenant

    _tenants(monkeypatch, ["tenant-a", "tenant-b"])
    assert resolve_acting_tenant("tenant-a", surface="test") == "tenant-a"


def test_surrounding_whitespace_is_not_part_of_the_tenant(monkeypatch):
    from robothor.tenancy.acting import resolve_acting_tenant

    _tenants(monkeypatch, ["tenant-a", "tenant-b"])
    assert resolve_acting_tenant("  tenant-a ", surface="test") == "tenant-a"


@pytest.mark.parametrize("ids", [[], ["default"]])
def test_a_single_tenant_deployment_keeps_its_default(monkeypatch, ids):
    from robothor.tenancy.acting import resolve_acting_tenant

    _tenants(monkeypatch, ids)
    assert resolve_acting_tenant("", surface="test") == DEFAULT_TENANT
    assert resolve_acting_tenant(None, surface="test") == DEFAULT_TENANT


def test_no_tenant_in_a_multi_tenant_deployment_is_refused(monkeypatch):
    from robothor.tenancy.acting import TenantRequiredError, resolve_acting_tenant

    _tenants(monkeypatch, ["tenant-a", "tenant-b"])
    with pytest.raises(TenantRequiredError) as refused:
        resolve_acting_tenant("", surface="orchestrator /ingest")
    message = str(refused.value)
    assert "orchestrator /ingest" in message
    assert "tenant" in message.lower()


def test_an_unreadable_tenant_table_is_refused_not_defaulted(monkeypatch):
    import robothor.crm.dal as dal
    from robothor.tenancy.acting import TenantRequiredError, resolve_acting_tenant

    def broken(parent_id=None, active_only=True):
        raise ConnectionError("synthetic: database unavailable")

    monkeypatch.setattr(dal, "list_tenants", broken)
    with pytest.raises(TenantRequiredError):
        resolve_acting_tenant("", surface="test")


def test_the_refusal_is_a_value_error_so_existing_400_mappings_hold():
    from robothor.tenancy.acting import TenantRequiredError

    assert issubclass(TenantRequiredError, ValueError)
