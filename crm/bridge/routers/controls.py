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


#: Flags whose engine default deliberately differs from the value their settings
#: field declares. ``ROBOTHOR_DNC_MODE`` is declared ``observe`` like every other
#: ladder, and ``feature_flags.do_not_contact_mode`` floors it at ``enforce``
#: because a compliance opt-out has no dark rung. Only that class of flag belongs
#: here: an entry that merely repeats the declaration is dead weight, and
#: ``test_controls_unset_defaults`` fails on one.
_UNSET_DEFAULTS: dict[str, str] = {"ROBOTHOR_DNC_MODE": "enforce"}


def _declared_default(name: str) -> str | None:
    """What ``robothor/settings/model.py`` says this flag is when nobody sets it.

    One source, read rather than restated. The registry is also where
    ``GOVERNED_FLAGS`` itself comes from, so every flag this page can render has
    a declared default by construction.
    """
    try:
        from robothor.settings.registry import field_index

        record = field_index().get(name)
    except Exception:  # noqa: BLE001 — the page must render without the model
        return None
    if record is None:
        return None
    declared = record.get("default")
    if isinstance(declared, bool):
        return "true" if declared else "false"
    text = str(declared or "").strip()
    return text or None


def _default_value_for(name: str) -> str:
    """A flag-appropriate "unset" default — what the ENGINE runs, not a guess.

    Reached only when there is neither a DB row nor an environment variable
    (``store.resolve`` covers both), so the answer is the flag's own hardcoded
    default and this page must show exactly that. It is DERIVED from the settings
    registry rather than inferred from the shape of the value set: the old rule
    ("boolean → false, else observe") was right for every flag that starts dark
    and gets promoted, and it silently became wrong the first time a governed
    flag shipped at ``enforce``, so the page would have contradicted the engine
    with no test noticing.

    ``_UNSET_DEFAULTS`` still wins, for the flags whose engine accessor
    deliberately ignores the declared value. The heuristic survives only as the
    last resort for a declaration that is empty or outside the value set —
    ``ROBOTHOR_SANDBOX_DEFAULT_MODE`` declares ``""``.
    """
    if name in _UNSET_DEFAULTS:
        return _UNSET_DEFAULTS[name]
    valid = store.valid_values_for(name)
    declared = _declared_default(name)
    if declared is not None and declared in valid:
        return declared
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
