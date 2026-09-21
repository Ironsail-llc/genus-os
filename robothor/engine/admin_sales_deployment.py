"""Human-only controls for the engine's native sales deployment runtime."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID  # noqa: TC003 — FastAPI resolves this annotation at runtime.

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from robothor.engine.auth import request_context
from robothor.operations.store import Conflict
from robothor.sales.models import Contract
from robothor.templates.fleet_snapshot import load_snapshot
from robothor.templates.fleet_store import staged_release_path


class Reason(Contract):
    reason: str = Field(min_length=10, max_length=2000)


class RevisionReason(Reason):
    expected_revision: int = Field(ge=0, strict=True)


class Prepare(RevisionReason):
    release_id: str = Field(pattern=r"^[a-f0-9]{64}$", strict=True)


class Empty(Contract):
    pass


def _summary(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    fields = (
        "id",
        "direction",
        "status",
        "source_release_id",
        "target_release_id",
        "base_revision",
        "actor",
        "reason",
        "created_at",
        "completed_at",
        "runtime_evidence",
    )
    result = {key: record.get(key) for key in fields}
    for side in ("source", "target"):
        artifact = record.get(side + "_artifact") or {}
        contracts = artifact.get("contracts") or {}
        result[side] = {
            "release_id": artifact.get("release_id"),
            "platform_revision": artifact.get("platform_revision"),
            "agents": contracts.get("agents", []),
            "workflows": contracts.get("workflows", []),
            "plugins": [
                {"name": row["name"], "version": row["version"]}
                for row in contracts.get("plugins", [])
            ],
        }
    return result


def _history(runtime: Any) -> tuple[list[dict[str, Any] | None], dict[str, Any] | None]:
    with runtime.coordinator.sales.ops.transaction() as cur:
        cur.execute(
            "SELECT * FROM sales_deployments WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 20",
            (runtime.coordinator.sales.tenant,),
        )
        recent = [_summary(row) for row in cur.fetchall()]
        cur.execute(
            "SELECT * FROM sales_deployments WHERE tenant_id=%s AND status='committed' ORDER BY base_revision DESC LIMIT 1",
            (runtime.coordinator.sales.tenant,),
        )
        rollback = _summary(cur.fetchone())
    return recent, rollback


async def _result(work: Any) -> Any:
    try:
        return await work
    except Conflict as exc:
        raise HTTPException(409, detail=str(exc)) from None
    except ValueError:
        raise HTTPException(422, detail="Release or runtime assets could not be verified") from None
    except Exception:
        raise HTTPException(
            503, detail="Deployment response unavailable; refresh status before retrying"
        ) from None


def register(app: Any, scheduler: Any) -> None:
    router = APIRouter(prefix="/api/admin/sales-deployment", tags=["sales-deployment"])

    def human(request: Request) -> tuple[Any, str]:
        context = request_context(request)
        if (
            context.is_service
            or context.role not in {"owner", "admin"}
            or context.audience == "loopback-development"
            or not context.has_scope("engine:control")
        ):
            raise HTTPException(403, detail="Authenticated human operator required")
        runtime = getattr(scheduler, "sales_runtime", None)
        if runtime is None:
            raise HTTPException(503, detail="Native sales runtime is unavailable")
        if context.tenant_id != runtime.coordinator.sales.tenant:
            raise HTTPException(403, detail="Tenant does not own this runtime")
        return runtime, "operator:" + context.actor_id

    @router.get("")
    async def status(request: Request) -> dict[str, Any]:
        runtime, _ = human(request)
        state = await _result(asyncio.to_thread(runtime._state))
        ready, reason = False, None
        try:
            await runtime.readiness()
            ready = True
        except Conflict as exc:
            reason = str(exc)
        except Exception:
            reason = "Runtime assets could not be verified"
        history, rollback = await _result(asyncio.to_thread(_history, runtime))
        current = await _result(asyncio.to_thread(runtime._state))
        if current != state:
            state, ready, reason = (
                current,
                False,
                "Deployment state changed; refresh to verify readiness",
            )
        return {
            "configured": bool(state["config"]),
            "settings_revision": state["revision"],
            "selected_release_id": state["config"].get("fleet_release_id"),
            "pending": _summary(state["pending"]),
            "history": history,
            "rollback_candidate": rollback,
            "control_busy": runtime._lock.locked(),
            "runtime": {"ready": ready, "reason": reason},
        }

    @router.get("/releases/{release_id}")
    async def inspect_release(release_id: str, request: Request) -> dict[str, Any]:
        runtime, _ = human(request)

        async def inspect() -> dict[str, Any]:
            root = staged_release_path(runtime.workspace, release_id)
            snapshot = await asyncio.to_thread(load_snapshot, root, expected_digest=release_id)
            metadata = snapshot.metadata()
            return {
                "release_id": release_id,
                "name": metadata["id"],
                "version": metadata["version"],
                "source_revision": snapshot.source_revision,
                "platform_revision": snapshot.platform_revision,
                "agents": metadata["contracts"]["agents"],
                "workflows": metadata["contracts"]["workflows"],
                "plugins": [
                    {
                        "name": p["name"],
                        "version": p["version"],
                        "wheel_sha256": metadata["files"][p["path"]]["sha256"],
                    }
                    for p in metadata["contracts"]["plugins"]
                ],
            }

        described: dict[str, Any] = await _result(inspect())
        return described

    @router.post("/prepare")
    async def prepare(body: Prepare, request: Request) -> Any:
        runtime, actor = human(request)
        return _summary(await _result(runtime.prepare(**body.model_dump(), actor=actor)))

    @router.post("/transitions/{transition_id}/commit")
    async def commit(transition_id: UUID, body: Empty, request: Request) -> Any:
        runtime, actor = human(request)
        return _summary(await _result(runtime.commit(str(transition_id), actor=actor)))

    @router.post("/transitions/{transition_id}/abort")
    async def abort(transition_id: UUID, body: Reason, request: Request) -> Any:
        runtime, actor = human(request)
        return _summary(
            await _result(runtime.abort(str(transition_id), actor=actor, reason=body.reason))
        )

    @router.post("/transitions/{transition_id}/rollback")
    async def rollback(transition_id: UUID, body: RevisionReason, request: Request) -> Any:
        runtime, actor = human(request)
        return _summary(
            await _result(
                runtime.prepare_rollback(str(transition_id), actor=actor, **body.model_dump())
            )
        )

    app.include_router(router)
