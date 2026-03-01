"""Integration routes — contact resolution, webhooks, Impetus One proxy."""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from models import (
    LogInteractionRequest,
    ResolveContactRequest,
    VaultCreateCardRequest,
    VaultCreateLoginRequest,
)

from robothor.audit.logger import log_event
from robothor.events.bus import publish

# Lazy-loaded vault client
_vault_client = None
# Lazy-loaded Impetus MCP client
_impetus_mcp = None

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


# ─── Vault ───────────────────────────────────────────────────────────────


def _get_vault():
    global _vault_client
    if _vault_client is None:
        import sys

        sys.path.insert(0, os.path.expanduser("~/clawd/scripts"))
        from vault_client import VaultClient

        _vault_client = VaultClient()
        _vault_client.login()
    return _vault_client


@router.get("/api/vault/list")
async def api_vault_list():
    try:
        return {"items": _get_vault().list_items()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/vault/get")
async def api_vault_get(name: str = Query(..., description="Item name")):
    try:
        item = _get_vault().get_item(name)
        if item:
            return item
        return JSONResponse({"error": f"No item matching '{name}'"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/vault/search")
async def api_vault_search(q: str = Query(..., description="Search query")):
    try:
        return {"items": _get_vault().search(q)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/vault/create")
async def api_vault_create(body: VaultCreateLoginRequest):
    try:
        return _get_vault().create_login(
            name=body.name,
            username=body.username,
            password=body.password,
            uri=body.uri,
            notes=body.notes,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/vault/create_card")
async def api_vault_create_card(body: VaultCreateCardRequest):
    try:
        return _get_vault().create_card(
            name=body.name,
            cardholderName=body.cardholderName,
            number=body.number,
            expMonth=body.expMonth,
            expYear=body.expYear,
            code=body.code,
            brand=body.brand,
            notes=body.notes,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── Impetus One Proxy ───────────────────────────────────────────────────


async def _io_get(path: str, params: dict | None = None) -> dict:
    from bridge_service import _bridge_config, http_client

    headers = {"Authorization": f"Bearer {_bridge_config['impetus_one_token']}"}
    r = await http_client.get(
        f"{_bridge_config['impetus_one_url']}{path}",
        headers=headers,
        params=params,
    )
    r.raise_for_status()
    return r.json()


async def _io_post(path: str, body: dict) -> dict:
    from bridge_service import _bridge_config, http_client

    headers = {
        "Authorization": f"Bearer {_bridge_config['impetus_one_token']}",
        "Content-Type": "application/json",
    }
    r = await http_client.post(
        f"{_bridge_config['impetus_one_url']}{path}",
        headers=headers,
        json=body,
    )
    r.raise_for_status()
    return r.json()


@router.get("/api/impetus/health")
async def api_impetus_health():
    try:
        from bridge_service import _bridge_config, http_client

        r = await http_client.get(f"{_bridge_config['impetus_one_url']}/healthz", timeout=5.0)
        return {"status": "ok" if r.status_code == 200 else "error", "http_code": r.status_code}
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=503)


@router.get("/api/impetus/patients")
async def api_impetus_patients(
    search: str | None = Query(None),
    firstName: str | None = Query(None),
    lastName: str | None = Query(None),
):
    try:
        params = {}
        if firstName:
            params["firstName"] = firstName
        if lastName:
            params["lastName"] = lastName
        if search:
            params["lastName"] = search
        return await _io_get("/api/patients", params or None)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@router.get("/api/impetus/patients/{patient_id}")
async def api_impetus_patient(patient_id: str):
    try:
        return await _io_get(f"/api/patients/{patient_id}")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@router.get("/api/impetus/prescriptions")
async def api_impetus_prescriptions(status: str | None = Query(None)):
    try:
        params = {"status": status} if status else None
        return await _io_get("/api/prescriptions", params)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@router.get("/api/impetus/prescriptions/{rx_id}")
async def api_impetus_prescription(rx_id: str):
    try:
        return await _io_get(f"/api/prescriptions/{rx_id}")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@router.get("/api/impetus/appointments")
async def api_impetus_appointments():
    try:
        return await _io_get("/api/appointments")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@router.get("/api/impetus/queue")
async def api_impetus_queue():
    try:
        return await _io_get("/api/queue_items")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@router.get("/api/impetus/medications")
async def api_impetus_medications():
    try:
        return await _io_get("/api/medications")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@router.get("/api/impetus/pharmacies")
async def api_impetus_pharmacies():
    try:
        return await _io_get("/api/pharmacies")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@router.get("/api/impetus/orders")
async def api_impetus_orders():
    try:
        return await _io_get("/api/ecommerce/orders")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@router.get("/api/impetus/encounters")
async def api_impetus_encounters():
    try:
        return await _io_get("/api/encounters")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@router.post("/api/impetus/graphql")
async def api_impetus_graphql(request: Request):
    try:
        body = await request.json()
        return await _io_post("/api/graphql", body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# ─── Impetus MCP Client ─────────────────────────────────────────────────


class ImpetusMCPClient:
    """JSON-RPC client for Impetus One MCP HTTP endpoint."""

    def __init__(self):
        self.session_id: str | None = None
        self._initialized = False
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _send(self, message: dict) -> dict:
        from bridge_service import _bridge_config, http_client

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_bridge_config['impetus_one_token']}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        r = await http_client.post(
            f"{_bridge_config['impetus_one_url']}/_mcp",
            headers=headers,
            json=message,
            timeout=30.0,
        )

        if session_id := r.headers.get("Mcp-Session-Id"):
            self.session_id = session_id

        content_type = r.headers.get("content-type", "")
        if "application/json" in content_type or "text/json" in content_type:
            return r.json()
        text = r.text
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {"error": f"Unexpected response ({r.status_code}): {text[:200]}"}

    async def ensure_initialized(self):
        if self._initialized:
            return
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "robothor-bridge", "version": "1.0.0"},
                },
            }
        )
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        await self.ensure_initialized()
        result = await self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        if "error" in result:
            err = result["error"]
            return {"error": err.get("message", str(err)) if isinstance(err, dict) else str(err)}
        content = result.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])
            except (json.JSONDecodeError, KeyError):
                return {"text": content[0].get("text", "")}
        return result.get("result", {})

    def reset(self):
        self.session_id = None
        self._initialized = False


