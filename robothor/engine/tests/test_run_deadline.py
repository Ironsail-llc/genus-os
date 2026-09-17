"""The budget ends the run, on time, holding the best artefact it has.

Measured, 2026-09-17 Code Intelligence sweep, `connect_the_dots_hard`: the task
budget was 1200s, the harness backstop 1500s, and the engine's own ceiling —
the manifest number scaled by the model's tempo — resolved to **1600**. The
watchdog was therefore aiming four hundred seconds past the point where the
container would be destroyed. `wd.log` records a wait in flight at every tick
from `elapsed=1200` to `elapsed=1500`:

    tick elapsed=1440 idle=0 hard=1600 wait=llm_inflight:…:25
    tick elapsed=1470 idle=0 hard=1600 wait=llm_inflight:…:55
    tick elapsed=1500 idle=5 hard=1600 wait=llm_inflight:…:20

then `agent.log` holds one line — `HARNESS KILL after 1500s` — and `score.json`
a `FileNotFoundError` for a transcript that was never written. The task had
scored 0.545 the day before.

Three separate facts in that, and each has tests below:

* an externally imposed budget must not be scaled by anything (`RunBudget`);
* a run must stop CALLING before its budget ends, with enough left to write
  (the wrap-up rung);
* and when it does end, it ends itself rather than being destroyed — no model
  call in flight, an honest final summary, a guardrail row (`end_run_at_budget`).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _agent(timeout: int = 1200, model: str = "openrouter/test/model") -> Any:
    return SimpleNamespace(
        id="budgeted", timeout_seconds=timeout, model_primary=model, model_fallbacks=[]
    )


class _Clock:
    """A fake clock. Nothing here may wait on the real one."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ── The resolver ────────────────────────────────────────────────────────────


