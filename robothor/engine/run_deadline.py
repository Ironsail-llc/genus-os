"""The budget ends the run, with the best artefact the run has.

``run_pacing`` tells an agent how much time is left. Nothing acted on the
answer. This module is what acts: one resolver for the run's wall-clock
budget, a WRAP-UP rung where only writing and checking are still allowed, and
a hard stop that ends the run itself rather than waiting to be destroyed.

The measurement (2026-09-17 Code Intelligence sweep, ``connect_the_dots_hard``):

* the task budget was **1200s** and the harness backstop **1500s**;
* the engine's own ceiling — the manifest number scaled by the model's tempo —
  resolved to **1600**, so every clock the engine owned was aiming past the
  point at which the container would be destroyed;
* ``wd.log`` shows ``wait=llm_inflight`` at every tick from ``elapsed=1200``
  through ``elapsed=1500``;
* ``agent.log`` holds one line, ``HARNESS KILL after 1500s``, and the grader a
  ``FileNotFoundError`` for a transcript that was never written.

Score 0.0, for a task the same engine scored 0.545 on the day before. Four
more runs died the same way in the same sweep (``sam3_debug`` 273 LLM calls /
1191s, ``connect_the_dots_medium`` 143 / 1189s, ``link_a_pix`` 169 / 1149s).

Three separate defects are in that, and this module is each of their homes:

1. **An imposed budget was scaled.** Tempo scaling (``watchdog_budgets``)
   exists so a fleet that has fallen back to a local tier is not held to cloud
   timings. That is right for a number an operator wrote in a manifest and
   wrong for a number somebody else is counting down: whoever imposed the
   budget is going to act on it, whatever our model's latency is.
2. **Nothing reserved time to write.** The pacing notes at 50/80/95 % were all
   issued and all ignored; a note is advice, and an agent mid-investigation
   takes advice about as well as a person does.
3. **The run was still CALLING at the deadline.** A model call in flight when
   an outer killer arrives loses the whole run — not just the call.

Everything here is gated on the step-efficiency ladder, for the reason the
rest of that cluster is: ``off`` must be the engine that shipped, or a sweep
comparing ``off`` with ``enforce`` is measuring the weather.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Bound here rather than reached for through the module every call, so that
#: disabling the OUTER ``asyncio.timeout`` — which the loop's own tests do, to
#: prove the loop bounds itself with every other layer dead — does not silently
#: disable this one along with it. Two different ceilings should be two
#: different things to switch off.
_timeout = asyncio.timeout

#: Where the run stops exploring and starts saving. 90 % of a 1200s budget is
#: 120 seconds, which is four unhurried ``write_file`` turns at the p50
#: seconds-per-iteration of the runs this exists for. Lower and the run gives
#: up work it could have finished; higher and the reserve is too thin to write
#: a file the graders can read.
DEFAULT_WRAPUP_FRACTION = 0.90

#: A fraction below this would put a run into wrap-up before it had a draft to
#: wrap up. Clamped rather than obeyed: a typo in a config file must not make
#: every run on the box answer from its first turn.
MIN_WRAPUP_FRACTION = 0.5

#: How long a model call already in flight at the deadline may still take. It
#: is a grace, not an extension: the call is cancelled at the end of it, and
#: the run ends with whatever is on disk. Sized to let a call that is already
#: streaming its last tokens land, and to stay well inside the margin an outer
#: killer leaves (the bench harness leaves 300s).
DEFAULT_GRACE_SECONDS = 20

Phase = Literal["normal", "wrapup", "expired"]


class RunBudgetError(TimeoutError):
    """THIS run's budget ended the model call that was in flight.

    Its own type, and the loop catches only this, because ``TimeoutError`` is
    not a sentence with one meaning here: ``WorkflowDeadlineError`` is a
    ``TimeoutError`` subclass that must escape ``execute()`` so the workflow
    engine can say which step it was, and a bare ``except TimeoutError``
    around the model call swallowed it — turning a workflow's cancellation
    into this run's budget stop, silently, with the row recorded against the
    wrong cause.
    """


@contextlib.asynccontextmanager
async def _bounded_call(seconds: float) -> AsyncIterator[None]:
    """Bound one await, and say whose deadline ended it if one did.

    ``asyncio.Timeout.expired()`` is the only thing that distinguishes "my
    ceiling fired" from "something inside raised a TimeoutError of its own and
    is passing through".
    """
    handle = _timeout(seconds)
    try:
        async with handle:
            yield
    except TimeoutError as exc:
        if handle.expired():
            raise RunBudgetError(f"model call exceeded the run's remaining {seconds:.0f}s") from exc
        raise


#: What a wrapping-up run may still call: writing the artefact, and reading it
#: back to check its shape against the task. An allow-list, because the point
#: is to take away everything that starts new work — ``exec``, ``execute_code``,
#: every search and fetch, every spawn.
WRAPUP_TOOLS: frozenset[str] = frozenset(
    {"write_file", "read_file", "list_directory", "todo_write"}
)


@dataclass(frozen=True)
class RunBudget:
    """How long this run gets, and who said so.

    ``source`` is not decoration: it decides whether the number may be scaled.
    """

    seconds: int
    source: str  # "external" | "manifest" | "fleet" | "none"

    @property
    def bounded(self) -> bool:
        return self.seconds > 0


def _external_budget_seconds() -> int:
    """A wall-clock budget imposed from outside this process, or 0.

    A harness, an orchestrator or a queue that will itself act when the clock
    runs out. Read through the settings registry rather than the environment
    so it is declared, documented and visible in the Helm like every other
    knob.

    A value the registry rejects is LOUD. Falling back silently restores the
    exact defect this module exists to remove — the run reverts to its
    tempo-scaled manifest ceiling, aiming past whatever is counting down —
    and a typo in a unit file is how that would happen twice.
    """
    from robothor.settings import get_settings

    try:
        return max(0, int(get_settings().engine.run_budget_seconds))
    except Exception as exc:  # noqa: BLE001 - never raise into a run's setup
        # The settings registry names the rejected value in its own message,
        # so the warning carries it without this module reading the
        # environment behind the registry's back.
        logger.warning(
            "ROBOTHOR_RUN_BUDGET_SECONDS is unusable, so no external budget is "
            "applied and this run falls back to its agent's own timeout — which "
            "is the very shape this control exists to remove. %s",
            str(exc).replace("\n", " ")[:300],
        )
        return 0


def resolve_run_budget(agent_config: Any, *, mode: str | None = None) -> RunBudget:
    """THE wall-clock budget for one run. One derivation, three sources.

    Precedence, most authoritative first:

    * an externally imposed budget (``ROBOTHOR_RUN_BUDGET_SECONDS``), taken
      EXACTLY — see this module's header for the 1600-against-1500 defect;
    * the agent's own ``timeout_seconds``, tempo-scaled exactly as before, so
      nothing about an ordinary fleet run changes;
    * the fleet ceiling, for an agent that declares no cap.

    ``mode`` is the step-efficiency rung, and the external branch is gated on
    it like everything else in this module. That is not a style choice:
    hostile review 2026-09-17 drove the loop at ``off`` with the variable set
    and got a run ending at the imposed 3s rather than at the manifest's 30s
    — so `off` was not the engine that shipped, and the benchmark harness
    exports that variable on EVERY run. The baseline arm of the very sweep
    this change has to be judged by would have carried the largest part of the
    fix. The watchdog and the loop still share this one derivation, so they
    cannot disagree about the number; what the rung decides is whether the
    imposed number is the one they share.

    ``None`` resolves the rung itself, for callers with no run in hand.
    """
    if mode is None:
        from robothor.engine.feature_flags import step_efficiency_mode

        mode = step_efficiency_mode()
    external = _external_budget_seconds() if mode != "off" else 0
    if external > 0:
        return RunBudget(seconds=external, source="external")
    from robothor.engine.watchdog_budgets import chain_for, effective_wallclock_ceiling

    declared = int(getattr(agent_config, "timeout_seconds", 0) or 0)
    seconds = effective_wallclock_ceiling(declared, chain_for(agent_config))
    if seconds <= 0:
        return RunBudget(seconds=0, source="none")
    return RunBudget(seconds=seconds, source="manifest" if declared > 0 else "fleet")


def wrapup_fraction() -> float:
    """Where in the budget wrap-up begins, clamped into a sane band."""
    try:
        from robothor.settings import get_settings

        raw = float(get_settings().engine.run_wrapup_fraction)
    except Exception:  # noqa: BLE001 - a bad value falls back, never raises
        return DEFAULT_WRAPUP_FRACTION
    return min(1.0, max(MIN_WRAPUP_FRACTION, raw))


def grace_seconds() -> int:
    """How long an in-flight call may outlive the budget before cancellation."""
    try:
        from robothor.settings import get_settings

        return max(0, int(get_settings().engine.run_budget_grace_seconds))
    except Exception:  # noqa: BLE001
        return DEFAULT_GRACE_SECONDS


def phase_for(elapsed: float, budget_seconds: int, fraction: float) -> Phase:
    """Which rung the clock says this run is on.

    An unbounded run is always ``normal``: a budget of 0 is the manifest's own
    sentinel for "no cap", and inventing one here would end runs an operator
    deliberately left uncapped.
    """
    if budget_seconds <= 0:
        return "normal"
    if elapsed >= budget_seconds:
        return "expired"
    if elapsed >= budget_seconds * fraction:
        return "wrapup"
    return "normal"


#: Where a wrapping-up run's stop is parked so tool admission can find it.
#: Admission has the session and nothing else; the alternative was threading a
#: budget through every gate signature for one branch.
WRAPUP_ATTR = "wrapup_stop"


def begin_wrapup(session: Any, stop: BudgetStop) -> None:
    """Make the narrowing real rather than advisory.

    Withdrawing a tool from the SCHEMA is a request. Hostile review 2026-09-17
    drove a model that kept asking for ``exec`` after the narrowing and the
    registry kept running it — eight times — so "nothing that starts new work
    survives" was false as written. From here admission refuses what the
    schema no longer offers, the same belt-and-suspenders the plan-mode and
    ``tools_allowed`` gates already have for exactly this reason.
    """
    with contextlib.suppress(AttributeError):
        setattr(session, WRAPUP_ATTR, stop)


def wrapup_refusal(session: Any, tool_name: str) -> str | None:
    """Why this call is refused during wrap-up, or None to let it run.

    The full explanation once per tool; a one-liner after that. A model that
    keeps asking is not helped by being lectured every turn, and the seconds
    remaining are what make the refusal actionable.
    """
    stop = getattr(session, WRAPUP_ATTR, None)
    if stop is None or tool_name in WRAPUP_TOOLS:
        return None
    remaining = max(0, math.ceil(stop.remaining()))
    if tool_name in stop.explained:
        return f"`{tool_name}` is unavailable: about {remaining}s left. Write the deliverable."
    stop.explained.add(tool_name)
    listed = ", ".join(sorted(WRAPUP_TOOLS))
    return (
        f"[SYSTEM] `{tool_name}` is withdrawn for the rest of this run: about "
        f"{remaining}s of the wall-clock budget remain and the run ENDS when they "
        f"are gone. Only {listed} are still available. Write your best current "
        "answer to the path the task named, then read it back and check its shape "
        "against what the task asked for."
    )


def wrapup_schemas(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tools a wrapping-up run may still call.

    Returns the input unchanged when the filter would empty it. An empty tool
    list is the engine's own signal for "answer in text only" (the hard-budget
    pre-flight uses it that way), and handing it to a model that still has a
    file to write would take away the one capability wrap-up exists to protect.
    """
    kept = [s for s in schemas if _schema_name(s) in WRAPUP_TOOLS]
    return kept or schemas


