"""Email domains a tenant claims for SSO just-in-time provisioning.

A tenant lists the domains in ``crm_tenants.settings -> 'sso_domains'`` (a JSON
list of lower-case DNS names). A brand-new SSO user whose address is in one of
them is provisioned into that tenant rather than the default one — see
``robothor.auth.accounts.tenant_for_email``.

Exactly one tenant may claim a domain. The writer (``dal.update_tenant`` /
``create_tenant``) enforces that; the reader still fails safe to the default
tenant if a hand-edited row ever breaks it.
"""

from __future__ import annotations

import re
from typing import Any

#: One DNS label: letters, digits and inner hyphens, 1-63 characters.
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
#: Two or more labels, ending in a letter-led TLD. No wildcards, no ports, no
#: scheme, no "@": the value is compared to the part of an address after "@".
_DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+[a-z][a-z0-9-]*[a-z0-9]$")


class InvalidSsoDomainError(ValueError):
    """A claimed SSO domain is not a plain DNS name, or the list is malformed."""


class SsoDomainConflictError(ValueError):
    """Another tenant already claims this SSO domain."""

    def __init__(self, domain: str, tenant_id: str) -> None:
        super().__init__(f"domain {domain!r} is already claimed by tenant {tenant_id!r}")
        self.domain = domain
        self.tenant_id = tenant_id


def normalize_sso_domains(value: Any) -> list[str]:
    """Validate a claimed-domain list and return it lower-cased and de-duplicated."""
    if not isinstance(value, list):
        raise InvalidSsoDomainError("sso_domains must be a list of domain names")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise InvalidSsoDomainError("sso_domains must be a list of domain names")
        domain = item.strip().lower()
        if len(domain) > 253 or not _DOMAIN_RE.match(domain):
            raise InvalidSsoDomainError(f"{item!r} is not a domain name")
        if domain not in out:
            out.append(domain)
    return out


def sso_domains_of(tenant: dict[str, Any]) -> list[str]:
    """The domains a tenant row (API shape) claims; ``[]`` if none or malformed."""
    settings = tenant.get("settings") or {}
    if not isinstance(settings, dict):
        return []
    try:
        return normalize_sso_domains(settings.get("sso_domains", []))
    except InvalidSsoDomainError:
        return []