class TestOneResolver:
    def test_the_manifest_ceiling_is_the_budget_when_nothing_imposes_one(self) -> None:
        from robothor.engine.run_deadline import resolve_run_budget

        budget = resolve_run_budget(_agent(timeout=900))
        assert budget.seconds == 900
        assert budget.source == "manifest"

    def test_an_externally_imposed_budget_wins_and_is_never_scaled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 1600-against-1500 defect, in one assertion.

        Tempo scaling exists so a fleet that fell back to a local tier is not
        held to cloud timings. It is the right answer for a number an operator
        wrote in a manifest and the wrong one for a number somebody else is
        counting down: whoever imposed the budget is going to act on it.
        """
        from robothor.engine import run_deadline

        monkeypatch.setattr(run_deadline, "_external_budget_seconds", lambda: 1200)
        monkeypatch.setattr(
            "robothor.engine.model_registry.chain_tempo_factor", lambda models: 1.3333
        )
        budget = run_deadline.resolve_run_budget(_agent(timeout=1200))
        assert budget.seconds == 1200, "an imposed budget was inflated past the imposer"
        assert budget.source == "external"

    def test_a_manifest_budget_keeps_its_tempo_scaling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from robothor.engine import run_deadline

        monkeypatch.setattr(run_deadline, "_external_budget_seconds", lambda: 0)
        monkeypatch.setattr("robothor.engine.model_registry.chain_tempo_factor", lambda models: 2.0)
        assert run_deadline.resolve_run_budget(_agent(timeout=600)).seconds == 1200

    def test_an_uncapped_agent_falls_back_to_the_fleet_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from robothor.engine import run_deadline

        monkeypatch.setattr(run_deadline, "_external_budget_seconds", lambda: 0)
        monkeypatch.setenv("ROBOTHOR_MAX_WALLCLOCK_SECONDS", "3600")
        budget = run_deadline.resolve_run_budget(_agent(timeout=0))
        assert budget.seconds > 0
        assert budget.source == "fleet"

    def test_off_is_the_engine_that_shipped_even_with_a_budget_imposed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hostile review 2026-09-17, finding 1 — the critical one.

        The resolver was not gated on the rung, and the bench harness exports
        `ROBOTHOR_RUN_BUDGET_SECONDS` on EVERY run whatever the rung says. So
        the `off` arm of the differential this change has to be judged by was
        already receiving the largest part of the fix: a run at `off` ended at
        the imposed 3s instead of the manifest's 30s. The watchdog and the
        loop still share one derivation; what the rung decides is whether the
        imposed number is the one they share.
        """
        from robothor.engine import run_deadline

        monkeypatch.setenv("ROBOTHOR_RUN_BUDGET_SECONDS", "1200")
        monkeypatch.setattr("robothor.engine.model_registry.chain_tempo_factor", lambda m: 1.3333)
        agent = _agent(timeout=1200)
        assert run_deadline.resolve_run_budget(agent, mode="off").seconds == 1599
        assert run_deadline.resolve_run_budget(agent, mode="off").source == "manifest"
        assert run_deadline.resolve_run_budget(agent, mode="enforce").seconds == 1200

    def test_observe_still_gets_the_imposed_number(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`observe` is allowed to differ from `off` — that is the ladder.

        It acts on nothing; it only measures against the number an operator
        would get at `enforce`, which is what makes its shadow rows worth
        reading.
        """
        from robothor.engine import run_deadline

        monkeypatch.setenv("ROBOTHOR_RUN_BUDGET_SECONDS", "1200")
        assert run_deadline.resolve_run_budget(_agent(), mode="observe").source == "external"

    def test_the_watchdog_at_off_gets_the_shipped_ceiling_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One derivation, so the watchdog cannot disagree with the loop."""
        from robothor.engine.watchdog_budgets import watchdog_budgets_for

        monkeypatch.setenv("ROBOTHOR_RUN_BUDGET_SECONDS", "3")
        monkeypatch.setenv("ROBOTHOR_STEP_EFFICIENCY_MODE", "off")
        config = SimpleNamespace(
            timeout_seconds=30,
            stall_timeout_seconds=0,
            early_stall_timeout_seconds=0,
            model_primary="openrouter/test/model",
            model_fallbacks=[],
        )
        assert watchdog_budgets_for(config).hard == 30

    def test_a_nonsense_budget_is_loud_rather_than_silent(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Silently falling back restores the defect this module removes."""
        import logging

        from robothor.engine import run_deadline

        monkeypatch.setenv("ROBOTHOR_RUN_BUDGET_SECONDS", "twenty minutes")
        with caplog.at_level(logging.WARNING, logger="robothor.engine.run_deadline"):
            budget = run_deadline.resolve_run_budget(_agent(timeout=900), mode="enforce")
        assert budget.source == "manifest"
        assert any("ROBOTHOR_RUN_BUDGET_SECONDS" in r.message for r in caplog.records)

    def test_the_watchdog_ceiling_comes_from_the_same_resolver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two derivations of one ceiling is how a loop kills a run at 1200s
        while the watchdog believes it has 1600."""
        from robothor.engine import run_deadline
        from robothor.engine.watchdog_budgets import watchdog_budgets_for

        monkeypatch.setattr(run_deadline, "_external_budget_seconds", lambda: 1200)
        monkeypatch.setattr("robothor.engine.model_registry.chain_tempo_factor", lambda models: 1.5)
        config = SimpleNamespace(
            timeout_seconds=1200,
            stall_timeout_seconds=0,
            early_stall_timeout_seconds=0,
            model_primary="openrouter/test/model",
            model_fallbacks=[],
        )
        assert watchdog_budgets_for(config).hard == 1200


# ── The ladder ──────────────────────────────────────────────────────────────


class TestThePhases:
    def test_below_the_wrapup_fraction_nothing_changes(self) -> None:
        from robothor.engine.run_deadline import phase_for

        assert phase_for(1000.0, 1200, 0.9) == "normal"

    def test_at_ninety_percent_the_run_wraps_up(self) -> None:
        from robothor.engine.run_deadline import phase_for

        assert phase_for(1080.0, 1200, 0.9) == "wrapup"

    def test_at_the_budget_the_run_is_expired(self) -> None:
        from robothor.engine.run_deadline import phase_for

        assert phase_for(1200.0, 1200, 0.9) == "expired"

    def test_an_unbounded_run_is_never_in_any_phase_but_normal(self) -> None:
        from robothor.engine.run_deadline import phase_for

        assert phase_for(99_999.0, 0, 0.9) == "normal"

    def test_the_fraction_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from robothor.engine import run_deadline

        monkeypatch.setenv("ROBOTHOR_RUN_WRAPUP_FRACTION", "0.75")
        assert run_deadline.wrapup_fraction() == pytest.approx(0.75)

    def test_a_nonsense_fraction_is_clamped_rather_than_obeyed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fraction of 0 would put every run in wrap-up from iteration 0."""
        from robothor.engine import run_deadline

        monkeypatch.setenv("ROBOTHOR_RUN_WRAPUP_FRACTION", "0")
        assert run_deadline.wrapup_fraction() >= run_deadline.MIN_WRAPUP_FRACTION
        monkeypatch.setenv("ROBOTHOR_RUN_WRAPUP_FRACTION", "5")
        assert run_deadline.wrapup_fraction() <= 1.0


class TestTheStopIsMeasuredInSeconds:
    """The hard stop is wall clock, and nothing else.

    Iterations were the wrong unit twice over. A run that spends 400s inside
    one model call has taken one iteration and has no budget left; and a
    snippet that calls fifty tools through `genus_tools` (#583) is ONE
    iteration however much work it does — which is the whole point of pricing
    batch work at one turn, and would be undone by a deadline that counted
    turns.
    """

    def test_one_long_iteration_can_expire_the_budget(self) -> None:
        from robothor.engine.run_deadline import phase_for

        assert phase_for(1300.0, 1200, 0.9) == "expired"

    def test_a_thousand_fast_iterations_inside_the_budget_do_not(self) -> None:
        from robothor.engine.run_deadline import phase_for

        assert phase_for(10.0, 1200, 0.9) == "normal"

    def test_a_snippet_costs_the_loop_one_iteration_however_many_calls_it_makes(
        self,
    ) -> None:
        """`execute_code`'s refund, pinned where it actually lives.

        There is no counter to refund: the loop increments `_iteration` once
        per pass, and a snippet's proxied calls never reach it — they go
        through `tool_proxy`, bounded by `ROBOTHOR_EXECUTE_CODE_MAX_CALLS`.
        The refund is structural, and this is the assertion that keeps it so:
        if the loop ever started counting tool calls, this fails.
        """
        import ast
        from pathlib import Path as _Path

        import robothor.engine.runner as m

        tree = ast.parse(_Path(m.__file__).read_text(encoding="utf-8"))
        loop = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_loop"
        )
        increments = [
            n
            for n in ast.walk(loop)
            if isinstance(n, ast.AugAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == "_iteration"
        ]
        assert len(increments) == 1, "the loop counts iterations in exactly one place"


class TestTheStop:
    def _stop(self, mode: str = "enforce", clock: _Clock | None = None, **kw: Any) -> Any:
        from robothor.engine.run_deadline import BudgetStop, RunBudget

        clock = clock or _Clock()
        return BudgetStop(
            budget=RunBudget(seconds=kw.pop("seconds", 1200), source="external"),
            mode=mode,
            now=clock,
            started=clock(),
            **kw,
        )

    def test_the_ladder_runs_normal_then_wrapup_then_expired(self) -> None:
        clock = _Clock()
        stop = self._stop(clock=clock)
        assert stop.due() == "normal"
        clock.advance(1080)
        assert stop.due() == "wrapup"
        clock.advance(120)
        assert stop.due() == "expired"

    def test_a_run_that_finishes_early_is_untouched(self) -> None:
        """Fifty iterations inside the budget see no rung at all."""
        clock = _Clock()
        stop = self._stop(clock=clock)
        for _ in range(50):
            clock.advance(20)
            assert stop.due() == "normal"
        assert stop.elapsed == 1000
        assert not stop.announced

    def test_off_is_the_engine_that_shipped(self) -> None:
        """`off` has to be the previous engine, or a sweep measures nothing."""
        clock = _Clock()
        stop = self._stop(mode="off", clock=clock)
        clock.advance(5000)
        assert stop.due() == "normal"

    def test_observe_acts_on_nothing_and_says_what_enforce_would_do(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        clock = _Clock()
        stop = self._stop(mode="observe", clock=clock)
        clock.advance(1200)
        with caplog.at_level(logging.WARNING, logger="robothor.engine.run_deadline"):
            assert stop.due() == "normal"
        assert any("would" in r.message for r in caplog.records)

    def test_observe_leaves_a_countable_row_not_only_a_log_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hostile review 2026-09-17, finding 5.

        `GUARDRAIL_FLIPS.md` promotes this flag on `agent_guardrail_events`,
        and the repeat guard on the SAME flag has always written at `observe`.
        Without this the two halves of one rung had different evidence
        behaviour and the fleet default could produce no `run_budget` figure
        at all.
        """
        rows: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event",
            lambda *a, **kw: rows.append((a, kw)),
        )
        session = _session()
        session.run.id = "run-observe"
        clock = _Clock()
        stop = self._stop(mode="observe", clock=clock, session=session)
        clock.advance(1100)
        assert stop.due() == "normal"
        assert rows, "observe recorded nothing anyone can query"
        assert rows[0][0][1] == "run_budget"
        assert rows[0][0][2] == "observed"
        assert rows[0][1]["mode"] == "observe"

    def test_observe_records_each_rung_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows: list[Any] = []
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event", lambda *a, **kw: rows.append(a)
        )
        session = _session()
        session.run.id = "run-observe"
        clock = _Clock()
        stop = self._stop(mode="observe", clock=clock, session=session)
        clock.advance(1100)
        for _ in range(5):
            stop.due()
        clock.advance(200)
        for _ in range(5):
            stop.due()
        assert len(rows) == 2, "one row per rung, not one per iteration"

    def test_off_records_nothing_at_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows: list[Any] = []
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event", lambda *a, **kw: rows.append(a)
        )
        session = _session()
        session.run.id = "run-off"
        clock = _Clock()
        stop = self._stop(mode="off", clock=clock, session=session)
        clock.advance(5000)
        stop.due()
        assert rows == []

    def test_the_call_window_leaves_the_budget_plus_a_grace_and_no_more(self) -> None:
        clock = _Clock()
        stop = self._stop(clock=clock, grace=15)
        clock.advance(1100)
        assert stop.call_budget() == pytest.approx(115.0)

    def test_a_call_started_past_the_budget_still_gets_the_grace_not_a_negative(
        self,
    ) -> None:
        clock = _Clock()
        stop = self._stop(clock=clock, grace=15)
        clock.advance(1300)
        assert stop.call_budget() == pytest.approx(stop.MIN_CALL_SECONDS)

    def test_an_unbounded_run_bounds_no_call(self) -> None:
        stop = self._stop(seconds=0)
        assert stop.call_budget() is None

    @pytest.mark.asyncio
    async def test_the_window_cancels_a_call_that_outlives_the_grace(self) -> None:
        clock = _Clock()
        stop = self._stop(clock=clock, grace=0)
        clock.advance(1200)
        cancelled = False
        with pytest.raises(TimeoutError):
            async with stop.call_window():
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled = True
                    raise
        assert cancelled, "the in-flight call was never actually cancelled"

    @pytest.mark.asyncio
    async def test_someone_elses_timeout_passes_straight_through(self) -> None:
        """`TimeoutError` is not a sentence with one meaning.

        `WorkflowDeadlineError` is a `TimeoutError` subclass that has to escape
        `execute()` so the workflow engine can say which step it was. A bare
        `except TimeoutError` around the model call swallowed it and recorded
        the run against the wrong cause; the window raises its OWN type.
        """
        from robothor.engine.run_deadline import RunBudgetError

        clock = _Clock()
        stop = self._stop(clock=clock)
        with pytest.raises(TimeoutError) as caught:
            async with stop.call_window():
                raise TimeoutError("somebody else's deadline")
        assert not isinstance(caught.value, RunBudgetError)

    @pytest.mark.asyncio
    async def test_the_windows_own_expiry_is_its_own_type(self) -> None:
        from robothor.engine.run_deadline import RunBudgetError

        clock = _Clock()
        stop = self._stop(clock=clock, grace=0)
        clock.advance(1200)
        with pytest.raises(RunBudgetError):
            async with stop.call_window():
                await asyncio.sleep(30)

    @pytest.mark.asyncio
    async def test_a_call_inside_the_window_is_untouched(self) -> None:
        clock = _Clock()
        stop = self._stop(clock=clock)
        async with stop.call_window():
            await asyncio.sleep(0)
        assert stop.due() == "normal"


