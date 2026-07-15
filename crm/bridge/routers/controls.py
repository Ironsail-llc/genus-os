"""Operator-only guardrail control.

Both the read and write paths are deliberately hostile to agents, via three
independent locks:

1. No agent tool exists for this — see
   ``robothor/engine/tests/test_no_control_tool.py``, which scans
   ``robothor/engine/tools/schemas.py`` (the only place an agent-facing tool
   could be registered) and fails CI if one ever appears.
2. This API lives on the bridge (``crm/bridge``), never the engine — an
   agent's tool-call surface cannot reach it at all.
3. Operator-only at the handler: agents carry service tokens
   (``AuthContext.typ == "service"``, ``is_service == True``) and are
   rejected with 403 here, structurally, regardless of RBAC/capability
   configuration elsewhere. Human callers are further gated on
   ``AuthContext.role`` — only ``OPERATOR_ROLES`` (``owner``, ``admin``)
   pass; every other human role (``member``, ``user``, ``viewer``,
   ``auditor``) is also 403'd, since dashboard SSO admits any verified org
   member, not just the operator.

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

_MODE_VALUES = frozenset({"off", "observe", "alert", "enforce"})
_BOOL_VALUES = frozenset({"true", "false"})

# Human roles permitted to read AND write guardrail flags. Every other human
# role (member, user, viewer, auditor) and every service (agent) token is
# rejected — see ``_require_operator``.
OPERATOR_ROLES = frozenset({"owner", "admin"})


def _valid_values_for(name: str) -> frozenset[str]:
    """Mode-ladder flags (``*_MODE``) and boolean flags (``*_ENABLED``) have
    disjoint value sets — a boolean flag stuck at "observe" or a mode flag
    stuck at "true" is a silent misconfiguration, not a valid state."""
    return _BOOL_VALUES if name.endswith("_ENABLED") else _MODE_VALUES


class FlagPatch(BaseModel):
    value: str
    reason: str


def _require_operator(request: Request) -> str:
    auth = getattr(request.state, "auth", None)
    if auth is None or auth.is_service or auth.role not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="operator role required")
    return f"operator:{auth.actor_id}"


@router.get("")
def list_controls(request: Request) -> list[dict]:
    _require_operator(request)
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
    if patch.value not in _valid_values_for(name):
        raise HTTPException(status_code=422, detail="invalid value")
    store.set_flag(name, patch.value, actor=actor, reason=patch.reason)
    return {"name": name, "value": patch.value}
