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
dashboard knows, the tenant-admin gate, and the audit trail that names the
operator — and forwards everything else with ``engine_request``. There is no
local-disk fallback: two readers of one fleet is how the list and the engine
come to disagree.

**The engine is the ACTING tenant's.** One engine per tenant cell, each with
its own workspace, so every call goes to ``get_tenant_id(request)``'s engine
with a control token minted for that tenant (``_engine_client``): a root admin
who switched into a child edits the child's fleet, and a child admin never
reaches the parent's or a sibling's — that engine refuses the token anyway.

Two properties this file is built around:

* **Every route gates before it forwards.** ``require_tenant_admin`` on every route,
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
from fastapi import APIRouter, HTTPException, Request

from robothor.engine.manifest_store import (
    CreateRequest,
    DeleteRequest,
    ManifestError,
    PatchRequest,
    ValidateRequest,
    kebab,
    safe_id,
)
from routers._audit import audited
from routers._engine_client import engine_request
from routers._operator import require_tenant_admin

logger = logging.getLogger(__name__)


router = APIRouter(tags=["agent-manifests"])

#: The engine's half of this router. Every path below is built from it and an
#: id that ``_safe_id`` has already constrained to ``[a-z0-9-]``.
_ENGINE = "/api/admin/agent-manifests"

#: What the browser is told when the engine did not decide the answer itself.
#: Worded for both cases it covers — silence and an unhandled error — so an
#: operator with a full disk is not sent looking for a network fault.
ENGINE_UNAVAILABLE = (
    "the engine did not answer, or answered with an error, so the request could not be completed"
)

#: FastAPI's own 404 for a path it has no route for: an engine older than these
#: routes, which is not the same answer as "no such agent".
_NO_ROUTE = "Not Found"
OLD_ENGINE = "the tenant's engine does not support manifest administration; upgrade the engine"

#: One write now covers the scan, the registry build, the write and the
#: reconcile, which used to be two calls with the client's default each.
WRITE_TIMEOUT_SECONDS = 90.0


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


async def _forward(
    request: Request,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
) -> Any:
    """One call to the acting tenant's engine, answered as the browser expects it."""
    tenant = get_tenant_id(request)
    if timeout is None:
        status, payload = await engine_request(method, path, json=body, tenant=tenant)
    else:
        status, payload = await engine_request(
            method, path, json=body, timeout=timeout, tenant=tenant
        )
    if status < 400:
        return payload
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if status == 404 and detail == _NO_ROUTE:
        logger.warning("The engine has no manifest administration routes; upgrade it")
        raise HTTPException(status_code=502, detail=OLD_ENGINE)
    if detail is not None and status not in (401, 403):
        raise HTTPException(status_code=status, detail=detail)
    logger.warning("Agent-manifest engine call answered %d", status)
    raise HTTPException(status_code=502, detail=ENGINE_UNAVAILABLE)


async def _forward_write(
    request: Request,
    event: str,
    agent_id: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    **details: Any,
) -> dict[str, Any]:
    """A write, audited even when its answer is lost.

    The engine may have completed a write whose answer never reached this
    process (a timeout, a reset). The operator sees a 502, and the audit trail
    must still name them — as ``status="error"``, because this side cannot know
    whether it landed. A refusal the engine DECIDED is a write that did not
    happen, and is not audited, as before.
    """
    try:
        result: dict[str, Any] = await _forward(
            request, method, path, body, timeout=WRITE_TIMEOUT_SECONDS
        )
    except HTTPException as error:
        if error.status_code == 502:
            audited(request, event, action=agent_id, agent_id=agent_id, status="error", **details)
        raise
    return result


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
    """Every agent the ACTING tenant has, including the ones that will not load."""
    require_tenant_admin(request)
    result: dict[str, Any] = await _forward(request, "GET", _ENGINE)
    return result


@router.get("/api/agent-manifests/{agent_id}")
async def get_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """One agent: its document, its raw YAML, its instructions, its verdict."""
    require_tenant_admin(request)
    result: dict[str, Any] = await _forward(request, "GET", _engine_path(_safe_id(agent_id)))
    return result


@router.post("/api/agent-manifests/validate")
async def validate_manifest(body: ValidateRequest, request: Request) -> dict[str, Any]:
    """Always 200 when the engine answers. The verdict is the payload."""
    require_tenant_admin(request)
    result: dict[str, Any] = await _forward(
        request, "POST", f"{_ENGINE}/validate", body.model_dump()
    )
    return result


@router.post("/api/agent-manifests", status_code=201)
async def create_manifest(body: CreateRequest, request: Request) -> dict[str, Any]:
    """Scaffold an agent, validate it, write it, and make it fire — in the engine."""
    require_tenant_admin(request)
    # The id the engine will derive, for an audit event whose answer is lost.
    # An id the engine would refuse is not a slug, so it is not the action.
    try:
        intended = safe_id(body.id if body.id is not None else kebab(body.name))
    except ManifestError:
        intended = "(invalid id)"
    event = "helm.agent_manifest.create"
    result = await _forward_write(request, event, intended, "POST", _ENGINE, body.model_dump())
    agent_id = str(result.get("id") or "")
    audited(request, event, action=agent_id, agent_id=agent_id)
    return result


@router.patch("/api/agent-manifests/{agent_id}")
async def patch_manifest(agent_id: str, body: PatchRequest, request: Request) -> dict[str, Any]:
    """Edit the paths the form owns, and nothing else."""
    require_tenant_admin(request)
    agent_id = _safe_id(agent_id)
    changes = body.owned_paths()
    result = await _forward_edit(request, agent_id, changes, "PATCH", "", body.model_dump())
    return _audited_edit(request, agent_id, changes, result)


@router.post("/api/agent-manifests/{agent_id}/enable")
async def enable_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """Put the agent back on its schedule. Never a no-op."""
    require_tenant_admin(request)
    agent_id = _safe_id(agent_id)
    changes = {"schedule.enabled": True}
    result = await _forward_edit(request, agent_id, changes, "POST", "enable")
    return _audited_edit(request, agent_id, changes, result)


@router.post("/api/agent-manifests/{agent_id}/disable")
async def disable_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """Silence the agent without retiring it — cron, heartbeat and worker."""
    require_tenant_admin(request)
    agent_id = _safe_id(agent_id)
    changes = {"schedule.enabled": False}
    result = await _forward_edit(request, agent_id, changes, "POST", "disable")
    return _audited_edit(request, agent_id, changes, result)


async def _forward_edit(
    request: Request,
    agent_id: str,
    changes: dict[str, Any],
    method: str,
    action: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await _forward_write(
        request,
        "helm.agent_manifest.update",
        agent_id,
        method,
        _engine_path(agent_id, action),
        body,
        fields=sorted(changes),
    )


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
    require_tenant_admin(request)
    agent_id = _safe_id(agent_id)
    event = "helm.agent_manifest.retire"
    path = _engine_path(agent_id)
    result = await _forward_write(request, event, agent_id, "DELETE", path, body.model_dump())
    audited(request, event, action=agent_id, agent_id=agent_id)
    return result


@router.post("/api/agent-manifests/{agent_id}/run")
async def run_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """Fire the agent once, now. The engine owns the runner."""
    require_tenant_admin(request)
    agent_id = _safe_id(agent_id)
    path = f"{_ENGINE}/{quote(agent_id, safe='')}/run"
    status, body = await engine_request("POST", path, tenant=get_tenant_id(request))
    # 404/422 are the engine's own answers about the id; pass them through.
    detail = body.get("detail") if isinstance(body, dict) else None
    if status in (404, 422) and detail not in (None, _NO_ROUTE):
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
