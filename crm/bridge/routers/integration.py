"""Integration routes — contact resolution, webhooks, vault."""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from models import (  # noqa: TC002 — used at runtime by FastAPI
    LogInteractionRequest,
    ResolveContactRequest,
)

from robothor.audit.logger import log_event
from robothor.events.bus import publish

router = APIRouter(tags=["integration"])


# ─── Contact Resolution ──────────────────────────────────────────────────


@router.post("/resolve-contact")
async def resolve_contact(body: ResolveContactRequest):
    if not body.channel or not body.identifier:
        return JSONResponse({"error": "channel and identifier required"}, status_code=400)

    from robothor.crm.dal import resolve_contact as _resolve

    result = _resolve(body.channel, body.identifier, body.name)
    for k, v in result.items():
        if hasattr(v, "isoformat"):
            result[k] = v.isoformat()
    return result


@router.get("/timeline/{identifier}")
async def timeline(identifier: str):
    from robothor.crm.dal import get_timeline

    return get_timeline(identifier)


# ─── Webhooks ────────────────────────────────────────────────────────────


@router.post("/log-interaction")
async def log_interaction(body: LogInteractionRequest):
    from robothor.crm.dal import (
        create_conversation,
        get_conversations_for_contact,
        send_message,
    )
    from robothor.crm.dal import (
        resolve_contact as _resolve,
    )

    channel_id = body.channel_identifier or body.contact_name
    resolved = _resolve(body.channel, channel_id, body.contact_name)
    person_id = resolved.get("person_id")
    if person_id and body.content_summary:
        convos = get_conversations_for_contact(str(person_id))
        convo_id = convos[0].get("id") if convos else None
        if not convo_id:
            convo = create_conversation(str(person_id))
            convo_id = convo.get("id") if convo else None
        if convo_id:
            msg_type = "incoming" if body.direction == "incoming" else "outgoing"
            send_message(convo_id, body.content_summary, msg_type)

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
    )
    return {"status": "ok", "contact": body.contact_name, "resolved": bool(person_id)}


# ─── Vault (PostgreSQL-backed) ────────────────────────────────────────────


@router.get("/api/vault/list")
async def api_vault_list(category: str | None = None):
    try:
        from robothor.vault import list as vault_list

        keys = vault_list(category=category)
        return {"keys": keys}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/vault/get")
async def api_vault_get(key: str = Query(..., description="Secret key")):
    try:
        from robothor.vault import get as vault_get

        value = vault_get(key)
        if value is not None:
            return {"key": key, "value": value}
        return JSONResponse({"error": f"No secret with key '{key}'"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
