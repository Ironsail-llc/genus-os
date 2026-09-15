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
import contextlib
import logging
import re
import threading
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from fastapi import FastAPI

#: What the offloaded call gives back — so a handler keeps the return type of
#: the function it hands to the worker instead of flattening to ``Any``.
_T = TypeVar("_T")

logger = logging.getLogger(__name__)

__all__ = [
    "install_plugin",
    "plugin_listing",
    "register",
    "reload_plugin_stack",
    "remove_plugin",
    "set_plugin_enabled",
    "sync_lockfile",
]

#: What a distribution name may look like. PyPI names are letters, digits and
#: ``.-_`` separated runs; this reaches a filename comparison and an audit
#: ``action`` value, so anything else is a 422 rather than a lookup that
#: quietly matches nothing.
_DIST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: A distribution name with an optional exact version, which is all the install
#: route accepts. No slash, no scheme, no space — a browser must not be able to
#: name a filesystem path or a URL, because the one is a file read and the other
#: is the engine fetching on a caller's say-so.
#:
#: The negative lookahead is not decoration: dots are legal in a package name,
#: so ``evil.whl`` matched, reached ``_looks_like_wheel()`` and came back "you
#: need --sha256" — a confusing answer today and a latent file-read footgun if
#: ``sha256`` ever became a route field.
_INSTALL_SPEC = re.compile(
    r"^(?!.*\.whl(?:==|$))[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"(?:==[A-Za-z0-9][A-Za-z0-9._+!-]{0,63})?$"
)

#: Seconds one plugin operation may take, as the CALLER experiences it. Every
#: step inside has its own bound and pip's is deliberately below this one
#: (``installer.PIP_TIMEOUT_SECONDS``, pinned by a test), so the work cannot
#: outlive the answer by more than one uninterruptible step.
_OPERATION_TIMEOUT = 60.0


def _is_version(value: str) -> bool:
    """Whether *value* is a PEP 440 version, through ``packaging``."""
    try:
        from packaging.version import InvalidVersion, Version

        Version(value)
    except ImportError:  # pragma: no cover - packaging ships with pip
        return bool(value) and " " not in value and not value.startswith("-")
    except InvalidVersion:
        return False
    return True


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
            # WHICH damage, in the words the CLI and the doctor already print.
            # `malformed` covers four different faults with four different
            # remedies -- unreadable path, undecodable bytes, invalid JSON, no
            # `plugins` list -- and a page holding only the boolean had to write
            # a fifth sentence covering all of them at once. Never the path:
            # `Lockfile.problem` is built from the error class, not the file.
            "problem": lock.problem or None,
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