def _schema_name(schema: dict[str, Any]) -> str:
    function = schema.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return str(schema.get("name") or "")


def _missing_note(task_text: str | None, remaining: int, workspace: str | Path | None) -> str:
    from robothor.engine.deliverables import declared_paths, missing_deliverables_note

    return missing_deliverables_note(declared_paths(task_text), remaining, workspace) or ""


def wrapup_note(
    *,
    elapsed: float,
    budget_seconds: int,
    task_text: str | None,
    workspace: str | Path | None,
) -> str:
    """What the run is told when it enters wrap-up.

    Not another warning. The three pacing rungs already warned, in the run
    this exists for, and were ignored while the agent kept investigating. This
    says what has just CHANGED — the exploring tools are gone — and names the
    number of seconds rather than a percentage, because a percentage of an
    unstated total is not actionable.
    """
    # `ceil`, not `int`: truncation printed "0s … remain" for any remainder
    # under a second, which reads as "already over" at the one moment the
    # number has to be actionable (hostile review 2026-09-17, finding 9).
    remaining = max(0, math.ceil(budget_seconds - elapsed))
    listed = ", ".join(sorted(WRAPUP_TOOLS))
    parts = [
        f"[SYSTEM] WRAP-UP: {remaining}s of this run's {int(budget_seconds)}s budget "
        "remain, and the run WILL end when they are gone — not pause, end. "
        "Write your best current answer to the path the task named NOW, "
        "however incomplete, then read it back and check its shape against "
        "what the task asked for. Anything unwritten when the budget expires "
        "is lost, and partial results are graded per criterion. "
        f"Only these tools are still available: {listed}."
    ]
    missing = _missing_note(task_text, remaining, workspace)
    if missing:
        parts.append(missing)
    return "\n".join(parts)


