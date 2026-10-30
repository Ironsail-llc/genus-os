"""Installed plugins, from the Helm.

Every answer here is the engine's. A plugin is an object in THAT process: what
loaded, what was refused and why, and which discovery generation the caches are
serving cannot be asked from outside it. So this router is three things and
nothing else — a gate, a proxy, and an audit trail.

Every tenant runs its own engine, so the gate splits in two. Which plugins are
on is the ACTING tenant's own administration: the listing, enable, disable and
reload take ``require_tenant_admin`` and go to that tenant's engine
(``tenant=get_tenant_id(request)``, the pattern ``agent_manifests`` uses), so a
child admin, or a root admin switched into the child, reaches the child's
engine and nobody else's. What is on the BOX is not one tenant's: install,
remove and sync keep ``require_root_operator`` and act on the root's engine
unless the body names another tenant in the root's tree (``tenant_id``).

The root also decides for its children. ``PUT /{name}/tenants/{tenant_id}``
forces a plugin on or off for one child, or clears the grant, in
``tenant_plugin_grants`` (``robothor.plugins.grants``) — never in
``crm_tenants.settings``, which a child admin may PATCH and whose save replaces
the whole object. The child's engine is reloaded straight after, so the grant
applies now; a reload that fails is in the answer rather than hidden behind
the grant's success.

Nothing here reads the lockfile, and nothing here decides anything. A second
implementation of "which plugins are installed" living in the bridge would
answer from a different process's view of site-packages — which is how a
dashboard comes to disagree with the engine it is describing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from deps import get_tenant_id
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from robothor.plugins import grants as plugin_grants
from routers import _operator
from routers._audit import audited
from routers._engine_client import engine_request
from routers._operator import require_root_operator, require_tenant_admin

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/plugins",
    tags=["plugins"],
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


async def _root_tree() -> tuple[str, ...]:
    """The platform tenant and its descendants, read off the event loop.

    ``resolve_accessible_tenants`` walks ``crm_tenants`` with psycopg2, which
    must not block the loop that is serving every other request.
    """
    from robothor.engine.permissions import resolve_accessible_tenants

    root = _operator.PLATFORM_TENANT
    return tuple(await asyncio.to_thread(resolve_accessible_tenants, root, "owner"))


async def _target_tenant(body: dict[str, Any]) -> str:
    """Whose engine a root-only act is for: ``tenant_id`` in the body, else the root.

    404 rather than 403 for a tenant outside the root's tree, for the reason
    ``tenants.api_get_tenant`` gives: whether it exists is not this caller's
    business to learn here.
    """
    named = _optional(body.get("tenant_id"))
    if named is None:
        return str(_operator.PLATFORM_TENANT)
    if named not in await _root_tree():
        raise HTTPException(status_code=404, detail="no such tenant")
    return named


@router.get("")
async def list_plugins(request: Request) -> JSONResponse:
    """What is installed, what loaded, and what the lockfile records.

    Not audited: a listing is not an act, and an audit row per page load would
    bury the disable that matters.
    """
    require_tenant_admin(request)
    status, body = await engine_request("GET", "/api/admin/plugins", tenant=get_tenant_id(request))
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
    require_root_operator(request)
    tenant = await _target_tenant(await _body(request))
    status, body = await engine_request("POST", "/api/admin/plugins/sync", tenant=tenant)
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
    require_tenant_admin(request)
    status, body = await engine_request(
        "POST", "/api/admin/plugins/reload", tenant=get_tenant_id(request)
    )
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
    require_root_operator(request)
    body = await _body(request)
    tenant = await _target_tenant(body)
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
        tenant=tenant,
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
    require_root_operator(request)
    plugin = _name(name)
    body = await _body(request)
    tenant = await _target_tenant(body)
    status, payload = await engine_request(
        "POST",
        f"/api/admin/plugins/{plugin}/remove",
        json={"force": bool(body.get("force"))},
        tenant=tenant,
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
    """Let a recorded plugin load again on the next reload, in the acting tenant.

    The engine answers 409 for a plugin the root tenant forced; that answer is
    passed through, so the page can say who decided.
    """
    require_tenant_admin(request)
    plugin = _name(name)
    status, body = await engine_request(
        "POST", f"/api/admin/plugins/{plugin}/enable", tenant=get_tenant_id(request)
    )
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
    require_tenant_admin(request)
    plugin = _name(name)
    status, body = await engine_request(
        "POST", f"/api/admin/plugins/{plugin}/disable", tenant=get_tenant_id(request)
    )
    audited(
        request,
        "plugin.disable",
        action=plugin,
        plugin=plugin,
        status="ok" if status < 400 else "error",
    )
    return _proxied(status, body)


@router.put("/{name}/tenants/{tenant_id}")
async def grant_plugin(name: str, tenant_id: str, request: Request) -> JSONResponse:
    """Force a plugin on or off for one child tenant, or clear the grant.

    Body ``{"state": "on" | "off" | null}``. Root only, at home: a root admin
    acting in a child is that child's administrator for the request, and a
    child deciding its own grant would make the grant decoration. The target
    must be a descendant of the root; the root decides for itself with enable
    and disable.

    The grant is written, audited under the CHILD (so its admins can see who
    decided), and then the child's engine is reloaded so it applies now. The
    reload's outcome is in the answer (``reloaded``, ``reload_status``,
    ``reload``): the grant stands either way, and the next reload or restart of
    that engine applies it.
    """
    actor = require_root_operator(request)
    plugin = _name(name)
    body = await _body(request)
    if "state" not in body or body["state"] not in (*plugin_grants.GRANT_STATES, None):
        raise HTTPException(status_code=422, detail='state must be "on", "off" or null')
    state: str | None = body["state"]
    if tenant_id == _operator.PLATFORM_TENANT:
        raise HTTPException(
            status_code=422,
            detail="a grant is for a child tenant; the root tenant uses enable and disable",
        )
    if tenant_id not in await _root_tree():
        raise HTTPException(status_code=404, detail="no such tenant")

    written = True
    try:
        if state is None:
            await asyncio.to_thread(plugin_grants.clear_grant, tenant_id, plugin)
        else:
            await asyncio.to_thread(plugin_grants.set_grant, tenant_id, plugin, state, actor)
    except Exception as exc:  # noqa: BLE001 - the class only; the text can carry a DSN
        logger.warning("Plugin grant could not be written (%s)", type(exc).__name__)
        written = False
    audited(
        request,
        "plugin.grant",
        action=plugin,
        plugin=plugin,
        tenant=tenant_id,
        state=state,
        status="ok" if written else "error",
        audit_tenant_id=tenant_id,
    )
    if not written:
        return JSONResponse(
            status_code=503, content={"error": "the plugin grant could not be written"}
        )

    reload_status, reload_body = await engine_request(
        "POST", "/api/admin/plugins/reload", tenant=tenant_id
    )
    return JSONResponse(
        status_code=200,
        content={
            "plugin": plugin,
            "tenant_id": tenant_id,
            "state": state,
            "reloaded": reload_status < 400,
            "reload_status": reload_status,
            "reload": reload_body,
        },
    )
