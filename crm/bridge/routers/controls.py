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


# The unset-default rule moved WHOLE into ``robothor.flags.store``, beside
# ``valid_values_for`` and ``resolve``. Three surfaces need it — this page, the
# Settings page and ``genus config get`` — and the two that are not the bridge
# must not import a bridge module, so a copy here would have been a second
# answer to "what does the engine run when nobody has written this flag". The
# names stay bound here because this module's tests and importers call them.
_UNSET_DEFAULTS = store._UNSET_DEFAULTS
_declared_default = store._declared_default
_default_value_for = store.default_value_for
# Bound to the REAL store on purpose: ``store`` itself is monkeypatched by the
# suite's ``fake_store`` fixture to fake the DB layer, and spelling a flag's
# value is not the part being faked.
_normalise = store.normalise


@router.get("")
def list_controls(request: Request) -> list[dict[str, object]]:
    _require_operator(request)
    out: list[dict[str, object]] = []
    for name in sorted(store.GOVERNED_FLAGS):
        # ``normalise`` subsumes the old ``resolve(name) or _default_value_for(name)``
        # and adds the case that rule missed: a variable set to something outside
        # the value set (``ROBOTHOR_RBAC_MODE=ALERT``) rendered a picker whose
        # current selection was not one of its options. The settings API resolves
        # governed flags the same way, so the two pages cannot disagree.
        value = _normalise(name, store.resolve(name))
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
def set_control(name: str, patch: FlagPatch, request: Request) -> dict[str, str]:
    actor = _require_operator(request)
    if name not in store.GOVERNED_FLAGS:
        raise HTTPException(status_code=404, detail="unknown flag")
    if patch.value not in store.valid_values_for(name):
        raise HTTPException(status_code=422, detail="invalid value")
    store.set_flag(name, patch.value, actor=actor, reason=patch.reason)
    return {"name": name, "value": patch.value}