def budget_reason(elapsed: float, budget_seconds: int) -> str:
    """Why the run ended, in the words the run row will carry."""
    return (
        f"Wall-clock budget exhausted: {int(elapsed)}s of {int(budget_seconds)}s. "
        "The run ended itself with the artefact it had."
    )


def _closing_summary(
    *, elapsed: float, budget_seconds: int, task_text: str | None, workspace: str | Path | None
) -> str:
    """The honest last word, when the model has no turn left to say one.

    Honest means it says the run was CUT, says when, and names what the task
    asked for that is not on disk — a summary that implies completion after a
    budget stop is the failure mode the honesty grading exists to catch.
    """
    from robothor.engine.deliverables import declared_paths

    lines = [
        f"[Run ended at its wall-clock budget: {int(elapsed)}s of "
        f"{int(budget_seconds)}s.] The answer below is the artefact as of that "
        "moment; no further work was done."
    ]
    from robothor.engine.deliverables import missing_paths

    paths = declared_paths(task_text)
    if paths and workspace:
        missing = missing_paths(paths, workspace)
        lines.append(
            ("Not written when the budget ended: " + ", ".join(missing))
            if missing
            else ("Written: " + ", ".join(paths))
        )
    return " ".join(lines)


def _close_dangling_tool_calls(session: Any) -> None:
    """Answer the tool calls this run will never make.

    The stop can land between a model's ``tool_calls`` message and its
    results — the clock is read again after every call returns, precisely so a
    tool turn does not START past the deadline. A persistent session carries
    that history into its next turn, where an assistant ``tool_calls`` message
    with no matching ``tool`` replies is a provider error rather than a
    curiosity. Saying why is also more use to whoever reads the transcript
    than a gap.
    """
    messages = getattr(session, "messages", None)
    if not messages:
        return
    last = messages[-1]
    if not (isinstance(last, dict) and last.get("role") == "assistant" and last.get("tool_calls")):
        return
    for call in last.get("tool_calls") or []:
        call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", "")
        messages.append(
            {
                "role": "tool",
                "tool_call_id": str(call_id or ""),
                "content": "[SYSTEM] Not run: the run reached its wall-clock budget.",
            }
        )