async def _bounded(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run one blocking plugin operation with a cap that bounds the WALL CLOCK.

    ``asyncio.wait_for(asyncio.to_thread(...))`` cancels the await, not the
    thread. A hostile review lowered the cap to 0.5 s, made the install sleep
    3 s, and measured the 504 arriving at 3.00 s with the work then running to
    completion in the background: the cap changed the status code and not the
    duration, which is the opposite of what a cap is for, and the operator was
    told an install had failed that had in fact succeeded.

    So the work gets a cancel token it checks between pipeline steps, and this
    stops WAITING at the cap rather than waiting for the thread to notice. The
    thread is bounded from the other end too: every step has its own timeout,
    and pip's is below this cap (``test_pip_cannot_outlive_the_route_cap``), so
    an abandoned thread is short-lived rather than unbounded.

    ``cancel`` is passed only to callables that declare it; enable/disable/sync
    are a single file write and have nothing to check it between.
    """
    import inspect

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    cancel = threading.Event()
    if "cancel" in inspect.signature(fn).parameters:
        kwargs = {**kwargs, "cancel": cancel}
    # What this operation was about, for the abandoned-worker log line.
    # "something failed" is not a line anybody can act on.
    subject = next((str(a) for a in args if isinstance(a, str)), fn.__name__)

    def _run() -> None:
        try:
            value, error = fn(*args, **kwargs), None
        except BaseException as exc:  # noqa: BLE001 - reported to the awaiting caller
            value, error = None, exc
        if cancel.is_set():
            # The caller gave up at the cap. The outcome is logged HERE, from
            # the worker thread, and not from a future callback: by then the
            # request is over, and on a short-lived loop there may be nothing
            # left to run a callback on. An install that was 504'd and then
            # FAILED used to leave no record anywhere -- the listing correctly
            # showed nothing installed and the reason was gone, which makes the
            # 504's "it may still be running, check the listing" only half
            # honest.
            _report_abandoned(fn.__name__, subject, error)
            return
        # The loop may still have gone (a test harness closes one per request);
        # an abandoned worker's last act must not raise inside a daemon thread
        # where nothing can report it.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_settle, future, value, error)

    # A DAEMON thread of our own rather than ``asyncio.to_thread``. The default
    # executor is joined when the loop shuts down, so an abandoned operation
    # would still hold the process at exit -- and in a test harness that closes
    # a loop per request it holds the response too, which is how "the cap does
    # not bound the wall clock" hides.
    threading.Thread(target=_run, name="genus-plugin-op", daemon=True).start()

    done, _pending = await asyncio.wait({future}, timeout=_OPERATION_TIMEOUT)
    if not done:
        cancel.set()
        # Deliberately NOT awaited: the point of the cap is that the caller
        # stops waiting. The token, the per-step timeouts and pip's own cap are
        # what bound the thread. Its OUTCOME is logged by the worker itself;
        # this callback only consumes a result that raced the cap, so Python
        # does not report it as an exception nobody retrieved.
        future.add_done_callback(_swallow)
        raise TimeoutError
    # The future is untyped at the bridge (it is settled from another thread),
    # so the cast is where the worker's return type is reasserted.
    result: _T = await future
    return result


def _settle(future: asyncio.Future[Any], value: Any, exc: BaseException | None) -> None:
    """Deliver a worker's outcome, unless the caller has already given up."""
    if future.done():
        return
    if exc is not None:
        future.set_exception(exc)
    else:
        future.set_result(value)


def _report_abandoned(operation: str, subject: str, error: BaseException | None) -> None:
    """Say what became of a worker the caller stopped waiting for.

    The 504 tells the operator the work may still be running and to re-read the
    listing. That is only honest if the other outcome leaves a trace: an install
    that was capped and then FAILED used to vanish entirely -- the listing
    correctly showed nothing installed, and the reason was gone.
    """
    if error is not None:
        logger.warning(
            "Plugin %s for %r was abandoned at the request cap and then failed: %s: %s",
            operation,
            subject,
            type(error).__name__,
            error,
        )
    else:
        logger.warning(
            "Plugin %s for %r was abandoned at the request cap but COMPLETED "
            "afterwards; the caller was told it did not finish.",
            operation,
            subject,
        )


def _swallow(future: asyncio.Future[Any]) -> None:
    """Consume an abandoned future's result so it is not logged as "never retrieved".

    Only reached when a worker settled the future in the moment the cap fired.
    The OUTCOME is reported by :func:`_report_abandoned` from the worker thread,
    which is the one place that still exists once the request is over.
    """
    with contextlib.suppress(BaseException):
        future.result()


def install_plugin(
    name: str,
    version: str | None = None,
    index: str | None = None,
    accept_review: bool = False,
    dry_run: bool = False,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Install one plugin from a signed index, and answer the plan.

    A distribution NAME and nothing that could become a path. The CLI can be
    handed a wheel on disk because the operator is standing at the box; a
    browser naming a filesystem path would be a dashboard reading any file the
    engine can reach, and a browser naming a URL would be the engine fetching
    on a caller's say-so. Both stay on the CLI.

    ``include_command`` is deliberately not passed to ``as_json``: the pip
    command holds a temp directory and the interpreter's location, and no
    response here carries a path.
    """
    from robothor.plugins import installer

    outcome = installer.install(
        name,
        version=version,
        index=index,
        accept_review=accept_review,
        dry_run=dry_run,
        cancel=cancel,
    )
    return outcome.as_json()


def remove_plugin(name: str, force: bool = False) -> dict[str, Any]:
    """Uninstall one plugin this platform installed, and drop its row."""
    from robothor.plugins import installer

    return installer.remove(name, force=force)


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
            {
                "name": f.name,
                "group": f.group,
                "reason": f.reason,
                # `name` is the ENTRY POINT. One distribution appears here once
                # per group it publishes into, and `genus-hostinfo` shows up as
                # `hostinfo`, so without this there is no join key at all and
                # every consumer has to invent one. None means the loader could
                # not name the distribution, never that it did not look.
                "distribution": f.distribution,
            }
            for f in result.failures
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

    async def _write(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Run one blocking plugin operation off the loop, reporting OSError.

        Everything in this module walks ``importlib.metadata`` over every
        distribution on ``sys.path``, reads files, and — on a reload, and on a
        listing that meets a distribution installed since boot — runs
        ``ep.load()``, which executes third-party module bodies with no bound
        at all. ``admin_providers`` hands exactly this class of work to
        ``asyncio.to_thread``; doing it inline here meant one slow package
        stalled every other request the engine was serving.
        """
        import subprocess

        from robothor.plugins.installer import InstallError
        from robothor.plugins.registry import RegistryError

        try:
            return await _bounded(fn, *args, **kwargs)
        except LockfileRefusedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (InstallError, RegistryError) as exc:
            # A blocked wheel, an unverifiable index, a hash that did not
            # match. Every one of those is a correct ANSWER about the caller's
            # request, so it is a 422 with the sentence rather than a 500 with
            # a traceback — and the sentence is what the Helm renders.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            # A SubprocessError, NOT an OSError, so it slid past the handler
            # below and would have been a bare 500 with a traceback.
            raise HTTPException(
                status_code=504,
                detail="pip did not finish within its timeout; nothing was recorded",
            ) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail=(
                    f"the plugin operation did not finish within {_OPERATION_TIMEOUT:.0f}s "
                    "and was cancelled; it may still be running, so check "
                    "`GET /api/admin/plugins` before retrying"
                ),
            ) from exc
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

    @router.post("/plugins/install")
    async def install(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Install one plugin from a signed index.

        Declared before ``/{name}/...`` so the literal path is matched as
        itself. The body takes a distribution NAME (optionally ``name==version``
        via the separate ``version`` field) and nothing that could become a
        path: installing a wheel off the filesystem is a CLI act, because a
        browser naming a path would be a dashboard reading any file the engine
        can reach.
        """
        body = payload or {}
        spec = str(body.get("name") or "").strip()
        if not _INSTALL_SPEC.match(spec):
            raise HTTPException(
                status_code=422,
                detail=(
                    "name must be a distribution name (optionally name==version). "
                    "Installing a wheel from a path or a URL is a CLI-only act."
                ),
            )
        version = body.get("version")
        # PEP 440, not the distribution-name pattern: that rejected ``+``, so a
        # legitimate local version like ``1.0+acme1`` could not be installed
        # over HTTP although the inline ``name==version`` form allowed it.
        if version is not None and not _is_version(str(version)):
            raise HTTPException(status_code=422, detail="not a version")
        index = body.get("index")
        if index is not None and not str(index).startswith("https://"):
            raise HTTPException(status_code=422, detail="index must be an https URL")
        return await _write(
            install_plugin,
            spec,
            version=None if version is None else str(version),
            index=None if index is None else str(index),
            accept_review=bool(body.get("accept_review")),
            dry_run=bool(body.get("dry_run")),
        )

    @router.post("/plugins/{name}/remove")
    async def remove(name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Uninstall a plugin this platform installed and drop its row."""
        body = payload or {}
        return await _write(remove_plugin, _checked(name), force=bool(body.get("force")))

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
