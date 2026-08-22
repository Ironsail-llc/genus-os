"""Integration routes — contact resolution, webhooks, vault."""

from __future__ import annotations

import logging

from deps import get_tenant_id
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from models import (  # noqa: TC002 — used at runtime by FastAPI
    LogInteractionRequest,
    ResolveContactRequest,
)

from robothor.audit.logger import log_event
from robothor.events.bus import publish

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
    from robothor.crm.dal import (
        create_conversation,
        get_conversations_for_contact,
        send_message,
    )
    from robothor.crm.dal import (
        resolve_contact as _resolve,
    )

    channel_id = body.channel_identifier or body.contact_name
    resolved = _resolve(body.channel, channel_id, body.contact_name, tenant_id=tenant_id)
    person_id = resolved.get("person_id")
    message_persisted: bool | None = None
    if person_id and body.content_summary:
        convos = get_conversations_for_contact(str(person_id), tenant_id=tenant_id)
        convo_id = convos[0].get("id") if convos else None
        if not convo_id:
            convo = create_conversation(str(person_id), tenant_id=tenant_id)
            convo_id = convo.get("id") if convo else None
        if convo_id:
            msg_type = "incoming" if body.direction == "incoming" else "outgoing"
            # The result is CHECKED, not discarded. Between 2026-04-08 and
            # 2026-08-22 this call failed on every invocation (a uuid into an
            # integer PK) and this endpoint still answered 200 "ok", so four and
            # a half months of messages went missing with nothing to show for it.
            message_persisted = (
                send_message(convo_id, body.content_summary, msg_type, tenant_id=tenant_id)
                is not None
            )
            if not message_persisted:
                logger.warning(
                    "log_interaction: message NOT persisted for conversation %s "
                    "(contact=%s channel=%s) — the interaction was accepted but "
                    "the message row was not written",
                    convo_id,
                    body.contact_name,
                    body.channel,
                )

    log_event(
        "ipc.interaction",
        f"log_interaction: {body.contact_name} via {body.channel}",
        category="bridge",
        source_channel=body.channel,
        target=f"person:{person_id}" if person_id else None,
        details={
            "contact_name": body.contact_name,
            "channel": body.channel,
            "direction": body.direction,
            "resolved": bool(person_id),
            "message_persisted": message_persisted,
            "tenant_id": tenant_id,
        },
    )
    publish(
        "crm",
        "ipc.interaction",
        {
            "contact_name": body.contact_name,
            "channel": body.channel,
            "direction": body.direction,
            "person_id": person_id,
        },
        source="bridge",
        tenant_id=tenant_id,
    )
    return {
        "status": "ok",
        "contact": body.contact_name,
        "resolved": bool(person_id),
        # None = no message was attempted; False = attempted and NOT written.
        "message_persisted": message_persisted,
    }


# ─── Vault (PostgreSQL-backed) ────────────────────────────────────────────


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


@router.get("/api/vault/get")
def api_vault_get(
    key: str = Query(..., description="Secret key"),
    tenant_id: str = Depends(get_tenant_id),
):
    try:
        from robothor.vault import get as vault_get

        value = vault_get(key, tenant_id=tenant_id)
        if value is not None:
            return {"key": key, "value": value}
        return JSONResponse({"error": f"No secret with key '{key}'"}, status_code=404)
    except Exception:
        logger.exception("vault get failed")
        return JSONResponse({"error": "Internal server error"}, status_code=500)