# ── Wrap-up: what the model may still do ────────────────────────────────────


class TestWrapUpIsEnforcedNotAdvised:
    """Withdrawing a tool from the SCHEMA is a request, not a rule.

    Hostile review 2026-09-17, finding 2: a model that kept asking for `exec`
    after the narrowing had it executed eight times, so "nothing that starts
    new work survives" was false as written. Admission now refuses it, the
    same belt-and-suspenders the plan-mode and `tools_allowed` gates have.
    """

    def _wrapping_up(self, seconds: int = 1200, elapsed: float = 1150.0) -> Any:
        from robothor.engine.run_deadline import BudgetStop, RunBudget, begin_wrapup

        clock = _Clock()
        session = _session()
        stop = BudgetStop(
            budget=RunBudget(seconds=seconds, source="external"),
            mode="enforce",
            session=session,
            now=clock,
            started=clock(),
        )
        clock.advance(elapsed)
        begin_wrapup(session, stop)
        return session

    def test_a_withdrawn_tool_is_refused_with_the_seconds_and_the_alternatives(self) -> None:
        from robothor.engine.run_deadline import wrapup_refusal

        session = self._wrapping_up()
        refusal = wrapup_refusal(session, "exec")
        assert refusal is not None
        assert "50s" in refusal
        assert "write_file" in refusal

    def test_a_writing_tool_is_untouched(self) -> None:
        from robothor.engine.run_deadline import wrapup_refusal

        session = self._wrapping_up()
        for tool in ("write_file", "read_file", "list_directory", "todo_write"):
            assert wrapup_refusal(session, tool) is None

    def test_a_run_not_in_wrapup_refuses_nothing(self) -> None:
        from robothor.engine.run_deadline import wrapup_refusal

        assert wrapup_refusal(_session(), "exec") is None

    def test_the_explanation_is_given_once_per_tool_and_the_refusal_still_stands(self) -> None:
        from robothor.engine.run_deadline import wrapup_refusal

        session = self._wrapping_up()
        first = wrapup_refusal(session, "exec")
        second = wrapup_refusal(session, "exec")
        assert first is not None and second is not None
        assert len(second) < len(first), "the model is not lectured every turn"
        assert wrapup_refusal(session, "web_search") is not None

    @pytest.mark.asyncio
    async def test_admission_is_where_it_acts(self) -> None:
        """Pinned through the real gate, not through the helper."""
        from robothor.engine.runner import AgentRunner

        session = self._wrapping_up()
        verdict = await AgentRunner._admit_tool_call(
            object.__new__(AgentRunner),
            tc=SimpleNamespace(id="call_1"),
            tool_name="exec",
            tool_args={"command": "ls"},
            session=session,
            agent_config=SimpleNamespace(id="a"),
            guardrail_engine=None,
            hook_registry=None,
            readonly_mode=False,
            readonly_tool_set=frozenset(),
            allowed_tool_set=frozenset({"exec", "write_file"}),
        )
        assert verdict.allowed is False
        assert verdict.output["guard"] == "run_wrapup"
        assert verdict.escalate is False
        assert verdict.count_as_iteration_error is False, (
            "being redirected to write in the last tenth is the control working"
        )


