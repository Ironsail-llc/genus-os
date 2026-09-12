"""Everything a check is allowed to reach, and the seams the suite replaces.

Three rules shape this object.

**Every dependency is lazy.** The doctor's whole job is to run on an instance
that does not work, so importing it must not need a database, a vault, an
engine or a settings file that parses. Nothing here connects, imports psycopg2
or resolves settings until a check asks.

**Blocking work runs on a DAEMON thread.** The runner time-boxes each check
with :func:`asyncio.timeout`, which can only cancel a coroutine -- it cannot
interrupt a blocking ``psycopg2.connect`` or a ``urlopen`` already in flight.
Handed to ``asyncio.to_thread``, such a call keeps its worker alive, and the
default executor is joined at interpreter exit: the timeout would report the
failure and the process would then hang on the very call that timed out. So
:meth:`DoctorContext.run_blocking` spawns its own daemon thread, which the
interpreter abandons at exit. Every blocking call ALSO carries its own timeout;
the thread is the backstop, not the plan.

**The seams are constructor arguments.** ``db_factory`` and ``http_fetch``
default to the real thing and are replaced wholesale in tests, so no check has
to know whether it is talking to PostgreSQL or to a dictionary.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic as _monotonic
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from robothor.settings.model import GenusSettings

__all__ = ["DoctorContext", "HttpResponse"]

#: Seconds a single check may take before the runner calls it failed.
DEFAULT_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class HttpResponse:
    """The little a check needs to know about an HTTP answer.

    ``status`` is 0 when the request never completed; ``error`` then names the
    exception type and message. No check inspects headers, and none should:
    every HTTP question here is "is it up, and did it say yes".
    """

    status: int
    body: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class DoctorContext:
    """What a check may reach, and how long it has to do it.

    Args:
        timeout_s: per-check budget. Also handed to the DB connect, the HTTP
            fetch and the host script, so a blocking dependency cannot outlive
            the check that started it by much.
        dry_run: report what ``--fix`` would do and change nothing.
        offline: do not spend money, leave the box, or start an external
            process. ``provider.completion`` and the Telegram ``getMe`` skip,
            and so does the host-unit script -- forking a walk of every unit
            and drop-in is the same class of expense for a polled endpoint as
            an upstream call. Local probes (loopback services, Ollama, the
            database) still run, which is what an install gate, a CI run and
            the bridge's ``GET /api/doctor`` need.
        total_timeout_s: a budget for the WHOLE run, on top of the per-check
            one. Without it the worst case is ``len(checks) * timeout_s`` --
            over two minutes here -- which is fine for an operator at a
            terminal and not fine for an HTTP handler holding a worker thread.
            None means only the per-check budget applies.
        fix: run the repair for failed fixable checks.
    """

    timeout_s: float = DEFAULT_TIMEOUT_S
    total_timeout_s: float | None = None
    dry_run: bool = False
    offline: bool = False
    fix: bool = False
    #: Whether this instance is supposed to have its long-running services up.
    #: ``None`` -- the default, and what the CLI passes -- means "ask the box":
    #: a host carrying the systemd units is meant to be serving, a bare wheel
    #: install is not. ``genus init`` sets it explicitly, because it is the one
    #: caller that KNOWS: without ``--start`` it deliberately starts nothing,
    #: and failing the install on daemons it chose not to launch is how the
    #: documented quickstart came to exit 1 on a complete, working instance.
    services_expected: bool | None = None
    db_factory: Callable[[], Any] | None = None
    http_fetch: Callable[[str, float], HttpResponse] | None = None
    _settings: Any = field(default=None, repr=False)
    #: The run's worker (``runner._Worker``), or None outside a run. Set by
    #: :func:`robothor.doctor.runner.run` so that ``run_blocking`` can tell
    #: whether it is already off the timing loop.
    _worker: Any = field(default=None, repr=False)
    #: The run's worker pool (``runner._WorkerPool``), or None outside a run.
    _pool: Any = field(default=None, repr=False)
    #: Monotonic instant the whole run must be finished by. Set by
    #: :func:`robothor.doctor.runner.run` from ``total_timeout_s``; None when
    #: only the per-check budget applies. Kept here rather than threaded
    #: through every call so that the per-check box, a repair and its re-run
    #: all narrow against ONE number -- the version that passed a budget down
    #: by argument let ``--fix`` overshoot the deadline by about twice
    #: ``timeout_s``, because the fix and the recheck each got a fresh one.
    _deadline: float | None = field(default=None, repr=False)

    def budget(self) -> float:
        """Seconds the next thing may take: the per-check box, narrowed by
        whatever is left of the run. Never negative -- zero means the deadline
        has passed and the caller should report, not start."""
        if self._deadline is None:
            return self.timeout_s
        remaining = self._deadline - _monotonic()
        return max(0.0, min(self.timeout_s, remaining))

    def expired(self) -> bool:
        """Has the run's total budget been spent?"""
        return self._deadline is not None and _monotonic() >= self._deadline

    @property
    def settings(self) -> GenusSettings:
        """The instance's typed settings, resolved once.

        Raises whatever :func:`robothor.settings.get_settings` raises -- a
        config.yaml that does not parse, a key nothing declares. That is not
        caught here: ``config.settings_load`` is the check whose job is to
        report it, and every other check is entitled to assume settings exist.
        """
        if self._settings is None:
            from robothor.settings import get_settings

            self._settings = get_settings()
        settings: GenusSettings = self._settings
        return settings

    @property
    def workspace(self) -> Path:
        """The instance's workspace directory, as the settings declare it."""
        return Path(self.settings.paths.workspace)

    def db(self) -> Any:
        """A context manager yielding a database connection.

        Defaults to the platform pool. A check must use it as
        ``with ctx.db() as conn:`` and must run it through
        :meth:`run_blocking` -- psycopg2 is synchronous, and a connect to a
        host that is dropping packets blocks for the OS timeout, not ours.
        """
        if self.db_factory is not None:
            return self.db_factory()
        from robothor.db.connection import get_connection

        return get_connection()

    def fetch(self, url: str) -> HttpResponse:
        """GET ``url`` with this run's timeout. Blocking; call via
        :meth:`run_blocking`."""
        if self.http_fetch is not None:
            return self.http_fetch(url, self.timeout_s)
        return _urllib_fetch(url, self.timeout_s)

    async def run_blocking(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run ``fn`` without letting it block the loop that is timing us.

        Two implementations, and which one runs is decided by where the caller
        already is.

        **Inside a check**, the whole check body is already executing on the
        run's private worker loop (see ``runner._Worker``), and the loop holding
        the timeout is a different one. Blocking work can therefore run INLINE:
        it blocks only the worker, the timeout still fires on time, and a check
        that calls this five times costs no threads at all. The first version
        spawned a thread per call, which is where "+2 threads per abandoned
        check" came from.

        **Anywhere else** -- a test invoking a check directly, or any caller
        with no worker attached -- it falls back to a daemon thread, because
        there the loop being blocked WOULD be the one holding the timeout.
        """
        worker = self._worker
        if worker is not None and worker.owns_current_loop():
            return fn(*args)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        def _deliver(setter: Callable[[Any], None], value: Any) -> None:
            if not future.done():
                setter(value)

        def _worker() -> None:
            try:
                value = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - relayed to the awaiter
                loop.call_soon_threadsafe(_deliver, future.set_exception, exc)
            else:
                loop.call_soon_threadsafe(_deliver, future.set_result, value)

        threading.Thread(target=_worker, name="genus-doctor-check", daemon=True).start()
        return await future


def _urllib_fetch(url: str, timeout: float) -> HttpResponse:
    """One GET, with urllib rather than httpx.

    stdlib on purpose: the doctor must import and run inside a broken
    installation, and httpx is a dependency of the engine, not of the CLI.
    An HTTP error status is an ANSWER, not an exception -- a 401 from an
    auth-gated endpoint proves the service is up, which is the whole point of
    ``robothor.config.probe_service``.
    """
    import urllib.error
    import urllib.request

    try:
        request = urllib.request.Request(url, method="GET")  # noqa: S310 - caller builds the URL
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read(4096).decode("utf-8", "replace")
            return HttpResponse(status=response.status, body=body)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(4096).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - the status is what matters
            body = ""
        return HttpResponse(status=exc.code, body=body)
    except Exception as exc:  # noqa: BLE001 - unreachable is a result, not a crash
        return HttpResponse(status=0, error=f"{type(exc).__name__}: {exc}")
