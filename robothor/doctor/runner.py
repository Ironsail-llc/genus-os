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


def _resolve(body: Callable[[DoctorContext], Any], ctx: DoctorContext) -> Any:
    """Call a check (or a fixer) to completion, on whatever thread we are on.

    A check may be a coroutine function or a plain one -- a plugin is entitled
    to contribute either. A coroutine gets its OWN event loop here, which is
    the whole point: this function runs on a daemon thread, so the loop serving
    the run is free to hit its timeout while this one blocks.
    """
    outcome = body(ctx)
    if not inspect.isawaitable(outcome):
        return outcome

    async def _await() -> Any:
        return await outcome

    return asyncio.run(_await())


async def _bounded(body: Callable[[DoctorContext], Any], ctx: DoctorContext) -> Any:
    """Run ``body`` under the current budget, on a thread, or raise TimeoutError.

    On a thread because ``asyncio.timeout`` can only cancel at an await, and a
    check that does its work synchronously -- a wedged ``stat()``, a DNS
    lookup, an import -- has no await to be cancelled at. Awaiting it directly
    made both budgets advisory: measured, a 0.2s box took 2.0s and the
    overrunning check reported success. Here the awaiting coroutine is only
    waiting on a future, so the timeout fires on time and the daemon thread is
    abandoned -- the same trade-off ``DoctorContext.run_blocking`` documents,
    and the price of "the run always finishes".
    """
    async with asyncio.timeout(ctx.budget()):
        return await ctx.run_blocking(_resolve, body, ctx)


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

    for check in selected:
        if ctx.expired():
            # NOT a skip. A skip says "this could not be checked and here is
            # why", and reads as benign; a budget that ran out means nobody
            # knows, and "nobody knows" must not present as health on the
            # surface an operator is asking for a verdict from.
            rows.append(_not_run(check, ctx.total_timeout_s))
            continue
        rows.extend(await _run_one(check, ctx))
    return DoctorReport(results=rows)


def _not_run(check: Check, total: float | None) -> CheckResult:
    return CheckResult(
        id=check.id,
        title=check.title,
        category=check.category,
        severity=check.severity,
        status="fail",
        detail=(
            f"not run: the run's total budget of {total:g}s was exhausted — "
            "raise it with --timeout, or narrow the run with --only/--category"
        ),
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
