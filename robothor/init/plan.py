"""The two phases, and the promise that sits between them.

Phase 1 runs every step's ``check()`` and produces a plan. Phase 2 applies the
steps, recording each one in ``init_state.yaml`` the moment it finishes so a
crash resumes at the next one rather than at the first.

The promise: if any REQUIRED check fails, phase 2 does not start. Not "stops
early" -- does not start. ``genus init --yes`` on a box with no database has
to leave that box exactly as it found it, because the alternative is a
half-configured instance whose owner.yaml names an operator no account exists
for, and the operator's only clue is an exit code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from robothor.init.steps import StepError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from robothor.init.context import InitContext
    from robothor.init.steps import Step

__all__ = ["InitPlan", "InitResult", "PlanEntry", "StepOutcome", "run_plan"]


@dataclass
class PlanEntry:
    """One row of the plan phase 1 prints.

    ``status`` is the check's ``action`` (``create``/``exists``/``skip``) when
    it passed, ``blocked`` when a required check failed, and ``warn`` when an
    optional one did. ``warn`` is deliberately not fatal: a Telegram token
    that does not work should not stop an install that never needed one.
    """

    id: str
    title: str
    required: bool
    status: str
    detail: str = ""
    fix_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "required": self.required,
            "status": self.status,
            "detail": self.detail,
            "fix_hint": self.fix_hint,
        }


@dataclass
class StepOutcome:
    """What phase 2 did with one step.

    ``applied`` it ran, ``skipped`` it did not need to, ``planned`` this was a
    dry run, ``failed`` it raised.
    """

    id: str
    status: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "status": self.status, "detail": self.detail}


@dataclass
class InitResult:
    """Everything one ``genus init`` produced, in one object.

    This is the ``--json`` document. Its four keys are the contract the
    install gate and the browser wizard read, so they are fixed.

    ``extra`` is how a substrate adds what only it knows -- compose adds
    ``"compose": {"files": [...], "images": {...}, "ready": {...}}`` -- without
    every other substrate growing an empty key for it. It is merged at the top
    level and may never shadow the four reserved names.
    """

    plan: list[PlanEntry] = field(default_factory=list)
    steps: list[StepOutcome] = field(default_factory=list)
    first_run_url: str = ""
    exit_code: int = 0
    blocked: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    #: Keys a substrate may not take over, because consumers read them.
    RESERVED = ("plan", "steps", "first_run_url", "exit_code")

    def as_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "plan": [row.as_dict() for row in self.plan],
            "steps": [row.as_dict() for row in self.steps],
            "first_run_url": self.first_run_url,
            "exit_code": self.exit_code,
        }
        for key, value in self.extra.items():
            if key in self.RESERVED:
                raise ValueError(f"a substrate may not report under the reserved key {key!r}")
            document[key] = value
        return document


@dataclass
class InitPlan:
    """The chosen substrate's ordered steps, plus what phase 1 learned."""

    substrate_name: str
    steps: list[Step]
    entries: list[PlanEntry] = field(default_factory=list)

    def check_all(self, ctx: InitContext) -> list[PlanEntry]:
        """Run every ``check()``. Writes nothing, by construction and by rule.

        A check that raises is treated as a failed check rather than allowed
        to escape: the wizard's whole job is to run on a broken box, and a
        probe that throws on a missing binary must produce a plan row naming
        it, not a traceback.
        """
        entries: list[PlanEntry] = []
        for step in self.steps:
            try:
                result = step.check(ctx)
            except Exception as exc:  # noqa: BLE001 - a broken check is a finding
                entries.append(
                    PlanEntry(
                        id=step.id,
                        title=step.title,
                        required=step.required,
                        status="blocked" if step.required else "warn",
                        detail=f"check raised {type(exc).__name__}: {exc}",
                    )
                )
                continue
            if result.ok:
                status = result.action
            else:
                status = "blocked" if step.required else "warn"
            entries.append(
                PlanEntry(
                    id=step.id,
                    title=step.title,
                    required=step.required,
                    status=status,
                    detail=result.detail,
                    fix_hint=result.fix_hint if not result.ok else "",
                )
            )
        self.entries = entries
        return entries


def run_plan(
    ctx: InitContext,
    plan: InitPlan,
    *,
    on_plan: Callable[[list[PlanEntry]], None] | None = None,
    on_step: Callable[[StepOutcome], None] | None = None,
) -> InitResult:
    """Both phases. Returns the whole run; raises nothing a caller must catch.

    ``on_plan`` is called once between the phases and ``on_step`` after each
    step, so the CLI can print the plan before phase 2 starts and report
    progress as it goes. Rendering afterwards from the returned result would
    leave an operator watching a silent terminal through a migration.
    """
    entries = plan.check_all(ctx)
    if on_plan is not None:
        on_plan(entries)
    blocked = [row.id for row in entries if row.status == "blocked"]
    if blocked:
        return InitResult(plan=entries, steps=[], exit_code=1, blocked=blocked)

    by_id = {row.id: row for row in entries}
    outcomes: list[StepOutcome] = []

    def record(outcome: StepOutcome) -> StepOutcome:
        outcomes.append(outcome)
        if on_step is not None:
            on_step(outcome)
        return outcome

    # The id of the first step that failed, or None. A failure stops the run,
    # but NOT the steps marked ``run_on_failure`` -- the first-run link is the
    # one thing an operator needs most on the run that went wrong, because by
    # then the instance is installed and the URL is the only way into it.
    failed_at: str | None = None

    for step in plan.steps:
        entry = by_id[step.id]
        if failed_at is not None and not getattr(step, "run_on_failure", False):
            record(StepOutcome(step.id, "not-run", f"not run: {failed_at} failed"))
            continue
        if step.resumable and ctx.state.get(step.id) == "completed":
            record(StepOutcome(step.id, "skipped", "already completed"))
            continue
        if entry.status in ("skip", "warn"):
            record(StepOutcome(step.id, "skipped", entry.detail))
            continue
        if ctx.dry_run:
            # Not "applied and wrote nothing" -- apply() is never entered. A
            # step that reached a helper resolving its own paths would already
            # have escaped the workspace before any guard could see it.
            record(StepOutcome(step.id, "planned", entry.detail))
            continue
        try:
            step.apply(ctx)
        except StepError as exc:
            record(StepOutcome(step.id, "failed", str(exc)))
            failed_at = failed_at or step.id
            continue
        except Exception as exc:  # noqa: BLE001 - report it, do not traceback
            record(StepOutcome(step.id, "failed", f"{type(exc).__name__}: {exc}"))
            failed_at = failed_at or step.id
            continue
        record(StepOutcome(step.id, "applied", ctx.details.get(step.id, entry.detail)))
        if _completed(step, ctx):
            ctx.mark_completed(step.id)

    return InitResult(
        plan=entries,
        steps=outcomes,
        first_run_url=ctx.first_run_url,
        exit_code=1 if failed_at else 0,
        # Whatever the substrate's own steps recorded -- copied, not aliased, so
        # a later mutation of the context cannot rewrite a finished run.
        extra=dict(ctx.report),
    )


def _completed(step: Step, ctx: InitContext) -> bool:
    """Whether a successful step may be recorded in ``init_state.yaml``.

    A ``completed`` line means no later run will try again, so a step that
    succeeded WITHOUT doing the thing a later run must still do says so. The
    provider step does exactly that under ``--offline``: it recorded a choice
    it never probed, and one such install made every subsequent `genus init`
    skip the probe forever.
    """
    decide = getattr(step, "completed", None)
    if decide is None:
        return True
    return bool(decide(ctx))
