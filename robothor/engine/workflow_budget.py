"""Workflow budget coherence: no single step may outspend its own workflow.

A workflow carries one wall-clock budget (``timeout_seconds``) and hands it to
every step in turn. An agent step spends that budget on an LLM *chain* — the
primary, its in-place transient retry, then each fallback — and each leg of
that chain has its own per-call allowance (``LLM_REQUEST_TIMEOUT_BATCH`` for
cloud models on a workflow/cron trigger, ``LLM_REQUEST_TIMEOUT_OLLAMA`` for the
local tail). Nothing compared the two numbers.

The 2026-09-13 diagnosis found what that costs: ``email-pipeline`` carried a
900s budget while one of its two steps could legitimately spend
``300 + 300 + 300 + 600`` plus one 300s transient retry = **1,800s** walking
its own four-model chain. The workflow could only ever finish while the primary
answered first try; the day the primary started returning empty completions,
every run died at exactly 900s with ``steps 0/2`` and no step rows at all.

Two halves live here, because they are the same fact read at two times:

* :func:`step_chain_allowance` / :func:`check_step_budgets` — the *static*
  half. The sibling of ``manifest_schema._check_stall_budget_vs_llm_timeout``,
  which validates this identical inversion one layer down (a stall budget
  against the per-call allowance of its own chain). Same ladder: a warning that
  names the step, the computed worst case and the budget; an error only under
  ``strict``.
* :func:`workflow_deadline` / :func:`bound_call_timeout` — the *runtime* half.
  A validator cannot stop a run that is already wedged. The workflow publishes
  its deadline on a ContextVar; the LLM chain walk clamps each per-call timeout
  to what is actually left and refuses to start a further model once the
  workflow's own budget is gone, naming the step and the model in flight
  instead of dying anonymously in the outer ``asyncio.timeout``.

This module is deliberately separate from ``workflow.py``: that module is
already at its size ratchet, and the allowance arithmetic is the kind of thing
that wants its own tests.
"""

from __future__ import annotations

import contextlib
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from robothor.engine.llm_budgets import in_place_retries, model_call_allowance
from robothor.engine.sanitize import sanitize_log as _sanitize

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Iterator, Sequence

    from robothor.engine.models import WorkflowDef

logger = logging.getLogger(__name__)

#: Slice of the workflow budget held back from the last LLM call so the STEP
#: fails first, with a message naming itself and the model in flight, instead
#: of the outer ``asyncio.timeout`` cancelling it anonymously. Five seconds is
#: enough to unwind the call and write the step row; on a 900s budget it is
#: under 0.6% of the run.
DEADLINE_RESERVE_SECONDS = 5.0


class WorkflowDeadlineError(TimeoutError):
    """The workflow's budget ran out inside one of its own steps.

    A ``TimeoutError`` subclass on purpose: every existing handler that treats
    a timeout as a timeout keeps working, and the chain walk's transient-retry
    path already classifies ``TimeoutError``. What this adds is the *identity*
    of what was in flight, which is the thing the 2026-09-13 diagnosis had to
    reconstruct by joining ``agent_runs`` to ``workflow_runs`` on timestamps.
    """

    def __init__(self, workflow_id: str, step_id: str, model: str, remaining: float) -> None:
        self.workflow_id = workflow_id
        self.step_id = step_id
        self.model = model
        self.remaining = remaining
        # ``model`` is the one the walk was REFUSED, not one in flight: by the
        # time this is raised the previous call has returned. Saying "in
        # flight" named a model that had already finished, and on a retry it
        # named the wrong attempt (2026-09-13 review M5).
        #
        # The leading constant is load-bearing, not decoration: the timeout
        # rate and the resume scan both identify this kind of cancellation by
        # that prefix, so the message is a contract rather than prose.
        from robothor.engine.analytics import WORKFLOW_BUDGET_CANCEL_PREFIX

        super().__init__(
            f"{WORKFLOW_BUDGET_CANCEL_PREFIX}: workflow {workflow_id!r} step {step_id!r} — "
            f"{remaining:.1f}s left, not enough to start model {model!r}"
        )


def propagates_to_caller(exc: BaseException) -> bool:
    """Does this cancellation continue past ``runner.execute``?

    Two kinds do, for the same reason: the run row is written first, and then
    something ABOVE the runner has to act on the exception.

    * ``CancelledError`` — an outer deadline is only a deadline if its
      cancellation reaches the ``asyncio.timeout`` that raised it. Absorbed,
      the cap silently does nothing (2026-08-24: a 3600s ceiling fired and the
      sweep ran three more hours).
    * ``WorkflowDeadlineError`` — only the workflow engine knows which STEP
      this was. Absorbed, the step lands `failed` carrying a circuit-breaker
      reason and every identifier this exception exists to carry is lost one
      frame up.

    A run's OWN hard cap is a plain ``TimeoutError`` and still returns a
    finished run: for that one the deadline that fired was this run's to
    enforce, and nobody above needs to hear about it.

    It lives here, beside the exception that gave the rule its second member,
    rather than in ``cancel_outcome`` beside the classification: ``runner``
    already imports ``WorkflowDeadlineError`` from this module, so this adds no
    edge to the import graph.
    """
    import asyncio

    from robothor.engine.runtime.deadlines import RuntimeDeadlineError

    return isinstance(exc, asyncio.CancelledError | WorkflowDeadlineError | RuntimeDeadlineError)


