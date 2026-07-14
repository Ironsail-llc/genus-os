"""Operator-only guardrail control.

The write path is deliberately hostile to agents, via three independent
locks:

1. No agent tool exists for this — see
   ``robothor/engine/tests/test_no_control_tool.py``, which scans
   ``robothor/engine/tools/schemas.py`` (the only place an agent-facing tool
   could be registered) and fails CI if one ever appears.
2. This API lives on the bridge (``crm/bridge``), never the engine — an
   agent's tool-call surface cannot reach it at all.
3. Operator-only at the handler: agents carry service tokens
   (``AuthContext.typ == "service"``, ``is_service == True``) and are
   rejected with 403 here, structurally, regardless of RBAC/capability
   configuration elsewhere.

Handlers are plain ``def`` (not ``async def``) on purpose: ``robothor.flags.store``
and ``robothor.flags.evidence.verdict`` call synchronous psycopg2, so FastAPI
must run them in its worker threadpool rather than the event loop (see
``crm/bridge/tests/test_route_concurrency.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from robothor.flags import store
from robothor.flags.evidence import verdict

router = APIRouter(prefix="/api/controls", tags=["controls"])

_VALID_VALUES = frozenset({"off", "observe", "alert", "enforce", "true", "false"})


class FlagPatch(BaseModel):
    value: str
    reason: str


def _require_operator(request: Request) -> str:
    auth = getattr(request.state, "auth", None)
    if auth is None or auth.is_service:
        raise HTTPException(status_code=403, detail="operator role required")
    return f"operator:{auth.actor_id}"


@router.get("")
def list_controls() -> list[dict]:
    out = []
    for name in sorted(store.GOVERNED_FLAGS):
        value = store.resolve(name) or "observe"
        v = verdict(name, value)
        out.append(
            {
                "name": name,
                "value": value,
                "verdict": {
                    "status": v.status,
                    "message": v.message,
                    "last_fired": v.last_fired.isoformat() if v.last_fired else None,
                    "count_7d": v.count_7d,
                },
            }
        )
    return out


@router.patch("/{name}")
def set_control(name: str, patch: FlagPatch, request: Request) -> dict:
    actor = _require_operator(request)
    if name not in store.GOVERNED_FLAGS:
        raise HTTPException(status_code=404, detail="unknown flag")
    if patch.value not in _VALID_VALUES:
        raise HTTPException(status_code=422, detail="invalid value")
    store.set_flag(name, patch.value, actor=actor, reason=patch.reason)
    return {"name": name, "value": patch.value}
