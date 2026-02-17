"""
Robothor Bridge Service — Connects OpenClaw, Twenty CRM, Chatwoot, and Memory System.
FastAPI app on port 9100.
"""
import json
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

import config
import contact_resolver
import chatwoot_client
import twenty_client

http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=30.0)
    yield
    await http_client.aclose()


app = FastAPI(title="Robothor Bridge", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    """Check connectivity to all dependent services."""
    services = {}
    try:
        r = await http_client.get(f"{config.TWENTY_URL}/healthz")
        services["twenty"] = "ok" if r.status_code == 200 else f"error:{r.status_code}"
    except Exception as e:
        services["twenty"] = f"error:{e}"

    try:
        r = await http_client.get(f"{config.CHATWOOT_URL}/api")
        services["chatwoot"] = "ok" if r.status_code == 200 else f"error:{r.status_code}"
    except Exception as e:
        services["chatwoot"] = f"error:{e}"

    try:
        r = await http_client.get(f"{config.MEMORY_URL}/health")
        services["memory"] = "ok" if r.status_code == 200 else f"error:{r.status_code}"
    except Exception as e:
        services["memory"] = f"error:{e}"

    all_ok = all(v == "ok" for v in services.values())
    return {"status": "ok" if all_ok else "degraded", "services": services}


@app.post("/resolve-contact")
async def resolve_contact(request: Request):
    """Resolve a channel identifier to cross-system contact IDs."""
    body = await request.json()
    channel = body.get("channel", "")
    identifier = body.get("identifier", "")
    name = body.get("name")

    if not channel or not identifier:
        return JSONResponse({"error": "channel and identifier required"}, status_code=400)

    result = await contact_resolver.resolve(channel, identifier, name, http_client)
    # Convert datetime fields to strings for JSON
    for k, v in result.items():
        if hasattr(v, "isoformat"):
            result[k] = v.isoformat()
    return result


@app.get("/timeline/{identifier}")
async def timeline(identifier: str):
    """Get unified timeline for a contact across all systems."""
    result = await contact_resolver.get_timeline(identifier, http_client)
    return result


@app.post("/webhooks/chatwoot")
async def webhook_chatwoot(request: Request):
    """Receive Chatwoot webhook events and ingest to memory system."""
    body = await request.json()
    event = body.get("event", "")

    if event == "message_created":
        content = body.get("content", {})
        message_text = content.get("content", "") if isinstance(content, dict) else str(content)
        sender = body.get("sender", {})
        conversation = body.get("conversation", {})

        # Ingest to memory system
        try:
            await http_client.post(f"{config.MEMORY_URL}/ingest", json={
                "channel": "chatwoot",
                "content": message_text,
                "metadata": {
                    "sender_name": sender.get("name", ""),
                    "sender_email": sender.get("email", ""),
                    "conversation_id": conversation.get("id"),
                    "event": event,
                },
            })
        except Exception:
            pass  # Don't fail webhook on memory ingestion error

    return {"status": "ok"}


@app.post("/webhooks/twenty")
async def webhook_twenty(request: Request):
    """Receive Twenty CRM webhook events."""
    body = await request.json()
    # Log for now; expand as needed
    return {"status": "ok", "event": body.get("event", "unknown")}


@app.post("/webhooks/openclaw")
async def webhook_openclaw(request: Request):
    """Receive OpenClaw message events, push to Chatwoot and resolve contacts."""
    body = await request.json()
    channel = body.get("channel", "unknown")
    identifier = body.get("identifier", "")
    name = body.get("name", "")
    content = body.get("content", "")
    direction = body.get("direction", "incoming")

    # Resolve contact across systems
    resolved = await contact_resolver.resolve(channel, identifier, name, http_client)

    # Push to Chatwoot if we have a contact
    chatwoot_contact_id = resolved.get("chatwoot_contact_id")
    if chatwoot_contact_id and content:
        # Find or create conversation
        convos = await chatwoot_client.get_conversations(chatwoot_contact_id, http_client)
        if convos:
            convo_id = convos[0].get("id")
        else:
            convo = await chatwoot_client.create_conversation(chatwoot_contact_id, client=http_client)
            convo_id = convo.get("id") if convo else None

        if convo_id:
            msg_type = "incoming" if direction == "incoming" else "outgoing"
            await chatwoot_client.send_message(convo_id, content, msg_type, client=http_client)

    return {"status": "ok", "resolved": {
        "twenty_person_id": resolved.get("twenty_person_id"),
        "chatwoot_contact_id": resolved.get("chatwoot_contact_id"),
    }}


@app.post("/log-interaction")
async def log_interaction(request: Request):
    """Log an interaction from the agent to CRM layer.
    Called by the log_interaction MCP tool."""
    body = await request.json()
    contact_name = body.get("contact_name", "")
    channel = body.get("channel", "api")
    direction = body.get("direction", "outgoing")
    content_summary = body.get("content_summary", "")
    channel_identifier = body.get("channel_identifier", contact_name)

    # Resolve contact
    resolved = await contact_resolver.resolve(channel, channel_identifier, contact_name, http_client)

    # Create Chatwoot conversation record
    chatwoot_contact_id = resolved.get("chatwoot_contact_id")
    if chatwoot_contact_id and content_summary:
        convos = await chatwoot_client.get_conversations(chatwoot_contact_id, http_client)
        if convos:
            convo_id = convos[0].get("id")
        else:
            convo = await chatwoot_client.create_conversation(chatwoot_contact_id, client=http_client)
            convo_id = convo.get("id") if convo else None

        if convo_id:
            msg_type = "incoming" if direction == "incoming" else "outgoing"
            await chatwoot_client.send_message(convo_id, content_summary, msg_type, client=http_client)

    return {"status": "ok", "contact": contact_name, "resolved": bool(resolved.get("twenty_person_id"))}


# ─── CRM Proxy Endpoints (for OpenClaw plugin) ───────────────────────────


@app.get("/api/conversations")
async def api_list_conversations(
    status: str = Query("open"),
    page: int = Query(1),
):
    """List Chatwoot conversations by status."""
    result = await chatwoot_client.list_conversations(status, page, client=http_client)
    return result


@app.get("/api/conversations/{conversation_id}")
async def api_get_conversation(conversation_id: int):
    """Get a single Chatwoot conversation."""
    result = await chatwoot_client.get_conversation(conversation_id, client=http_client)
    if not result:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    return result


@app.get("/api/conversations/{conversation_id}/messages")
async def api_list_messages(conversation_id: int):
    """List messages in a Chatwoot conversation."""
    result = await chatwoot_client.list_messages(conversation_id, client=http_client)
    return {"payload": result}


@app.post("/api/conversations/{conversation_id}/messages")
async def api_create_message(conversation_id: int, request: Request):
    """Create a message in a Chatwoot conversation."""
    body = await request.json()
    content = body.get("content", "")
    message_type = body.get("message_type", "outgoing")
    private = body.get("private", False)

    if not content:
        return JSONResponse({"error": "content required"}, status_code=400)

    result = await chatwoot_client.send_message(
        conversation_id, content, message_type, client=http_client
    )
    return result or {"status": "ok"}


@app.post("/api/conversations/{conversation_id}/toggle_status")
async def api_toggle_conversation_status(conversation_id: int, request: Request):
    """Toggle conversation status (open, resolved, pending, snoozed)."""
    body = await request.json()
    status = body.get("status", "resolved")
    if status not in ("open", "resolved", "pending", "snoozed"):
        return JSONResponse({"error": "status must be open, resolved, pending, or snoozed"}, status_code=400)
    result = await chatwoot_client.toggle_conversation_status(
        conversation_id, status, client=http_client
    )
    if not result:
        return JSONResponse({"error": "failed to toggle status"}, status_code=500)
    return result


@app.get("/api/people")
async def api_list_people(
    search: Optional[str] = Query(None),
    limit: int = Query(20),
):
    """List or search people in Twenty CRM."""
    result = await twenty_client.list_people(search, limit, client=http_client)
    return {"people": result}


@app.post("/api/people")
async def api_create_person(request: Request):
    """Create a person in Twenty CRM."""
    body = await request.json()
    first_name = body.get("firstName", "")
    last_name = body.get("lastName", "")

    if not first_name:
        return JSONResponse({"error": "firstName required"}, status_code=400)

    email = body.get("email")
    phone = body.get("phone")

    person_id = await twenty_client.create_person(
        first_name, last_name, email, phone, client=http_client
    )
    if person_id:
        return {"id": person_id, "firstName": first_name, "lastName": last_name}
    return JSONResponse({"error": "failed to create person"}, status_code=500)


@app.post("/api/notes")
async def api_create_note(request: Request):
    """Create a note in Twenty CRM."""
    body = await request.json()
    title = body.get("title", "")
    note_body = body.get("body", "")

    if not title:
        return JSONResponse({"error": "title required"}, status_code=400)

    note_id = await twenty_client.create_note(title, note_body, client=http_client)
    if note_id:
        return {"id": note_id, "title": title}
    return JSONResponse({"error": "failed to create note"}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9100)
