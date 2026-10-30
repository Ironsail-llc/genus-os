"""Installed plugins, from the Helm.

Every answer here is the engine's. A plugin is an object in THAT process: what
loaded, what was refused and why, and which discovery generation the caches are
serving cannot be asked from outside it. So this router is three things and
nothing else — a gate, a proxy, and an audit trail.

The gate is the half worth arguing about. ``disable`` takes a capability out of
service on the next reload, and ``reload`` re-imports third-party code into a
running daemon; a non-operator reaching either would be changing what the
appliance executes. So every route's first statement is ``require_operator``,
and the tenant dependency refuses a second tenant outright: one engine runs one
set of plugins, and another tenant's operator disabling one would be acting on
somebody else's instance. The same shape ``agent_manifests`` and
``channel_access`` are built on.

Nothing here reads the lockfile, and nothing here decides anything. A second
implementation of "which plugins are installed" living in the bridge would
answer from a different process's view of site-packages — which is how a
dashboard comes to disagree with the engine it is describing.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from deps import get_tenant_id
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from routers._audit import audited
from routers._engine_client import engine_request
from routers._operator import require_operator

logger = logging.getLogger(__name__)


def _require_primary_tenant(tenant_id: str = Depends(get_tenant_id)) -> None:
    """Plugins are appliance-global, so deny secondary tenants.

    Symmetric with ``agent_manifests`` and ``channel_access``: one engine runs
    one set of plugins, and a second tenant's operator disabling one would be
    taking a capability out of somebody else's instance.
    """
    from robothor.constants import DEFAULT_TENANT

    if tenant_id != DEFAULT_TENANT:
        raise HTTPException(
            status_code=403,
            detail="appliance administration not authorized for tenant",
        )


router = APIRouter(
    prefix="/api/plugins",
    tags=["plugins"],
    dependencies=[Depends(_require_primary_tenant)],
)

#: What a distribution name may look like. PyPI names are letters and digits
#: separated by ``.-_``; the value reaches a proxied URL path and an audit
#: ``action``, so anything else is a 422 here rather than a request the engine
#: has to decide about.
_DIST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: What ``install`` accepts: a distribution name with an optional exact
#: version. No slash, no scheme, no space. Checked here as well as in the
#: engine because a refusal the browser gets is a refusal that never became a
#: request, and because the two surfaces must not disagree about what a plugin
#: name is.
_INSTALL_SPEC = re.compile(
    r"^(?!.*\.whl(?:==|$))[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"(?:==[A-Za-z0-9][A-Za-z0-9._+!-]{0,63})?$"
)


def _proxied(status: int, body: Any) -> JSONResponse:
    """The engine's answer, with its status code. Never manufactured here."""
    return JSONResponse(status_code=status, content=body)


def _name(value: str) -> str:
    if not _DIST_NAME.match(value or ""):
        raise HTTPException(status_code=422, detail="not a distribution name")
    return value


def _optional(value: Any) -> str | None:
    """A body field as a string, or None when it was absent or empty.

    Empty-string-as-absent matters: a form that posts ``version=""`` must mean
    "any version", not "the version whose name is the empty string", which the
    engine would refuse with a message about the wrong thing.
    """
    text = str(value or "").strip()
    return text or None