def end_run_at_budget(
    session: Any,
    budget: RunBudget,
    *,
    elapsed: float,
    task_text: str | None,
    workspace: str | Path | None,
) -> None:
    """End the run here, without another model call.

    Deliberately NOT ``_force_wrapup``: that asks the model for a closing
    summary, which is one more LLM call — the exact thing a run at 100 % of
    its budget must not start. The run has no turns left, so the engine writes
    the last word itself and lets the ordinary finalizer do the rest: the
    deliverable verdicts, the guardrail rows, delivery and persistence all run
    unchanged on the way out.

    Nothing here raises. A budget stop that crashes loses the artefact it
    exists to save.
    """
    reason = budget_reason(elapsed, budget.seconds)
    with contextlib.suppress(Exception):
        _close_dangling_tool_calls(session)
    with contextlib.suppress(Exception):
        session.run.budget_exhausted = True
        if not session.run.error_message:
            session.run.error_message = reason
    with contextlib.suppress(Exception):
        session.record_error(reason)
    with contextlib.suppress(Exception):
        if not (session.get_final_text() or "").strip():
            session.messages.append(
                {
                    "role": "assistant",
                    "content": _closing_summary(
                        elapsed=elapsed,
                        budget_seconds=budget.seconds,
                        task_text=task_text,
                        workspace=workspace,
                    ),
                }
            )
    _record(session, "blocked", reason, mode="enforce")
    logger.warning(
        "Run budget exhausted after %.0fs of %ds (%s) — run %s ended itself",
        elapsed,
        budget.seconds,
        budget.source,
        getattr(getattr(session, "run", None), "id", "?"),
    )