@dataclass(frozen=True)
class WorkflowDeadline:
    """The monotonic instant a workflow run must be finished by."""

    workflow_id: str
    monotonic_deadline: float
    step_id: str = ""

    def remaining(self) -> float:
        """Seconds left before the workflow's own budget is spent."""
        return self.monotonic_deadline - time.monotonic()


_deadline_var: ContextVar[WorkflowDeadline | None] = ContextVar(
    "robothor_workflow_deadline", default=None
)


@contextlib.contextmanager
def workflow_deadline(workflow_id: str, timeout_seconds: float) -> Iterator[WorkflowDeadline]:
    """Publish this run's deadline for the duration of the ``with`` block.

    A ContextVar rather than a threaded parameter: the chain walk that has to
    respect this sits five frames below ``WorkflowEngine.execute`` (engine ->
    runner -> session -> llm_client -> the per-model loop), and every one of
    those frames would otherwise grow a parameter it does not use. Parallel
    branches inherit it for free — ``asyncio.gather`` copies the context into
    each task.
    """
    reserve = min(DEADLINE_RESERVE_SECONDS, max(0.0, timeout_seconds * 0.1))
    deadline = WorkflowDeadline(
        workflow_id=workflow_id,
        monotonic_deadline=time.monotonic() + max(0.0, timeout_seconds - reserve),
    )
    token = _deadline_var.set(deadline)
    try:
        yield deadline
    finally:
        _deadline_var.reset(token)


@contextlib.contextmanager
def step_scope(step_id: str) -> Iterator[None]:
    """Name the step that is spending the budget, for error messages."""
    current = _deadline_var.get()
    if current is None:
        yield
        return
    token = _deadline_var.set(
        WorkflowDeadline(
            workflow_id=current.workflow_id,
            monotonic_deadline=current.monotonic_deadline,
            step_id=step_id,
        )
    )
    try:
        yield
    finally:
        _deadline_var.reset(token)


def current_deadline() -> WorkflowDeadline | None:
    """The deadline in force, or ``None`` outside a workflow step."""
    return _deadline_var.get()


def bound_call_timeout(per_call_timeout: float, model: str) -> float:
    """Clamp one LLM call's allowance to what the workflow has left.

    Returns ``per_call_timeout`` unchanged outside a workflow — agent runs,
    chat, heartbeats — so this is inert everywhere but the path it was written
    for. Inside a workflow it does two things:

    * refuses to START a model the workflow cannot afford to finish, which is
      what stops the chain walk burning a further 300s (or 600s on the local
      tail) of a budget that is already gone; and
    * hands back the remaining seconds when they are smaller than the model's
      own allowance, so the call is cancelled by the *step*, with a name, a
      few seconds before the workflow's outer timeout fires anonymously.
    """
    deadline = _deadline_var.get()
    if deadline is None:
        return per_call_timeout
    remaining = deadline.remaining()
    if remaining <= 0:
        # Say so on the same channel the chain walk uses for every other
        # refusal. Without this the models the deadline prevents leave no trace
        # at all — the walk raises instead of skipping, so "why did the chain
        # stop at model 2 of 4" had no answer in the log.
        logger.info(
            "skipping %s — the workflow budget for step %s is spent",
            _sanitize(model),
            _sanitize(deadline.step_id or "unknown"),
        )
        raise WorkflowDeadlineError(
            deadline.workflow_id, deadline.step_id or "unknown", model, remaining
        )
    return min(per_call_timeout, remaining)


# ── Static validation ──────────────────────────────────────────────────


@dataclass(frozen=True)
class BudgetIssue:
    """One workflow step whose worst case does not fit its workflow budget."""

    workflow_id: str
    step_id: str
    agent_id: str
    allowance_seconds: int
    budget_seconds: int
    severity: str  # "warning" | "error"

    @property
    def message(self) -> str:
        return (
            f"step '{self.step_id}' (agent '{self.agent_id}') can spend up to "
            f"{self.allowance_seconds}s walking its model chain, which exceeds the "
            f"workflow's own {self.budget_seconds}s budget — a single slow model "
            f"call consumes the whole workflow, and the run can only finish while "
            f"the primary answers first try"
        )


