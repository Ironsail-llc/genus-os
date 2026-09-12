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
    """

    plan: list[PlanEntry] = field(default_factory=list)
    steps: list[StepOutcome] = field(default_factory=list)
    first_run_url: str = ""
    exit_code: int = 0
    blocked: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": [row.as_dict() for row in self.plan],
            "steps": [row.as_dict() for row in self.steps],
            "first_run_url": self.first_run_url,
            "exit_code": self.exit_code,
        }


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


def run_plan(ctx: InitContext, plan: InitPlan) -> InitResult:
    """Both phases. Returns the whole run; raises nothing a caller must catch."""
    entries = plan.check_all(ctx)
    blocked = [row.id for row in entries if row.status == "blocked"]
    if blocked:
        return InitResult(plan=entries, steps=[], exit_code=1, blocked=blocked)

    by_id = {row.id: row for row in entries}
    outcomes: list[StepOutcome] = []
    for step in plan.steps:
        entry = by_id[step.id]
        if step.resumable and ctx.state.get(step.id) == "completed":
            outcomes.append(StepOutcome(step.id, "skipped", "already completed"))
            continue
        if entry.status in ("skip", "warn"):
            outcomes.append(StepOutcome(step.id, "skipped", entry.detail))
            continue
        if ctx.dry_run:
            outcomes.append(StepOutcome(step.id, "planned", entry.detail))
            continue
        try:
            step.apply(ctx)
        except StepError as exc:
            outcomes.append(StepOutcome(step.id, "failed", str(exc)))
            return InitResult(plan=entries, steps=outcomes, exit_code=1)
        except Exception as exc:  # noqa: BLE001 - report it, do not traceback
            outcomes.append(StepOutcome(step.id, "failed", f"{type(exc).__name__}: {exc}"))
            return InitResult(plan=entries, steps=outcomes, exit_code=1)
        outcomes.append(StepOutcome(step.id, "applied", ctx.details.get(step.id, entry.detail)))
        ctx.mark_completed(step.id)

    return InitResult(
        plan=entries,
        steps=outcomes,
        first_run_url=ctx.first_run_url,
        exit_code=0,
    )