def _record(session: Any, action: str, reason: str, *, mode: str) -> None:
    """Land the decision in ``agent_guardrail_events``, best-effort.

    The table the flag's evidence source reads, and the same one the repeat
    guard writes to — reused, not invented.
    """
    run_id = str(getattr(getattr(session, "run", None), "id", "") or "")
    if not run_id:
        return
    try:
        from robothor.engine import tracking

        tracking.log_guardrail_event(
            run_id,
            "run_budget",
            action,
            reason=reason[:500],
            mode=mode,
            step_number=int(getattr(session, "_step_counter", 0) or 0),
        )
    except Exception as exc:  # noqa: BLE001 - evidence is never worth a run
        logger.debug("run budget event not recorded: %s", exc)


@dataclass
class BudgetStop:
    """One run's deadline, and the two decisions that follow from it.

    Held by the loop, read every iteration. ``now`` is injectable so the whole
    ladder is testable on a fake clock — a control whose tests have to wait
    twenty real minutes to see its last rung does not get tested.
    """

    #: A call that is already past its budget still gets this long, so the
    #: window never becomes a zero-timeout that fails a call before it starts.
    MIN_CALL_SECONDS = 1.0

    budget: RunBudget
    mode: str = "off"
    #: The live session, so an ``observe`` rung can leave a countable row
    #: rather than only a log line — see ``due``.
    session: Any = None
    fraction: float = DEFAULT_WRAPUP_FRACTION
    grace: int = DEFAULT_GRACE_SECONDS
    now: Callable[[], float] = time.monotonic
    started: float = field(default_factory=time.monotonic)
    #: Which rungs this run has already been told about, so a loop that checks
    #: every iteration does not repeat the wrap-up note thirty times.
    announced: set[str] = field(default_factory=set)
    #: Tools already refused during wrap-up, so the full explanation is given
    #: once per tool and a model that keeps asking is not lectured every turn.
    explained: set[str] = field(default_factory=set)

    @classmethod
    def for_run(
        cls, agent_config: Any, *, mode: str, watchdog: Any = None, session: Any = None
    ) -> BudgetStop:
        """The stop for one run, started where the RUN started.

        Not where the loop started: the watchdog covers setup too (it is
        started before the system prompt is built, deliberately), and a stop
        that begins counting later than the ceiling it shares would let the
        watchdog cancel the run first — reintroducing the destroyed-mid-call
        shape at a smaller scale.
        """
        started = time.monotonic()
        with contextlib.suppress(Exception):
            started -= max(0.0, float(getattr(watchdog, "elapsed_seconds", 0) or 0))
        return cls(
            budget=resolve_run_budget(agent_config, mode=mode),
            mode=mode,
            session=session,
            fraction=wrapup_fraction(),
            grace=grace_seconds(),
            started=started,
        )

    @property
    def elapsed(self) -> float:
        return max(0.0, self.now() - self.started)

    def remaining(self) -> float:
        if not self.budget.bounded:
            return 0.0
        return self.budget.seconds - self.elapsed

    def fraction_spent(self) -> float:
        """How much of the budget has gone, 0.0 when there is no budget.

        0.0 rather than None so a caller that only wants "are we past
        halfway" reads as "no" on an uncapped run, which is the safe answer:
        the controls that key on this all take something AWAY from the agent.
        """
        if not self.budget.bounded:
            return 0.0
        return self.elapsed / self.budget.seconds

    def due(self) -> Phase:
        """The rung to ACT on this iteration.

        ``off`` acts on nothing and says nothing — it is the engine that
        shipped. ``observe`` acts on nothing and says, once per rung, what
        ``enforce`` would have done; at WARNING, because the benchmark
        container installs no logging configuration and Python's
        ``lastResort`` handler drops everything below it.
        """
        phase = phase_for(self.elapsed, self.budget.seconds, self.fraction)
        if phase == "normal" or self.mode == "off":
            return "normal"
        if self.mode == "enforce":
            return phase
        if phase not in self.announced:
            self.announced.add(phase)
            reason = (
                f"would enter {phase} at {self.elapsed:.0f}s of "
                f"{self.budget.seconds}s ({self.budget.source})"
            )
            logger.warning("step-efficiency observe: run %s", reason)
            # A ROW, not only a line. `GUARDRAIL_FLIPS.md` promotes this flag
            # on `agent_guardrail_events`, and the repeat guard on the same
            # flag has always written at `observe` — so without this the two
            # halves of one rung had different evidence behaviour and the
            # fleet default could not produce a countable `run_budget` figure
            # at all (hostile review 2026-09-17, finding 5).
            _record(self.session, "observed", reason, mode=self.mode)
        return "normal"

    def announce(self, phase: Phase) -> bool:
        """True the first time this run reaches ``phase``. One-shot latch."""
        if phase in self.announced:
            return False
        self.announced.add(phase)
        return True

    def call_budget(self) -> float | None:
        """Seconds the next model call may take, or None for no bound.

        The budget's remainder plus the grace: a call that starts with 100s
        left may run 100 + grace and is then cancelled, so the run always
        reaches its own ending rather than somebody else's.
        """
        if not self.budget.bounded or self.mode != "enforce":
            return None
        return max(self.MIN_CALL_SECONDS, self.remaining() + self.grace)

    def call_window(self) -> Any:
        """A context manager bounding one model call.

        It cancels the call and raises ``RunBudgetError`` at the boundary —
        its own type, so a ``TimeoutError`` raised INSIDE by something else
        passes straight through (see ``_bounded_call``). The loop ALSO re-reads
        the clock after the call returns, because a callee that swallows its
        ``CancelledError`` and returns normally leaves the context manager
        nothing to raise — the shape recorded in the 2026-08-22 sweep, and the
        reason this is a belt as well as a brace.
        """
        seconds = self.call_budget()
        if seconds is None:
            return contextlib.nullcontext()
        return _bounded_call(seconds)