def step_chain_allowance(chain: Sequence[str]) -> int:
    """Worst-case wall-clock for ONE agent step's LLM chain, in seconds.

    The chain is walked once — every model gets its own per-call allowance —
    plus the in-place retries the walk grants the primary, which is the leg
    that actually gets retried in practice: on 2026-09-13 the primary returned
    empty completions three times inside one run before the chain advanced.

    Deliberately the FLOOR of the worst case for the models AFTER the primary,
    not the ceiling: the true ceiling multiplies each of them by its own retry
    count too, and a check calibrated there would flag budgets that are merely
    tight rather than incoherent. A warning from this number means the budget
    is definitely too small, which is the only kind of warning anyone acts on.
    The primary is billed exactly, because under-counting there is how a
    local-primary agent slips through entirely.

    For the chain that produced the incident —
    ``deepseek-v4.1-flash, mimo-v2.5, deepseek-v4-flash, ollama_chat/qwen3.8:27b``
    — this is ``300 + 300 + 300 + 600 + 300 = 1800`` against a 900s budget.

    Every number comes from ``llm_budgets``, the same leaf ``llm_client``
    spends them out of, so the prediction and the runtime cannot drift.
    """
    models = [m for m in chain if m]
    if not models:
        return 0
    total = sum(model_call_allowance(m) for m in models)
    return total + in_place_retries(models[0]) * model_call_allowance(models[0])


@dataclass(frozen=True)
class BudgetReport:
    """What the budget check managed to look at, and what it found.

    The counts exist because the first version of this check was inert on the
    platform's own repo and looked exactly like a clean one: every agent
    manifest is gitignored, so every chain resolved to ``[]``,
    ``step_chain_allowance([])`` returned 0, 0 is never greater than a budget,
    and the loader printed nothing at all. Three layers of silence stacked —
    an empty chain, a swallowed exception in the walk, and a swallowed
    exception at the call site. A check that cannot say "I checked 2 of 6" can
    never be told apart from one that had nothing to complain about.
    """

    workflow_id: str
    agent_steps: int
    checked: int
    unresolved: tuple[str, ...]
    issues: tuple[BudgetIssue, ...]

    @property
    def summary(self) -> str:
        text = f"checked {self.checked} of {self.agent_steps} agent step(s)"
        if self.unresolved:
            text += (
                f"; {len(self.unresolved)} chain(s) unresolved "
                f"({', '.join(self.unresolved)}) — NOT checked"
            )
        return text


def check_step_budgets(
    wf: WorkflowDef,
    resolve_chain: Callable[[str], Sequence[str]],
    *,
    strict: bool = False,
) -> list[BudgetIssue]:
    """Flag every agent step whose chain can outspend the whole workflow.

    ``resolve_chain`` maps an ``agent_id`` to that agent's full model chain
    (primary first, fallbacks after, last-resort model included — i.e. exactly
    what ``config.load_agent_config`` produces). It is injected rather than
    imported so this arithmetic stays testable without a manifest directory.

    Warn-only by default, matching ``_check_stall_budget_vs_llm_timeout``: the
    workflow still loads, because refusing to load it would take a pipeline
    offline over a budget that is merely optimistic. ``strict=True`` promotes
    the same finding to an error for the validation surfaces that want one.

    Thin wrapper over :func:`report_step_budgets`, which is what callers that
    need to know how much was actually checked should use.
    """
    return list(report_step_budgets(wf, resolve_chain, strict=strict).issues)


def report_step_budgets(
    wf: WorkflowDef,
    resolve_chain: Callable[[str], Sequence[str]],
    *,
    strict: bool = False,
) -> BudgetReport:
    """As :func:`check_step_budgets`, but also says what it could not check.

    An agent step whose chain will not resolve — a retired manifest, a renamed
    agent, or simply a platform checkout where every manifest is gitignored —
    is counted as UNRESOLVED rather than quietly treated as a zero-length chain
    that trivially fits any budget.
    """
    from robothor.engine.models import WorkflowStepType

    budget = int(wf.timeout_seconds or 0)
    issues: list[BudgetIssue] = []
    unresolved: list[str] = []
    agent_steps = 0
    checked = 0
    severity = "error" if strict else "warning"

    def _visit(steps: Sequence[object]) -> None:
        nonlocal agent_steps, checked
        for step in steps:
            step_type = getattr(step, "type", None)
            agent_id = getattr(step, "agent_id", "")
            nested = getattr(step, "parallel_steps", None)
            if nested:
                _visit(nested)
            if step_type != WorkflowStepType.AGENT or not agent_id:
                continue
            agent_steps += 1
            step_id = getattr(step, "id", "") or agent_id
            try:
                chain = [m for m in (resolve_chain(agent_id) or []) if m]
            except Exception as e:
                logger.debug("Chain lookup failed for %s: %s", _sanitize(agent_id), e)
                chain = []
            allowance = step_chain_allowance(chain) if chain else 0
            if allowance <= 0:
                # A step is CHECKED when a number was computed for it, not when
                # it was merely visited. An empty chain, or a chain of models
                # with no allowance between them — either way nothing was
                # scored, and counting it would restate the inert state these
                # counts exist to expose, one layer further in.
                unresolved.append(step_id)
                continue
            checked += 1
            if budget > 0 and allowance > budget:
                issues.append(
                    BudgetIssue(
                        workflow_id=wf.id,
                        step_id=getattr(step, "id", ""),
                        agent_id=agent_id,
                        allowance_seconds=allowance,
                        budget_seconds=budget,
                        severity=severity,
                    )
                )

    _visit(wf.steps)
    return BudgetReport(
        workflow_id=wf.id,
        agent_steps=agent_steps,
        checked=checked,
        unresolved=tuple(unresolved),
        issues=tuple(issues),
    )