def _get_impetus_mcp() -> ImpetusMCPClient:
    global _impetus_mcp
    if _impetus_mcp is None:
        _impetus_mcp = ImpetusMCPClient()
    return _impetus_mcp


@router.post("/api/impetus/tools/call")
async def api_impetus_tools_call(request: Request):
    """Generic MCP passthrough — Engine routes all Impetus tools through this."""
    try:
        body = await request.json()
        name = body.get("name", "")
        arguments = body.get("arguments", {})
        if not name:
            return JSONResponse({"error": "tool name required"}, status_code=400)
        return await _get_impetus_mcp().call_tool(name, arguments)
    except Exception as e:
        _get_impetus_mcp().reset()
        return JSONResponse({"error": str(e)}, status_code=502)


@router.get("/api/impetus/providers")
async def api_impetus_providers():
    try:
        return await _get_impetus_mcp().call_tool("list_actable_providers")
    except Exception as e:
        _get_impetus_mcp().reset()
        return JSONResponse({"error": str(e)}, status_code=502)


@router.post("/api/impetus/prescriptions/draft")
async def api_impetus_create_draft(request: Request):
    try:
        body = await request.json()
        return await _get_impetus_mcp().call_tool("create_prescription_draft", body)
    except Exception as e:
        _get_impetus_mcp().reset()
        return JSONResponse({"error": str(e)}, status_code=502)


@router.post("/api/impetus/prescriptions/{rx_id}/transmit")
async def api_impetus_transmit(rx_id: str, request: Request):
    try:
        body = await request.json()
        body["prescriptionId"] = rx_id
        return await _get_impetus_mcp().call_tool("transmit_prescription", body)
    except Exception as e:
        _get_impetus_mcp().reset()
        return JSONResponse({"error": str(e)}, status_code=502)
