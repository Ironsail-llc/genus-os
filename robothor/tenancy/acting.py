"""The tenant a memory entry point acts as.

Every way of reaching memory -- the engine's tools, deep_reason, the MCP server,
the orchestrator's HTTP endpoints -- must read and write as the caller's tenant.
The memory functions themselves fall back to ``DEFAULT_TENANT`` when handed an
empty tenant, which is right for a deployment with one tenant and silently
wrong for any other: the caller's reads and writes land in someone else's
memory, and nothing reports it.

So the entry point decides, with one rule:

* a caller that names a tenant acts as that tenant;
* a caller that names none acts as ``DEFAULT_TENANT`` only when the deployment
  has at most one active tenant (a fresh or genuinely single-tenant install,
  which therefore keeps working unchanged);
* otherwise -- several active tenants, or a tenant table that cannot be read --
  the call is refused with :class:`TenantRequiredError`, never defaulted.
"""

from __future__ import annotations

import logging

from robothor.constants import DEFAULT_TENANT

logger = logging.getLogger(__name__)


class TenantRequiredError(ValueError):
    """A memory call named no tenant where the deployment cannot supply one.

    A ``ValueError`` so the HTTP surfaces' existing bad-request mapping holds.
    """


def resolve_acting_tenant(explicit: str | None, *, surface: str) -> str:
    """The tenant this call acts as, or :class:`TenantRequiredError`.

    ``surface`` names the entry point in the refusal, so the caller can tell
    which door turned it away.
    """
    named = (explicit or "").strip()
    if named:
        return named

    try:
        from robothor.crm.dal import list_tenants

        active = list_tenants(active_only=True)
    except Exception as error:  # noqa: BLE001 - reported as the refusal below
        logger.warning(
            "%s: no tenant named and the tenant table is unreadable (%s)",
            surface,
            type(error).__name__,
        )
        raise TenantRequiredError(
            f"{surface}: no tenant was named and the deployment's tenants could not be "
            "read, so the call was refused rather than run as the default tenant."
        ) from error

    if len(active) <= 1:
        return DEFAULT_TENANT
    raise TenantRequiredError(
        f"{surface}: no tenant was named and this deployment has {len(active)} active "
        "tenants. Name the tenant the call acts as (see the entry point's docs); it is "
        "refused rather than run as the default tenant."
    )
