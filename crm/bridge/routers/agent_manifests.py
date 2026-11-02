"""Agent manifests from the browser — create, edit, retire, validate, run.

Until the agent builder, building an agent meant an ssh session, a text editor,
and an engine restart. That is the single largest step between "the appliance
is installed" and "the appliance does something".

**The files are the engine's.** The manifests (``docs/agents/*.yaml``) and the
instruction files (``brain/*.md``) live in the workspace, and the workspace is
mounted on the ENGINE. This router used to read and write them on the bridge's
own disk, which was only the engine's where every service happened to mount
the same volume; where the bridge has no workspace volume the fleet list was
empty, the chat switcher offered nothing, and a write landed where the engine
never reads. So the store, the verdict, the reconcile and the trigger all live
in the engine (``robothor/engine/admin_manifests.py``, under
``/api/admin/agent-manifests``), and this router is the operator-facing front
door to them: it keeps every path, status code and response shape the
dashboard knows, the operator gate and the primary-tenant gate, and the audit
trail that names the operator — and forwards everything else with
``engine_request``. There is no local-disk fallback: two readers of one fleet
is how the list and the engine come to disagree.

Two properties this file is built around:

* **Every route gates before it forwards.** ``require_operator`` on every route,
  GETs included (this enumerates the fleet, its models and its delivery
  targets), and the agent id is validated here as well as in the engine, so a
  hostile id never becomes part of an engine URL.
* **An engine that cannot answer is a sentence, not a traceback.** A refusal
  the engine DECIDED (404, 409, 422, with its ``detail``) reaches the browser
  unchanged — the validator's codes travel in it. Anything else — the engine
  unreachable, or refusing the bridge's own credential — is a 502 with a fixed
  message. An engine 401/403 passed through would read as "you are not
  allowed" to an operator who is.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from deps import get_tenant_id
from fastapi import APIRouter, Depends, HTTPException, Request

from robothor.engine.manifest_store import (
    CreateRequest,
    DeleteRequest,
    ManifestError,
    PatchRequest,
    ValidateRequest,
    safe_id,
)
from routers._audit import audited
from routers._engine_client import engine_request
from routers._operator import require_operator

logger = logging.getLogger(__name__)


def _require_primary_tenant(tenant_id: str = Depends(get_tenant_id)) -> None:
    """The agent fleet is appliance-global, so deny secondary tenants.

    Symmetric with ``installed_agents``: one engine owns one manifest
    directory, and a second tenant's operator editing it would be editing
    somebody else's fleet.
    """
    from robothor.constants import DEFAULT_TENANT

    if tenant_id != DEFAULT_TENANT:
        raise HTTPException(
            status_code=403,
            detail="appliance administration not authorized for tenant",
        )


router = APIRouter(
    tags=["agent-manifests"],
    dependencies=[Depends(_require_primary_tenant)],
)

#: The engine's half of this router. Every path below is built from it and an
#: id that ``_safe_id`` has already constrained to ``[a-z0-9-]``.
_ENGINE = "/api/admin/agent-manifests"

#: What the browser is told when the engine did not decide the answer itself.
ENGINE_UNAVAILABLE = "the engine did not answer, so the agent fleet could not be reached"


def _safe_id(agent_id: object) -> str:
    """A kebab-case agent id, or a 422 naming the rule it broke.

    The engine's own validator, run HERE first: the id becomes part of an
    engine URL, and a request built from a caller's value gets its lock before
    it is built. ``automations.py`` uses it for the agent half of a job id.
    """
    try:
        return safe_id(agent_id)
    except ManifestError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error


def _engine_path(agent_id: str, action: str = "") -> str:
    # Percent-encoded as well as identifier-validated: this value is the only
    # part of an engine URL a caller chooses, and it gets two locks, not one.
    suffix = f"/{action}" if action else ""
    return f"{_ENGINE}/{quote(agent_id, safe='')}{suffix}"


async def _forward(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """One engine call, answered as the browser expects it. See the module header."""
    status, payload = await engine_request(method, path, json=body)
    if status < 400:
        return payload
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if detail is not None and status not in (401, 403):
        raise HTTPException(status_code=status, detail=detail)
    logger.warning("Agent-manifest engine call answered %d", status)
    raise HTTPException(status_code=502, detail=ENGINE_UNAVAILABLE)


# ─── Engine reconcile ────────────────────────────────────────────────


async def reconcile_engine_schedules() -> dict[str, Any]:
    """Tell the engine to re-derive its job set, and report what it said.

    For the bridge's OTHER manifest writers (the first-run wizard and the
    marketplace installer); the agent builder's own writes are reconciled
    inside the engine. Never raises. A manifest write that succeeded is not
    rolled back because the engine could not be reached — the file is the
    source of truth and the next watchdog pass (five minutes) reconciles it
    anyway. What must not happen is the operator being told the agent is live
    when it is not, so the failure is reported in the response instead of
    swallowed.
    """
    status, body = await engine_request("POST", "/api/admin/scheduler/reconcile")
    if status >= 400 or not isinstance(body, dict):
        detail = body.get("error") if isinstance(body, dict) else None
        return {"applied": False, "error": detail or "the engine did not reconcile"}
    return {"applied": True, **body}


# ─── Endpoints ───────────────────────────────────────────────────────


@router.get("/api/agent-manifests")
async def list_manifests(request: Request) -> dict[str, Any]:
    """Every agent this appliance has, including the ones that will not load."""
    require_operator(request)
    result: dict[str, Any] = await _forward("GET", _ENGINE)
    return result


@router.get("/api/agent-manifests/{agent_id}")
async def get_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """One agent: its document, its raw YAML, its instructions, its verdict."""
    require_operator(request)
    result: dict[str, Any] = await _forward("GET", _engine_path(_safe_id(agent_id)))
    return result


@router.post("/api/agent-manifests/validate")
async def validate_manifest(body: ValidateRequest, request: Request) -> dict[str, Any]:
    """Always 200 when the engine answers. The verdict is the payload."""
    require_operator(request)
    result: dict[str, Any] = await _forward("POST", f"{_ENGINE}/validate", body.model_dump())
    return result


@router.post("/api/agent-manifests", status_code=201)
async def create_manifest(body: CreateRequest, request: Request) -> dict[str, Any]:
    """Scaffold an agent, validate it, write it, and make it fire — in the engine."""
    require_operator(request)
    result: dict[str, Any] = await _forward("POST", _ENGINE, body.model_dump())
    agent_id = str(result.get("id") or "")
    audited(request, "helm.agent_manifest.create", action=agent_id, agent_id=agent_id)
    return result


@router.patch("/api/agent-manifests/{agent_id}")
async def patch_manifest(agent_id: str, body: PatchRequest, request: Request) -> dict[str, Any]:
    """Edit the paths the form owns, and nothing else."""
    require_operator(request)
    agent_id = _safe_id(agent_id)
    result = await _forward("PATCH", _engine_path(agent_id), body.model_dump())
    return _audited_edit(request, agent_id, body.owned_paths(), result)


@router.post("/api/agent-manifests/{agent_id}/enable")
async def enable_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """Put the agent back on its schedule. Never a no-op."""
    require_operator(request)
    agent_id = _safe_id(agent_id)
    result = await _forward("POST", _engine_path(agent_id, "enable"))
    return _audited_edit(request, agent_id, {"schedule.enabled": True}, result)


@router.post("/api/agent-manifests/{agent_id}/disable")
async def disable_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """Silence the agent without retiring it — cron, heartbeat and worker."""
    require_operator(request)
    agent_id = _safe_id(agent_id)
    result = await _forward("POST", _engine_path(agent_id, "disable"))
    return _audited_edit(request, agent_id, {"schedule.enabled": False}, result)


def _audited_edit(
    request: Request, agent_id: str, changes: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    """One audit event per accepted edit, naming the fields and the new version."""
    manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
    audited(
        request,
        "helm.agent_manifest.update",
        action=agent_id,
        agent_id=agent_id,
        fields=sorted(changes),
        version=str((manifest or {}).get("version") or ""),
    )
    return result


@router.delete("/api/agent-manifests/{agent_id}")
async def retire_manifest(agent_id: str, body: DeleteRequest, request: Request) -> dict[str, Any]:
    """Move the manifest to ``retired/``; the confirmation is the id, typed."""
    require_operator(request)
    agent_id = _safe_id(agent_id)
    result: dict[str, Any] = await _forward("DELETE", _engine_path(agent_id), body.model_dump())
    audited(request, "helm.agent_manifest.retire", action=agent_id, agent_id=agent_id)
    return result


@router.post("/api/agent-manifests/{agent_id}/run")
async def run_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """Fire the agent once, now. The engine owns the runner."""
    require_operator(request)
    agent_id = _safe_id(agent_id)
    path = f"{_ENGINE}/{quote(agent_id, safe='')}/run"
    status, body = await engine_request("POST", path)
    # 404/422 are the engine's own answers about the id; pass them through.
    detail = body.get("detail") if isinstance(body, dict) else None
    if status in (404, 422) and detail is not None:
        raise HTTPException(status_code=status, detail=detail)
    triggered = body.get("triggered") if isinstance(body, dict) else None
    # The trigger answers with an ``error`` key for an agent it cannot load,
    # so the status code alone is not the verdict.
    refused = status >= 400 or not isinstance(triggered, dict) or bool(triggered.get("error"))
    audited(
        request,
        "helm.agent_manifest.run",
        action=agent_id,
        agent_id=agent_id,
        status="error" if refused else "ok",
    )
    if refused:
        raise HTTPException(status_code=502, detail="the engine did not accept the trigger")
    return {"id": agent_id, "triggered": triggered}
