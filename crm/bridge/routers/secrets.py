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

Operator-only (``require_operator``), ``Cache-Control: no-store``, and a plain
``def`` because the table reads the vault through synchronous psycopg2
(``crm/bridge/tests/test_route_concurrency.py``).

``DELETE /api/secrets/{name}`` removes the VAULT row behind a listed name. It
takes the environment name the list shows, never a vault key -- the list sends
none, so the page has none to send back -- and finds the row the accessor would
serve. Bootstrap credentials are refused (409), as the vault tools refuse them;
a name with no vault row is 404. Audited as ``secret.delete`` with the name
only, then the engine is told to reload its cached readers.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from robothor.secrets.classification import is_bootstrap
from robothor.secrets.status import SecretStatus, status_for_name, status_table
from robothor.settings.operator import DEFAULT_UNITS, units_for
from robothor.settings.registry import field_index
from routers._audit import audited
from routers._operator import PLATFORM_TENANT, require_operator

logger = logging.getLogger(__name__)

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


_NO_STORE = {"Cache-Control": "no-store"}


def _refuse(status_code: int, detail: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers=_NO_STORE)


def _require_operator_no_store(request: Request) -> str:
    try:
        return require_operator(request)
    except HTTPException as refused:
        # The refusal is no-store too, like the list (``runs.py`` does the same).
        raise _refuse(refused.status_code, refused.detail) from None


def _vault_delete(key: str, *, tenant_id: str) -> bool:
    import robothor.vault as vault

    return vault.delete(key, tenant_id=tenant_id)


def _reload_engine() -> None:
    """Best effort: the engine's own readers pick the change up within seconds
    anyway (the accessor's cache TTL), but a provider key republished into the
    engine's environment needs the reload."""
    from robothor.engine_control import control_request

    try:
        control_request("POST", "/api/admin/secrets/reload")
    except Exception as exc:  # noqa: BLE001 - the delete succeeded
        logger.warning(
            "secrets: removed a vault row but the engine reload failed (%s)", type(exc).__name__
        )


@router.get("")
def list_secrets(request: Request) -> JSONResponse:
    _require_operator_no_store(request)
    index = field_index()
    rows = [_public(row, index) for row in status_table(tenant_id=PLATFORM_TENANT)]
    return JSONResponse({"secrets": rows}, headers={"Cache-Control": "no-store"})


@router.delete("/{name}")
def delete_secret(name: str, request: Request) -> JSONResponse:
    require_operator(request)
    if is_bootstrap(name):
        raise _refuse(
            409,
            f"{name} is a bootstrap credential: it brings this instance up, so it is "
            "changed in the instance's secrets file, not removed here.",
        )
    row = status_for_name(name, tenant_id=PLATFORM_TENANT)
    if not row.in_vault or not row.vault_key:
        raise _refuse(404, f"{name} has no vault row to remove")
    deleted = _vault_delete(row.vault_key, tenant_id=PLATFORM_TENANT)
    audited(request, "secret.delete", action=name, status="ok" if deleted else "error")
    if deleted:
        _reload_engine()
    return JSONResponse(
        {
            "name": name,
            "deleted": bool(deleted),
            # The environment may still hold a copy, which readers are now served.
            "still_served_from_env": bool(row.in_env),
        },
        headers=_NO_STORE,
    )
