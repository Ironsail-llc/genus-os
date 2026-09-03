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
4. Platform-tenant-only: ``feature_flags`` is a single GLOBAL table, not
   scoped per tenant, so a role check alone lets any tenant's owner/admin
   flip every other tenant's guardrails. Human callers are further gated on
   ``AuthContext.tenant_id == PLATFORM_TENANT`` — an owner/admin of any
   other tenant is 403'd here even though their role would otherwise pass.

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
from routers._operator import (  # noqa: F401 - re-exported for existing importers (e.g. conftest.py)
    OPERATOR_ROLES,
    PLATFORM_TENANT,
    require_operator,
)

# keep the private name the handlers already call:
_require_operator = require_operator

router = APIRouter(prefix="/api/controls", tags=["controls"])


class FlagPatch(BaseModel):
    value: str
    reason: str


#: Flags whose unset default is NOT the bottom rung. A compliance control
#: ships enforcing, so reporting "observe" for one nobody has written yet
#: would have this page contradict the engine — an operator would read the
#: opt-out as not-yet-on and go looking for why it never fired.
_UNSET_DEFAULTS: dict[str, str] = {"ROBOTHOR_DNC_MODE": "enforce"}


def _default_value_for(name: str) -> str:
    """A flag-appropriate "unset" default.

    A boolean flag that has never been written defaults to "false", never
    "observe" (which isn't even in its value set). Everything else starts on
    its lowest rung, which is where a flag being promoted through a soak
    genuinely starts — except the flags in ``_UNSET_DEFAULTS``, which ship
    enforcing and must be reported as such.

    This must agree with the engine's own default for the same flag
    (``robothor.engine.feature_flags``); the two are read by different people
    for the same question, and only one of them is the truth.
    """
    if name in _UNSET_DEFAULTS:
        return _UNSET_DEFAULTS[name]
    valid = store.valid_values_for(name)
    return "false" if "false" in valid else "observe"


@router.get("")
def list_controls(request: Request) -> list[dict]:
    _require_operator(request)
    out = []
    for name in sorted(store.GOVERNED_FLAGS):
        value = store.resolve(name) or _default_value_for(name)
        v = verdict(name, value)
        out.append(
            {
                "name": name,
                "value": value,
                "valid_values": list(store.valid_values_for(name)),
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
    if patch.value not in store.valid_values_for(name):
        raise HTTPException(status_code=422, detail="invalid value")
    store.set_flag(name, patch.value, actor=actor, reason=patch.reason)
    return {"name": name, "value": patch.value}