class TestWrapUp:
    def test_only_writing_and_checking_survive_the_filter(self) -> None:
        from robothor.engine.run_deadline import WRAPUP_TOOLS, wrapup_schemas

        schemas = [
            {"type": "function", "function": {"name": n}}
            for n in ("exec", "write_file", "web_search", "read_file", "execute_code")
        ]
        kept = {s["function"]["name"] for s in wrapup_schemas(schemas)}
        assert kept == {"write_file", "read_file"} <= WRAPUP_TOOLS
        assert "exec" not in kept and "web_search" not in kept

    def test_the_filter_never_returns_an_empty_schema_list(self) -> None:
        """An empty list is the engine's own signal for 'answer in text only'.

        Handing it to a model that still has a file to write would take away
        the one capability wrap-up exists to protect.
        """
        from robothor.engine.run_deadline import wrapup_schemas

        schemas = [{"type": "function", "function": {"name": "exec"}}]
        assert wrapup_schemas(schemas) == schemas

    def test_the_note_says_how_many_seconds_are_left_and_what_to_do(self) -> None:
        from robothor.engine.run_deadline import wrapup_note

        note = wrapup_note(elapsed=1080.0, budget_seconds=1200, task_text=None, workspace=None)
        assert "120s" in note
        assert "NOW" in note

    def test_a_remainder_under_a_second_does_not_read_as_zero(self) -> None:
        """`int()` printed "0s … remain" at the one moment the number has to
        be actionable (hostile review 2026-09-17, finding 9)."""
        from robothor.engine.run_deadline import wrapup_note

        note = wrapup_note(elapsed=9.4, budget_seconds=10, task_text=None, workspace=None)
        assert "1s of this run's 10s budget remain" in note

    def test_the_note_names_a_declared_path_that_is_not_on_disk(self, tmp_path: Path) -> None:
        from robothor.engine.run_deadline import wrapup_note

        wanted = str(tmp_path / "results" / "answer.md")
        note = wrapup_note(
            elapsed=1080.0,
            budget_seconds=1200,
            task_text=f"Write the summary to {wanted} when you are done.",
            workspace=tmp_path,
        )
        assert wanted in note


