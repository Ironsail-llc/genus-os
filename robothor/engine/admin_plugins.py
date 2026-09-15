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

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from fastapi import FastAPI

#: What the offloaded call gives back — so a handler keeps the return type of
#: the function it hands to the worker instead of flattening to ``Any``.
_T = TypeVar("_T")

logger = logging.getLogger(__name__)

__all__ = [
    "plugin_listing",
    "register",
    "reload_plugin_stack",
    "set_plugin_enabled",
    "sync_lockfile",
]

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


class LockfileRefusedError(Exception):
    """``sync`` declined to write, and why. Becomes a 409, never a traceback."""


def sync_lockfile(force: bool = False) -> dict[str, Any]:
    """Record every installed distribution, and report what changed.

    Raises :class:`LockfileRefusedError` rather than writing when the existing file
    holds intent it cannot read — rewriting it would silently re-enable every
    plugin the operator had turned off. The reason never names the path: which
    distributions are installed is a platform fact, where the file lives is not.
    """
    from robothor.plugins.lockfile import sync

    result = sync(force=force)
    if not result.ok:
        raise LockfileRefusedError(result.refused or "the lockfile could not be written")
    return {
        "recorded": [row.name for row in result.recorded],
        "added": list(result.added),
        "updated": list(result.updated),
        "removed": list(result.removed),
        # Recording is not applying. The running engine keeps serving the set
        # it discovered until something reloads it.
        "reloaded": False,
    }


def reload_plugin_stack() -> dict[str, Any]:
    """Re-run discovery and report what the new set holds.

    ``generation`` is None when the reload itself failed — which ``daemon``
    reports rather than raising, because a reload must leave the engine running
    on the plugins it already had.
    """
    from robothor.engine import daemon
    from robothor.plugins.loader import load_plugins

    gen = daemon.perform_plugin_reload()
    # The bare call takes the per-group built-in reserved names, so this
    # answers with the refusals production actually applies.
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

    async def _write(fn: Callable[..., _T], *args: Any) -> _T:
        """Run one blocking plugin operation off the loop, reporting OSError.

        Everything in this module walks ``importlib.metadata`` over every
        distribution on ``sys.path``, reads files, and — on a reload, and on a
        listing that meets a distribution installed since boot — runs
        ``ep.load()``, which executes third-party module bodies with no bound
        at all. ``admin_providers`` hands exactly this class of work to
        ``asyncio.to_thread``; doing it inline here meant one slow package
        stalled every other request the engine was serving.
        """
        try:
            return await asyncio.to_thread(fn, *args)
        except LockfileRefusedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            # A lockfile path that is a directory, a read-only filesystem, a
            # full disk. The message names the error class, never the path.
            raise HTTPException(
                status_code=503,
                detail=f"the plugin lockfile could not be written ({type(exc).__name__})",
            ) from exc

    @router.get("/plugins")
    async def list_plugins() -> dict[str, Any]:
        """What is installed, what loaded, and what the lockfile records."""
        return await _write(plugin_listing)

    @router.post("/plugins/reload")
    async def reload() -> dict[str, Any]:
        """Re-discover plugins without restarting. The SIGHUP body, by HTTP."""
        return await _write(reload_plugin_stack)

    @router.post("/plugins/sync")
    async def sync() -> dict[str, Any]:
        """Record the installed distributions. What makes the Helm page usable
        on a fresh install, where nothing is recorded and every enable is a
        404. Deliberately not ``force``: an operator discarding recorded
        disables should have read the doctor line first, so that escape lives
        on the CLI."""
        return await _write(sync_lockfile)

    @router.post("/plugins/{name}/enable")
    async def enable(name: str) -> dict[str, Any]:
        """Let a recorded plugin load again on the next reload."""
        row = await _write(set_plugin_enabled, _checked(name), True)
        if row is None:
            raise HTTPException(status_code=404, detail="no lockfile row for that plugin")
        return row

    @router.post("/plugins/{name}/disable")
    async def disable(name: str) -> dict[str, Any]:
        """Stop a plugin being imported at all, from the next reload on."""
        row = await _write(set_plugin_enabled, _checked(name), False)
        if row is None:
            raise HTTPException(status_code=404, detail="no lockfile row for that plugin")
        return row

    app.include_router(router)
