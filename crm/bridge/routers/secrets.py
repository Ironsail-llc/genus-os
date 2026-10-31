"""The Helm's Secrets page: which credentials exist, where, and who reads them.

``GET /api/secrets`` is ``genus secrets status`` for an operator who is not on
the box. The read model is :func:`robothor.secrets.status.status_table`, the
same one the CLI and the doctor use, so the three cannot disagree about what
is configured. What this module owns is the HTTP shape, and one rule above all:

**Nothing value-shaped leaves.** Not the value, not a fingerprint, not the
vault key a row was found under. Each row is built field by field from an
allowlist rather than by dumping the status and deleting the dangerous keys,
so a field added to :class:`~robothor.secrets.status.SecretStatus` later is
absent here until somebody decides it is safe. The page answers "which
credentials, where, when last changed, read by what"; a fingerprint answers
"is it the same one", which is a terminal question (``genus secrets status``).

``readers`` are the restart units the credential's settings declaration names
(:func:`robothor.settings.operator.units_for`, the rule ``genus config``
uses), or :data:`~robothor.settings.operator.DEFAULT_UNITS` for a credential
nothing declares: naming too few readers is how a rotation reports done while
a service is still holding the old value.

Root-operator only (``require_root_operator``): the table is the platform
tenant's vault and the appliance's environment, which no child tenant's admin
may read, exactly as Settings. ``Cache-Control: no-store``, and a plain
``def`` because the table reads the vault through synchronous psycopg2
(``crm/bridge/tests/test_route_concurrency.py``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from robothor.secrets.status import SecretStatus, status_table
from robothor.settings.operator import DEFAULT_UNITS, units_for
from robothor.settings.registry import field_index
from routers._operator import PLATFORM_TENANT, require_root_operator

router = APIRouter(prefix="/api/secrets", tags=["secrets"])


def _source(row: SecretStatus) -> str:
    """Which stores hold a value: ``env``, ``vault``, ``both`` or ``none``."""
    if row.in_env and row.in_vault:
        return "both"
    if row.in_env:
        return "env"
    if row.in_vault:
        return "vault"
    return "none"


def _public(row: SecretStatus, index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    declared = index.get(row.name)
    readers = units_for(declared) if declared else DEFAULT_UNITS
    return {
        "name": row.name,
        "configured": row.configured,
        "source": _source(row),
        "updated_at": row.updated_at,
        "readers": list(readers),
        "shadowed": row.shadowed,
        "shadowed_by": row.shadowed_by,
        "env_name": declared["env"] if declared else row.name,
        # False when the vault could not be read at all. Without it a
        # vault-only credential reads as "not configured" during an outage.
        "vault_readable": row.vault_readable,
    }


@router.get("")
def list_secrets(request: Request) -> JSONResponse:
    try:
        require_root_operator(request)
    except HTTPException as refused:
        # The refusal is no-store too, like the list (``runs.py`` does the same).
        raise HTTPException(
            status_code=refused.status_code,
            detail=refused.detail,
            headers={"Cache-Control": "no-store"},
        ) from None
    index = field_index()
    rows = [_public(row, index) for row in status_table(tenant_id=PLATFORM_TENANT)]
    return JSONResponse({"secrets": rows}, headers={"Cache-Control": "no-store"})