# ── Expiry: the run ends itself ─────────────────────────────────────────────


def _session() -> Any:
    from robothor.engine.session import AgentSession

    return AgentSession("budget-test")


class TestEndingAtBudget:
    def test_the_run_is_marked_budget_exhausted(self) -> None:
        from robothor.engine.run_deadline import RunBudget, end_run_at_budget

        session = _session()
        end_run_at_budget(
            session,
            RunBudget(seconds=1200, source="external"),
            elapsed=1201.0,
            task_text=None,
            workspace=None,
        )
        assert session.run.budget_exhausted is True
        assert "budget" in (session.run.error_message or "").lower()

    def test_it_leaves_an_honest_final_summary_rather_than_silence(self) -> None:
        from robothor.engine.run_deadline import RunBudget, end_run_at_budget

        session = _session()
        end_run_at_budget(
            session,
            RunBudget(seconds=1200, source="external"),
            elapsed=1201.0,
            task_text=None,
            workspace=None,
        )
        text = session.get_final_text() or ""
        assert "budget" in text.lower()
        assert "1200" in text

    def test_a_final_answer_the_model_already_gave_is_not_overwritten(self) -> None:
        from robothor.engine.run_deadline import RunBudget, end_run_at_budget

        session = _session()
        session.messages.append({"role": "assistant", "content": "Here is the answer."})
        end_run_at_budget(
            session,
            RunBudget(seconds=1200, source="external"),
            elapsed=1201.0,
            task_text=None,
            workspace=None,
        )
        assert "Here is the answer." in (session.get_final_text() or "")

    def test_the_summary_names_the_deliverable_that_is_missing(self, tmp_path: Path) -> None:
        from robothor.engine.run_deadline import RunBudget, end_run_at_budget

        session = _session()
        wanted = str(tmp_path / "results" / "answer.md")
        end_run_at_budget(
            session,
            RunBudget(seconds=1200, source="external"),
            elapsed=1201.0,
            task_text=f"Write your answer to {wanted}",
            workspace=tmp_path,
        )
        assert wanted in (session.get_final_text() or "")

    def test_tool_calls_the_run_will_never_make_are_answered(self) -> None:
        """The stop can land between a `tool_calls` message and its results.

        A persistent session carries that history into its next turn, where an
        assistant `tool_calls` message with no matching `tool` replies is a
        provider error rather than a curiosity.
        """
        from robothor.engine.run_deadline import RunBudget, end_run_at_budget

        session = _session()
        session.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_7", "function": {"name": "exec"}}],
            }
        )
        end_run_at_budget(
            session,
            RunBudget(seconds=1200, source="external"),
            elapsed=1201.0,
            task_text=None,
            workspace=None,
        )
        replies = [m for m in session.messages if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in replies] == ["call_7"]
        assert "budget" in replies[0]["content"].lower()

    def test_a_balanced_conversation_gains_no_tool_replies(self) -> None:
        from robothor.engine.run_deadline import RunBudget, end_run_at_budget

        session = _session()
        session.messages.append({"role": "assistant", "content": "all done"})
        end_run_at_budget(
            session,
            RunBudget(seconds=1200, source="external"),
            elapsed=1201.0,
            task_text=None,
            workspace=None,
        )
        assert not [m for m in session.messages if m.get("role") == "tool"]

    def test_a_guardrail_row_records_the_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from robothor.engine import run_deadline

        rows: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event",
            lambda *a, **kw: rows.append((a, kw)),
        )
        session = _session()
        session.run.id = "run-1"
        run_deadline.end_run_at_budget(
            session,
            run_deadline.RunBudget(seconds=1200, source="external"),
            elapsed=1201.0,
            task_text=None,
            workspace=None,
        )
        assert rows, "the stop left no evidence anyone can query"
        assert rows[0][0][1] == "run_budget"
        assert rows[0][0][2] == "blocked"

    def test_nothing_here_raises_into_finalization(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A budget stop that crashes loses the artefact it exists to save."""
        from robothor.engine import run_deadline

        def _boom(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("no database here")

        monkeypatch.setattr("robothor.engine.tracking.log_guardrail_event", _boom)
        session = _session()
        run_deadline.end_run_at_budget(
            session,
            run_deadline.RunBudget(seconds=1200, source="external"),
            elapsed=1201.0,
            task_text=None,
            workspace=None,
        )
        assert session.run.budget_exhausted is True
