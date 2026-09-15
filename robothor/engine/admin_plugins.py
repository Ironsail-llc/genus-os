"""What is installed, what the engine did with it, and the two acts on it.

A plugin is an object in THIS process. What loaded, what was refused and why,
and which generation of discovery the caches are serving are facts nothing
outside the engine can ask for — the same reason ``admin_channels`` and
``admin_providers`` live here rather than in the bridge, which proxies.

Three rules this module is built around:

* **A reload is the SIGHUP body, not a second copy of it.** ``daemon``'s
  ``perform_plugin_reload`` is called through the module so a test can prove
  this route did not grow its own version. This platform has shipped the other
  arrangement three times — a correct function with a caller that quietly
  diverged — and both spellings looked like they worked.
* **No response carries a path.** The lockfile sits under the operator's home
  directory on a normal install, and where an instance keeps its files is not a
  platform fact. The listing answers ``path_configured`` and ``present``; it
  never answers *where*.
* **Enable and disable do not reload.** They write a row and say
  ``reloaded: false``. The Helm offers the reload as its own act, because an
  operator disabling three plugins should reload once, and because a mutation
  that silently restarted discovery would be a surprise the API had not
  declared.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

__all__ = ["plugin_listing", "register", "reload_plugin_stack", "set_plugin_enabled"]

#: What a distribution name may look like. PyPI names are letters, digits and
#: ``.-_`` separated runs; this reaches a filename comparison and an audit
#: ``action`` value, so anything else is a 422 rather than a lookup that
#: quietly matches nothing.
_DIST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def plugin_listing() -> dict[str, Any]:
    """Every installed plugin distribution and what the lockfile says about it."""
    from robothor.plugins.inventory import inventory
    from robothor.plugins.loader import generation
    from robothor.plugins.lockfile import lockfile_path, read_lockfile

    lock = read_lockfile()
    return {
        "generation": generation(),
        "lockfile": {
            # Whether a path resolves at all, never which one. An instance with
            # no workspace has nowhere to put the file, and that is the only
            # thing a caller can act on.
            "path_configured": lockfile_path() is not None,
            "present": lock.present,
            "malformed": lock.malformed,
            "rows": len(lock.rows),
        },
        "plugins": [row.as_json() for row in inventory()],
    }


def set_plugin_enabled(name: str, enabled: bool) -> dict[str, Any] | None:
    """Flip one lock row, or None when nothing recorded that distribution.

    None is a 404 rather than a created row: a disable that invented a row for
    a name nothing installed would report success and change nothing.
    """
    from robothor.plugins.lockfile import set_enabled

    row = set_enabled(name, enabled)
    if row is None:
        return None
    payload = row.as_json()
    # Stated rather than implied: the running process is still serving the set
    # it discovered, and the caller decides when to reload.
    payload["reloaded"] = False
    return payload


def reload_plugin_stack() -> dict[str, Any]:
    """Re-run discovery and report what the new set holds.

    ``generation`` is None when the reload itself failed — which ``daemon``
    reports rather than raising, because a reload must leave the engine running
    on the plugins it already had.
    """
    from robothor.engine import daemon
    from robothor.plugins.loader import load_plugins

    gen = daemon.perform_plugin_reload()
    result = load_plugins()
    return {
        "generation": gen,
        "loaded": len(result.loaded),
        "failures": [
            {"name": f.name, "group": f.group, "reason": f.reason} for f in result.failures
        ],
    }


def register(app: FastAPI) -> None:
    """Mount the plugin admin routes on the engine app.

    Under ``/api/admin``, which ``engine/auth.py`` requires ``engine:control``
    for — the same gate the provider, channel and scheduler surfaces sit
    behind, inherited from the prefix rather than re-declared here.
    """
    from fastapi import APIRouter, HTTPException

    router = APIRouter(prefix="/api/admin", tags=["admin"])

    def _checked(name: str) -> str:
        if not _DIST_NAME.match(name or ""):
            raise HTTPException(status_code=422, detail="not a distribution name")
        return name

    @router.get("/plugins")
    async def list_plugins() -> dict[str, Any]:
        """What is installed, what loaded, and what the lockfile records."""
        return plugin_listing()

    @router.post("/plugins/reload")
    async def reload() -> dict[str, Any]:
        """Re-discover plugins without restarting. The SIGHUP body, by HTTP."""
        return reload_plugin_stack()

    @router.post("/plugins/{name}/enable")
    async def enable(name: str) -> dict[str, Any]:
        """Let a recorded plugin load again on the next reload."""
        row = set_plugin_enabled(_checked(name), True)
        if row is None:
            raise HTTPException(status_code=404, detail="no lockfile row for that plugin")
        return row

    @router.post("/plugins/{name}/disable")
    async def disable(name: str) -> dict[str, Any]:
        """Stop a plugin being imported at all, from the next reload on."""
        row = set_plugin_enabled(_checked(name), False)
        if row is None:
            raise HTTPException(status_code=404, detail="no lockfile row for that plugin")
        return row

    app.include_router(router)
