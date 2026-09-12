"""Run the checks, time-box every one of them, and return one verdict.

The runner is the only place in the doctor that knows about failure modes it
did not cause. Its contract, in order of how expensive getting it wrong is:

1. **The run always finishes.** A check that hangs is reported as a failure
   after ``timeout_s`` and the others still run. The doctor is used when things
   are already broken; "it hung" is the one outcome that leaves the operator
   with nothing.
2. **No traceback escapes.** A check that raises becomes a failed result naming
   the exception type. A doctor that crashes on a broken box is a second
   incident.
3. **Exit codes mean one thing each.** 0 no required failure, 1 a required
   check failed, 2 the doctor itself could not run -- an unknown ``--only`` id,
   a registry that would not import. The third is separate from the second on
   purpose: "nothing is wrong" and "nothing was checked" must never share a
   code, which is how a typo in a CI gate turns into a green build.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any

from robothor.doctor.context import DoctorContext
from robothor.doctor.model import Result

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from robothor.doctor.model import Check, Severity, Status

logger = logging.getLogger(__name__)

__all__ = [
    "CheckResult",
    "CheckSelectionError",
    "DoctorReport",
    "run",
    "run_sync",
    "select",
]


#: How many wedged workers one run may orphan before it stops starting checks.
#: One: enough that a single hung dependency does not cost the rest of the
#: report, few enough that the cost is a constant rather than one thread, one
#: event loop and three descriptors per check.
_MAX_REPLACEMENTS = 1

#: How long :meth:`_Worker.close` waits for a healthy worker to stop. Short --
#: it only has to process one ``loop.stop`` callback; anything longer is a
#: worker that is wedged, and the pool has already decided what to do about
#: those.
_CLOSE_JOIN_SECONDS = 2.0


class CheckSelectionError(RuntimeError):
    """``--only`` or ``--category`` named nothing. Exit 2, never exit 0."""


@dataclass(frozen=True)
class CheckResult:
    """One row of the report. This is also the JSON the bridge serves."""

    id: str
    title: str
    category: str
    severity: Severity
    status: Status
    detail: str
    fixable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "severity": self.severity,
            "status": self.status,
            "detail": self.detail,
            "fixable": self.fixable,
        }


@dataclass(frozen=True)
class DoctorReport:
    """Everything one run produced.

    ``errored`` is the exit-2 state: the checks did not run, so the results
    list is empty and ``error_detail`` says why. It is kept separate from a
    failed check because they call for different actions -- fix the instance,
    versus fix the command.
    """

    results: list[CheckResult] = field(default_factory=list)
    errored: bool = False
    error_detail: str = ""

    @property
    def summary(self) -> dict[str, int]:
        return {
            "required_failed": self._failed("required"),
            "recommended_failed": self._failed("recommended"),
            "passed": sum(1 for row in self.results if row.status == "pass"),
            "skipped": sum(1 for row in self.results if row.status == "skip"),
        }

    def _failed(self, severity: str) -> int:
        return sum(1 for row in self.results if row.status == "fail" and row.severity == severity)

    @property
    def status(self) -> str:
        """``ok`` or ``degraded`` -- the same two words, in the same key, as
        :func:`robothor.health_contract.readiness_response`, so the Helm Health
        view can render a readiness payload and a doctor payload with one
        component.

        An ``info`` failure does NOT degrade the instance. This word is what a
        dashboard banner renders, and severity is the whole vocabulary the
        report has for "how much does this matter": a Slack token that is not
        shaped like one, on an instance that does not use Slack, would
        otherwise paint the appliance red while the CLI exits 0 -- two surfaces
        disagreeing about the same run, which is how an operator learns to
        distrust both.
        """
        if self.errored:
            return "degraded"
        summary = self.summary
        degraded = summary["required_failed"] or summary["recommended_failed"]
        return "degraded" if degraded else "ok"

    @property
    def exit_code(self) -> int:
        if self.errored:
            return 2
        return 1 if self.summary["required_failed"] else 0

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "summary": self.summary,
            "checks": [row.as_dict() for row in self.results],
        }
        if self.errored:
            payload["error"] = self.error_detail
        return payload


def select(
    checks: Sequence[Check], *, only: str | None = None, category: str | None = None
) -> list[Check]:
    """Narrow the check list, or say plainly that the filter matched nothing.

    An empty selection is an ERROR rather than an empty pass. ``genus doctor
    --only db.migration`` (singular, a real typo) would otherwise print
    "0 failed" and exit 0, and a CI gate built on it would be permanently
    green.
    """
    selected = list(checks)
    if only is not None:
        selected = [check for check in selected if check.id == only]
        if not selected:
            raise CheckSelectionError(
                f"no check with id {only!r}; run 'genus doctor --json' to list them"
            )
    if category is not None:
        selected = [check for check in selected if check.category == category]
        if not selected:
            raise CheckSelectionError(
                f"no checks in category {category!r}; the categories are "
                + ", ".join(sorted({check.category for check in checks}))
            )
    return selected


class _Worker:
    """One daemon thread with one private event loop, shared by a whole run.

    Why a private loop at all: ``asyncio.timeout`` can only cancel at an await,
    so a check that does its work synchronously -- a wedged ``stat()``, a DNS
    lookup, an import -- has no await to be cancelled at, and awaiting it on the
    run's own loop made both budgets advisory (measured: a 0.2s box took 2.0s
    and the overrunning check reported success). Running the body on a different
    loop leaves the timing loop free to fire.

    Why ONE per run rather than one per check: a thread stuck in a blocking call
    cannot be killed, a daemon thread is never joined, and a loop whose
    ``asyncio.run`` was cancelled is never closed -- so the per-check version
    leaked a thread, a loop and three descriptors for every abandoned check,
    with nothing ever reclaimed. A CLI process exits and nobody notices. The
    bridge does not, and the run that abandons checks is precisely the run taken
    during an incident. Paying per run makes a healthy run cost nothing lasting
    and a pathological one cost a constant.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._serve, name="genus-doctor-worker", daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def owns_current_loop(self) -> bool:
        """Is the caller already running on this worker's loop?"""
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    async def call(self, body: Callable[[DoctorContext], Any], ctx: DoctorContext) -> Any:
        """Run ``body`` on this worker and await its result from the caller's loop.

        The body may be a coroutine function or a plain one -- a plugin is
        entitled to contribute either. A coroutine is driven as a task on the
        worker's loop; a plain callable is simply called, blocking the worker
        and nothing else.
        """
        caller = asyncio.get_running_loop()
        future: asyncio.Future[Any] = caller.create_future()

        def _deliver(setter: Callable[[Any], None], value: Any) -> None:
            if not future.done():
                setter(value)

        def _finish(task: asyncio.Task[Any]) -> None:
            if task.cancelled():  # pragma: no cover - only on loop teardown
                return
            error = task.exception()
            if error is not None:
                caller.call_soon_threadsafe(_deliver, future.set_exception, error)
            else:
                caller.call_soon_threadsafe(_deliver, future.set_result, task.result())

        def _start() -> None:
            try:
                outcome = body(ctx)
            except BaseException as exc:  # noqa: BLE001 - relayed to the awaiter
                caller.call_soon_threadsafe(_deliver, future.set_exception, exc)
                return
            if not inspect.isawaitable(outcome):
                caller.call_soon_threadsafe(_deliver, future.set_result, outcome)
                return
            self.loop.create_task(_drive(outcome)).add_done_callback(_finish)

        self.loop.call_soon_threadsafe(_start)
        return await future

    def close(self) -> None:
        """Stop the loop and reclaim the thread. Best effort: a worker wedged in
        a blocking call will not process the stop, which is why an abandoned one
        is dropped by the pool rather than closed here."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=_CLOSE_JOIN_SECONDS)
        if not self.thread.is_alive():
            self.loop.close()


async def _drive(awaitable: Any) -> Any:
    return await awaitable


class _WorkerPool:
    """The run's worker, and the rule for replacing one that had to be abandoned.

    A worker whose check was abandoned is unusable -- the blocking call still
    owns it -- and unkillable, so continuing means orphaning it. The pool allows
    exactly ``_MAX_REPLACEMENTS`` of those and then stops: the remaining checks
    are reported as not run rather than each buying another orphan. One
    replacement is what keeps the common incident useful (one wedged dependency,
    every other check still answered) while making the cost a constant instead
    of one orphan per check.
    """

    def __init__(self, ctx: DoctorContext) -> None:
        self._ctx = ctx
        self._worker: _Worker | None = None
        self._replacements = 0
        self._exhausted = False

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    def worker(self) -> _Worker:
        if self._worker is None:
            self._worker = _Worker()
            self._ctx._worker = self._worker
        return self._worker

    def abandon(self) -> None:
        """The current worker is wedged. Drop it, and decide whether to continue."""
        logger.warning(
            "doctor: abandoning a wedged worker thread (replacement %d of %d)",
            self._replacements + 1,
            _MAX_REPLACEMENTS + 1,
        )
        self._worker = None
        self._ctx._worker = None
        if self._replacements >= _MAX_REPLACEMENTS:
            self._exhausted = True
        else:
            self._replacements += 1

    def close(self) -> None:
        if self._worker is not None:
            self._worker.close()
            self._worker = None
        self._ctx._worker = None


async def _bounded(body: Callable[[DoctorContext], Any], ctx: DoctorContext) -> Any:
    """Run ``body`` on the run's worker under the current budget, or raise
    TimeoutError -- and mark the worker abandoned when that happens, because it
    is still holding whatever refused to return."""
    pool: _WorkerPool = ctx._pool
    worker = pool.worker()
    try:
        async with asyncio.timeout(ctx.budget()):
            return await worker.call(body, ctx)
    except TimeoutError:
        pool.abandon()
        raise


async def _call(check: Check, ctx: DoctorContext) -> list[Result]:
    """Run one check inside its time box, never raising."""
    allowed = ctx.budget()
    try:
        answer = await _bounded(check.run, ctx)
    except TimeoutError:
        logger.warning("doctor: check %s timed out after %ss", check.id, allowed)
        return [Result(status="fail", detail=f"timed out after {allowed:g}s")]
    except Exception as exc:  # noqa: BLE001 - a check's dependency failing is normal here
        logger.warning("doctor: check %s raised %s", check.id, type(exc).__name__)
        return [Result(status="fail", detail=f"{type(exc).__name__}: {exc}")]
    if isinstance(answer, Result):
        return [answer]
    rows = list(answer)
    return rows or [Result(status="pass", detail="nothing to report")]


async def _repair(check: Check, ctx: DoctorContext, result: Result) -> Result:
    """Run a check's fix and then ASK THE CHECK AGAIN.

    The re-run is the point. A fix that reports success while the condition
    persists is this project's most expensive recurring defect -- a control
    that logs its own success is not evidence -- so the reported status is
    always the one a fresh run of the check produced, never the fixer's word.
    """
    if check.fix is None:
        return result
    if ctx.dry_run:
        return Result(
            status=result.status,
            detail=f"{result.detail} (--fix would repair this; --dry-run, so nothing was done)",
            fixable=True,
            sub_id=result.sub_id,
        )
    allowed = ctx.budget()
    try:
        outcome = await _bounded(check.fix, ctx)
    except TimeoutError:
        return Result(
            status="fail",
            detail=f"{result.detail}; the repair timed out after {allowed:g}s",
            fixable=True,
            sub_id=result.sub_id,
        )
    except Exception as exc:  # noqa: BLE001 - a refused repair is a result
        return Result(
            status="fail",
            detail=f"{result.detail}; the repair failed: {type(exc).__name__}: {exc}",
            fixable=True,
            sub_id=result.sub_id,
        )

    rechecked = (await _call(check, ctx))[0]
    joined = (
        f"{outcome.detail}; rechecked: {rechecked.detail}" if outcome.detail else rechecked.detail
    )
    return Result(
        status=rechecked.status,
        detail=joined,
        fixable=rechecked.fixable,
        sub_id=rechecked.sub_id,
    )


async def _run_one(check: Check, ctx: DoctorContext) -> list[CheckResult]:
    results = await _call(check, ctx)
    if ctx.fix:
        results = [
            await _repair(check, ctx, item) if item.status == "fail" and item.fixable else item
            for item in results
        ]
    return [
        CheckResult(
            id=f"{check.id}:{item.sub_id}" if item.sub_id else check.id,
            title=check.title,
            category=check.category,
            severity=check.severity,
            status=item.status,
            detail=item.detail,
            fixable=item.fixable,
        )
        for item in results
    ]


async def run(
    ctx: DoctorContext | None = None,
    *,
    checks: Sequence[Check] | None = None,
    checks_factory: Callable[[], Sequence[Check]] | None = None,
    only: str | None = None,
    category: str | None = None,
) -> DoctorReport:
    """Run every selected check and return the report.

    Checks run SEQUENTIALLY. They compete for one database pool, one Ollama
    server and one LLM budget, and a doctor that opened ten connections to a
    PostgreSQL already refusing them would be adding to the incident it was
    called to explain.

    Two budgets, because one is not enough. The per-check box bounds any single
    hanging dependency; without a TOTAL box the worst case is still
    ``len(checks) * timeout_s`` -- over two minutes on the current check set,
    which is tolerable for an operator watching a terminal and not tolerable
    for an HTTP handler holding a worker thread that other routes need.
    ``ctx.total_timeout_s`` sets that second box: once it is spent the
    remaining checks are reported as not run, and the check straddling the
    deadline gets only what is left rather than a fresh ``timeout_s``. Both
    boxes are HARD -- every check body runs on a daemon thread, so a coroutine
    that does its work synchronously cannot outrun them (see :func:`_bounded`),
    and a repair and its re-check narrow against the same deadline rather than
    each taking a fresh budget.
    """
    ctx = ctx or DoctorContext()
    try:
        if checks is None:
            factory = checks_factory or _default_checks
            checks = factory()
        selected = select(checks, only=only, category=category)
    except Exception as exc:  # noqa: BLE001 - exit 2 is exactly this case
        logger.warning("doctor: could not assemble the check list: %s", exc)
        return DoctorReport(errored=True, error_detail=f"{type(exc).__name__}: {exc}")

    rows: list[CheckResult] = []
    if ctx.total_timeout_s is not None:
        ctx._deadline = monotonic() + ctx.total_timeout_s
    ctx._pool = pool = _WorkerPool(ctx)

    try:
        for check in selected:
            # Neither of these is a skip. A skip says "this could not be checked
            # and here is why", and reads as benign; a budget that ran out or a
            # worker that had to be abandoned means nobody knows, and
            # "nobody knows" must not present as health on the surface an
            # operator is asking for a verdict from.
            if ctx.expired():
                rows.append(_not_run(check, _BUDGET_EXHAUSTED.format(total=ctx.total_timeout_s)))
                continue
            if pool.exhausted:
                rows.append(_not_run(check, _WORKERS_EXHAUSTED))
                continue
            rows.extend(await _run_one(check, ctx))
    finally:
        pool.close()
        ctx._pool = None
    return DoctorReport(results=rows)


#: Why a check was not run. Both are failures, never skips.
_BUDGET_EXHAUSTED = (
    "not run: the run's total budget of {total:g}s was exhausted — raise it with "
    "--timeout, or narrow the run with --only/--category"
)
_WORKERS_EXHAUSTED = (
    "not run: an earlier check wedged this run's worker and its replacement, so no "
    "further check could be started — the timed-out lines above name the dependency"
)


def _not_run(check: Check, detail: str) -> CheckResult:
    return CheckResult(
        id=check.id,
        title=check.title,
        category=check.category,
        severity=check.severity,
        status="fail",
        detail=detail,
        fixable=False,
    )


def _default_checks() -> Sequence[Check]:
    from robothor.doctor.registry import all_checks

    return all_checks()


def run_sync(
    ctx: DoctorContext | None = None,
    *,
    checks: Sequence[Check] | None = None,
    checks_factory: Callable[[], Sequence[Check]] | None = None,
    only: str | None = None,
    category: str | None = None,
) -> DoctorReport:
    """Blocking entry point, for the CLI and for the bridge's worker thread.

    ``asyncio.run`` rather than a shared loop: the bridge calls this from
    ``asyncio.to_thread``, where there is no running loop, and the CLI has none
    at all.
    """
    return asyncio.run(
        run(ctx, checks=checks, checks_factory=checks_factory, only=only, category=category)
    )