async def _body(request: Request) -> dict[str, Any]:
    """The request's JSON object, or ``{}``.

    A POST with no body at all is the normal shape for ``remove`` with no
    options, and it must not be a 422 about malformed JSON.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - an absent or unparseable body is "no options"
        return {}
    return payload if isinstance(payload, dict) else {}


@router.get("")
async def list_plugins(request: Request) -> JSONResponse:
    """What is installed, what loaded, and what the lockfile records.

    Not audited: a listing is not an act, and an audit row per page load would
    bury the disable that matters.
    """
    require_operator(request)
    status, body = await engine_request("GET", "/api/admin/plugins")
    return _proxied(status, body)


@router.post("/sync")
async def sync_plugins(request: Request) -> JSONResponse:
    """Record the installed distributions in the lockfile.

    The action a fresh install opens with: until a sync has run, nothing is
    recorded, the lockfile governs nothing, and every enable/disable is a 404 —
    so without this route the Plugins page was read-only until somebody got a
    shell on the box.

    No ``force``. The engine refuses when the existing file holds intent it
    cannot read, and an operator discarding recorded disables should be reading
    the doctor's line about what it costs first; that escape stays on the CLI.

    Declared before ``/{name}/...`` so the literal path is matched as itself.
    """
    require_operator(request)
    status, body = await engine_request("POST", "/api/admin/plugins/sync")
    audited(
        request,
        "plugin.sync",
        action="sync",
        status="ok" if status < 400 else "error",
    )
    return _proxied(status, body)


@router.post("/reload")
async def reload_plugins(request: Request) -> JSONResponse:
    """Re-discover plugins in the running engine, without a restart.

    Declared before ``/{name}/...`` so the literal path is matched as itself.
    """
    require_operator(request)
    status, body = await engine_request("POST", "/api/admin/plugins/reload")
    audited(
        request,
        "plugin.reload",
        action="reload",
        status="ok" if status < 400 else "error",
    )
    return _proxied(status, body)


@router.post("/install")
async def install_plugin(request: Request) -> JSONResponse:
    """Install a plugin from a signed index.

    Declared before ``/{name}/...`` so the literal path is matched as itself.

    The body takes a distribution NAME and nothing that could become a path or
    a URL, and that is checked HERE as well as in the engine: a browser naming
    a filesystem path would be a dashboard reading any file the engine can
    reach, and one naming a URL would be the engine fetching on a caller's
    say-so. Installing a wheel from disk stays on the CLI, where the operator
    is standing at the box.
    """
    require_operator(request)
    body = await _body(request)
    name = str(body.get("name") or "").strip()
    if not _INSTALL_SPEC.match(name):
        raise HTTPException(
            status_code=422,
            detail=(
                "name must be a distribution name (optionally name==version). "
                "Installing a wheel from a path or a URL is a CLI-only act."
            ),
        )
    index = body.get("index")
    if index is not None and not str(index).startswith("https://"):
        raise HTTPException(status_code=422, detail="index must be an https URL")

    status, payload = await engine_request(
        "POST",
        "/api/admin/plugins/install",
        json={
            "name": name,
            "version": _optional(body.get("version")),
            "index": _optional(index),
            "accept_review": bool(body.get("accept_review")),
            "dry_run": bool(body.get("dry_run")),
        },
    )
    # Identifiers and the decision, never the engine's whole answer: the scan
    # reasons name files inside a wheel and the plan carries a hash and a
    # filename, none of which is what an audit trail is for.
    plan = payload.get("plan") if isinstance(payload, dict) else None
    audited(
        request,
        "plugin.install",
        action=name,
        plugin=name,
        version=str((plan or {}).get("version") or ""),
        verdict=str((plan or {}).get("verdict") or ""),
        status="ok" if status < 400 else "error",
    )
    return _proxied(status, payload)


@router.post("/{name}/remove")
async def remove_plugin(name: str, request: Request) -> JSONResponse:
    """Uninstall a plugin this platform installed and drop its lockfile row."""
    require_operator(request)
    plugin = _name(name)
    body = await _body(request)
    status, payload = await engine_request(
        "POST",
        f"/api/admin/plugins/{plugin}/remove",
        json={"force": bool(body.get("force"))},
    )
    audited(
        request,
        "plugin.remove",
        action=plugin,
        plugin=plugin,
        status="ok" if status < 400 else "error",
    )
    return _proxied(status, payload)


@router.post("/{name}/enable")
async def enable_plugin(name: str, request: Request) -> JSONResponse:
    """Let a recorded plugin load again on the next reload."""
    require_operator(request)
    plugin = _name(name)
    status, body = await engine_request("POST", f"/api/admin/plugins/{plugin}/enable")
    audited(
        request,
        "plugin.enable",
        action=plugin,
        plugin=plugin,
        status="ok" if status < 400 else "error",
    )
    return _proxied(status, body)


@router.post("/{name}/disable")
async def disable_plugin(name: str, request: Request) -> JSONResponse:
    """Stop a plugin being imported at all, from the next reload on."""
    require_operator(request)
    plugin = _name(name)
    status, body = await engine_request("POST", f"/api/admin/plugins/{plugin}/disable")
    audited(
        request,
        "plugin.disable",
        action=plugin,
        plugin=plugin,
        status="ok" if status < 400 else "error",
    )
    return _proxied(status, body)
