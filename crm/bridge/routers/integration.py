"""Integration routes — contact resolution, webhooks, vault."""

from __future__ import annotations

import logging

from deps import get_tenant_id
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from models import (  # noqa: TC002 — used at runtime by FastAPI
    LogInteractionRequest,
    ResolveContactRequest,
)

from robothor.engine.sanitize import sanitize_log
from routers._audit import audited

logger = logging.getLogger(__name__)

router = APIRouter(tags=["integration"])


# ─── Contact Resolution ──────────────────────────────────────────────────


@router.post("/resolve-contact")
def resolve_contact(
    body: ResolveContactRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    if not body.channel or not body.identifier:
        return JSONResponse({"error": "channel and identifier required"}, status_code=400)

    from robothor.crm.dal import resolve_contact as _resolve

    result = _resolve(body.channel, body.identifier, body.name, tenant_id=tenant_id)
    for k, v in result.items():
        if hasattr(v, "isoformat"):
            result[k] = v.isoformat()
    return result


@router.get("/timeline/{identifier}")
def timeline(identifier: str, tenant_id: str = Depends(get_tenant_id)):
    from robothor.crm.dal import get_timeline

    result = get_timeline(identifier, tenant_id=tenant_id)
    # The legacy DAL helper historically queried identifier mappings without a
    # tenant predicate.  Do not expose those rows across the bridge boundary.
    result["mappings"] = [
        mapping
        for mapping in result.get("mappings", [])
        if str(mapping.get("tenant_id", tenant_id)) == tenant_id
    ]
    return result


# ─── Webhooks ────────────────────────────────────────────────────────────


@router.post("/log-interaction")
def log_interaction(
    body: LogInteractionRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    # The core lives in the platform so in-process callers (the Microsoft 365
    # mail ingest) write exactly what this endpoint writes.
    from robothor.crm.interactions import log_interaction as _log_interaction

    return _log_interaction(
        contact_name=body.contact_name,
        channel=body.channel,
        direction=body.direction,
        content_summary=body.content_summary,
        channel_identifier=body.channel_identifier,
        tenant_id=tenant_id,
        source="bridge",
    )


# ─── Vault (PostgreSQL-backed) ────────────────────────────────────────────
#
# Secrets are WRITE-ONLY from any human surface.  ``/api/vault/get`` used to
# return the decrypted value to any owner/admin session, and the Helm's BFF
# proxy (``app/src/app/api/bridge/[...path]/route.ts``) forwards every bridge
# path — so a signed-in browser, an XSS on the dashboard, or a stolen session
# cookie was a credential dump.  Only a verified *service* token may retrieve
# a value; humans administer secrets with ``robothor vault`` on the appliance.
#
# ``/api/vault/list`` stays reachable to operators because ``robothor.vault.list``
# returns key NAMES only (``list[str]``) — no value ever crosses it.

_WRITE_ONLY_ERROR = (
    "vault values are write-only from the UI: a human session cannot read a secret "
    "value. Set or rotate secrets with `robothor vault set` on the appliance; only "
    "a service token may retrieve one."
)


@router.get("/api/vault/list")
def api_vault_list(
    category: str | None = None,
    tenant_id: str = Depends(get_tenant_id),
):
    try:
        from robothor.vault import list as vault_list

        keys = vault_list(category=category, tenant_id=tenant_id)
        return {"keys": keys}
    except Exception:
        logger.exception("vault list failed")
        return JSONResponse({"error": "Internal server error"}, status_code=500)


# ``response_model=None``: the return annotation is a union with JSONResponse,
# which FastAPI would otherwise try to turn into a Pydantic response field.
@router.get("/api/vault/get", response_model=None)
def api_vault_get(
    request: Request,
    key: str = Query(..., description="Secret key"),
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, str] | JSONResponse:
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "is_service", False):
        # Refuse BEFORE the vault is consulted: no decrypt, no plaintext in
        # this process, nothing to leak through a log or an exception.
        #
        # The key is caller-controlled and lands in an audit row an operator
        # reads back, so it is escaped and bounded before it is recorded —
        # an unbounded raw value is a log-injection vector, not an identifier.
        audited(
            request,
            "vault.read.denied",
            action=sanitize_log(key)[:200],
            status="denied",
        )
        return JSONResponse({"error": _WRITE_ONLY_ERROR}, status_code=403)
    try:
        from robothor.vault import get as vault_get

        value = vault_get(key, tenant_id=tenant_id)
        if value is not None:
            return {"key": key, "value": value}
        return JSONResponse({"error": f"No secret with key '{key}'"}, status_code=404)
    except Exception:
        logger.exception("vault get failed")
        return JSONResponse({"error": "Internal server error"}, status_code=500)
