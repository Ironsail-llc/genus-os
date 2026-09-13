"""A workflow step may not outspend the workflow, and must be visible while it runs.

Pins the 2026-09-13 diagnosis of ``email-pipeline``: every timed-out run showed
``steps 0/2`` with **zero** rows in ``workflow_run_steps``, because a step row
was only ever written when a step *finished*. A step that hung for the full 900s
left no trace at all, so "the workflow never started" and "step 1 has been
running for fifteen minutes" looked identical.

Underneath that was the reason it hung: the classify step's four-model chain is
allowed ``300 + 300 + 300 + 600`` seconds plus one 300s in-place retry — 1,800s
of worst case inside a 900s workflow budget. The workflow could only ever finish
while the primary answered first try.

Three claims, three groups:

* the step row exists *while* the step runs, and a timed-out run leaves it
  behind as ``timeout`` rather than deleting the evidence;
* loading a workflow whose step can outspend it says so, naming both numbers;
* at runtime the chain walk stops at the workflow deadline instead of starting
  a model the workflow cannot afford to finish.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import llm_client, workflow_budget
from robothor.engine.llm_client import LLMClient
from robothor.engine.model_breaker import ModelBreaker
from robothor.engine.models import (
    AgentConfig,
    RunStatus,
    WorkflowStepStatus,
    WorkflowStepType,
)
from robothor.engine.workflow import WorkflowEngine, parse_workflow
from robothor.engine.workflow_budget import (
    BudgetIssue,
    WorkflowDeadlineError,
    check_step_budgets,
    step_chain_allowance,
)

TENANT = "workflow-budget-test"

#: The chain from the incident: three cloud models at the batch timeout plus
#: the local tail at the Ollama timeout.
INCIDENT_CHAIN = [
    "openrouter/deepseek/deepseek-v4.1-flash",
    "openrouter/xiaomi/mimo-v2.5",
    "openrouter/deepseek/deepseek-v4-flash",
    "ollama_chat/qwen3.8:27b",
]


# ── fake persistence ───────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, statements: list[tuple[str, tuple]]) -> None:
        self._statements = statements
        self.rowcount = 1

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._statements.append((" ".join(sql.split()), params))

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, statements: list[tuple[str, tuple]]) -> None:
        self._statements = statements

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._statements)

    def commit(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextmanager
def _capture_sql():
    """Record every statement the engine's persistence layer issues."""
    statements: list[tuple[str, tuple]] = []
    with patch("robothor.db.connection.get_connection", lambda *a, **k: _FakeConn(statements)):
        yield statements


def _step_rows(statements: list[tuple[str, tuple]]) -> list[tuple[str, tuple]]:
    return [(sql, params) for sql, params in statements if "workflow_run_steps" in sql]


def _engine(wf_data: dict, runner=None) -> WorkflowEngine:
    config = MagicMock()
    config.tenant_id = TENANT
    engine = WorkflowEngine(config, runner=runner or MagicMock())
    engine._workflows[wf_data["id"]] = parse_workflow(wf_data)
    return engine


def _two_step_pipeline(timeout_seconds: int = 900) -> dict:
    """The shape of the pipeline in the diagnosis: classify, then respond."""
    return {
        "id": "budget-test-pipeline",
        "name": "Budget test pipeline",
        "timeout_seconds": timeout_seconds,
        "steps": [
            {
                "id": "classify",
                "type": "agent",
                "agent_id": "classifier",
                "message": "classify",
            },
            {
                "id": "respond",
                "type": "agent",
                "agent_id": "responder",
                "message": "respond",
            },
        ],
    }


@pytest.fixture
def no_alerts():
    with (
        patch("robothor.engine.workflow.WorkflowEngine._notify_run_failure"),
        patch("robothor.engine.dedup.try_acquire", new=AsyncMock(return_value=True)),
        patch("robothor.engine.dedup.release", new=AsyncMock()),
    ):
        yield


# ── 1. the step is visible while it runs ───────────────────────────────


class TestStepVisibility:
    @pytest.mark.asyncio
    async def test_a_step_that_never_completes_leaves_a_running_row(self, no_alerts):
        """``steps 0/2`` with no rows is what made the incident unreadable."""

        async def _hang(**_kwargs):
            await asyncio.sleep(30)

        runner = MagicMock()
        runner.execute = AsyncMock(side_effect=_hang)

        data = _two_step_pipeline(timeout_seconds=1)
        engine = _engine(data, runner=runner)

        with (
            patch(
                "robothor.engine.config.load_agent_config_or_reason",
                return_value=(_agent_config("classifier"), ""),
            ),
            patch("robothor.engine.delivery.deliver", new=AsyncMock()),
            _capture_sql() as statements,
        ):
            run = await engine.execute(data["id"], trigger_type="cron")

        assert run.status == RunStatus.TIMEOUT
        rows = _step_rows(statements)
        inserts = [(sql, p) for sql, p in rows if sql.startswith("INSERT")]
        assert inserts, "a step that never finished left no workflow_run_steps row at all"
        sql, params = inserts[0]
        assert WorkflowStepStatus.RUNNING.value in params, (
            f"the step row was not written as 'running': {params}"
        )
        assert "classify" in params
        started_at_written = [p for p in params if hasattr(p, "tzinfo")]
        assert started_at_written, "the running row carries no started_at"

    @pytest.mark.asyncio
    async def test_the_timed_out_run_closes_its_orphan_step_rows(self, no_alerts):
        """A row left 'running' forever is the immortal-orphan bug one layer down."""

        async def _hang(**_kwargs):
            await asyncio.sleep(30)

        runner = MagicMock()
        runner.execute = AsyncMock(side_effect=_hang)

        data = _two_step_pipeline(timeout_seconds=1)
        engine = _engine(data, runner=runner)

        with (
            patch(
                "robothor.engine.config.load_agent_config_or_reason",
                return_value=(_agent_config("classifier"), ""),
            ),
            patch("robothor.engine.delivery.deliver", new=AsyncMock()),
            _capture_sql() as statements,
        ):
            await engine.execute(data["id"], trigger_type="cron")

        closing = [
            (sql, p)
            for sql, p in _step_rows(statements)
            if sql.startswith("UPDATE") and WorkflowStepStatus.TIMEOUT.value in p
        ]
        assert closing, "the in-flight step row was never closed out as 'timeout'"

    @pytest.mark.asyncio
    async def test_a_completed_step_updates_its_running_row_rather_than_duplicating(
        self, no_alerts
    ):
        data = {
            "id": "budget-test-transform",
            "name": "Transform only",
            "timeout_seconds": 60,
            "steps": [{"id": "shape", "type": "transform", "expression": "'ok'"}],
        }
        engine = _engine(data)
        with _capture_sql() as statements:
            run = await engine.execute(data["id"], trigger_type="cron")

        assert run.status == RunStatus.COMPLETED
        rows = _step_rows(statements)
        inserts = [sql for sql, _ in rows if sql.startswith("INSERT")]
        updates = [sql for sql, _ in rows if sql.startswith("UPDATE")]
        assert len(inserts) == 1, f"expected one running row, got {len(inserts)}"
        assert updates, "the completed step never updated its running row"


# ── 2. budget coherence at load time ───────────────────────────────────


class TestBudgetCoherence:
    def test_the_incident_chain_costs_1800s(self):
        assert step_chain_allowance(INCIDENT_CHAIN) == 1800

    def test_a_step_that_can_outspend_the_workflow_is_flagged(self):
        wf = parse_workflow(_two_step_pipeline(timeout_seconds=900))
        issues = check_step_budgets(wf, lambda _agent: INCIDENT_CHAIN)

        classify = [i for i in issues if i.step_id == "classify"]
        assert classify, f"the classify step was not flagged: {issues}"
        issue = classify[0]
        assert issue.allowance_seconds == 1800
        assert issue.budget_seconds == 900
        assert issue.severity == "warning"
        assert "1800s" in issue.message
        assert "900s" in issue.message
        assert "classify" in issue.message

    def test_no_issue_when_the_budget_exceeds_the_chain(self):
        wf = parse_workflow(_two_step_pipeline(timeout_seconds=2400))
        assert check_step_budgets(wf, lambda _agent: INCIDENT_CHAIN) == []

    def test_strict_mode_promotes_the_same_finding_to_an_error(self):
        wf = parse_workflow(_two_step_pipeline(timeout_seconds=900))
        issues = check_step_budgets(wf, lambda _agent: INCIDENT_CHAIN, strict=True)
        assert issues and all(i.severity == "error" for i in issues)

    def test_non_agent_steps_are_not_flagged(self):
        wf = parse_workflow(
            {
                "id": "budget-test-noagent",
                "timeout_seconds": 1,
                "steps": [{"id": "shape", "type": "transform", "expression": "1"}],
            }
        )
        assert check_step_budgets(wf, lambda _agent: INCIDENT_CHAIN) == []

    def test_load_workflows_warns_with_both_numbers(self, tmp_path, caplog):
        """The check is worthless if nothing calls it where workflows load."""
        import yaml

        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "pipeline.yaml").write_text(yaml.safe_dump(_two_step_pipeline(900)))

        config = MagicMock()
        config.tenant_id = TENANT
        config.manifest_dir = tmp_path / "agents"
        engine = WorkflowEngine(config, runner=MagicMock())

        with (
            patch(
                "robothor.engine.config.load_agent_config",
                side_effect=lambda agent_id, *_a, **_k: _agent_config(agent_id),
            ),
            caplog.at_level(logging.WARNING, logger="robothor.engine.workflow"),
        ):
            assert engine.load_workflows(wf_dir) == 1

        budget_lines = [r.getMessage() for r in caplog.records if "1800" in r.getMessage()]
        assert budget_lines, f"no budget warning was logged: {caplog.text}"
        assert any("900" in line and "classify" in line for line in budget_lines)


# ── 3. the deadline bounds the chain walk at runtime ────────────────────


class TestRuntimeDeadline:
    def test_outside_a_workflow_the_clamp_is_inert(self):
        assert workflow_budget.bound_call_timeout(300.0, "openrouter/x") == 300.0

    def test_the_clamp_never_exceeds_what_is_left(self):
        with workflow_budget.workflow_deadline("wf", 20.0):
            assert workflow_budget.bound_call_timeout(300.0, "openrouter/x") <= 20.0

    def test_a_spent_budget_refuses_to_start_another_model(self):
        with workflow_budget.workflow_deadline("wf", 0.0), workflow_budget.step_scope("classify"):
            with pytest.raises(WorkflowDeadlineError) as exc:
                workflow_budget.bound_call_timeout(300.0, "openrouter/slow")
        assert exc.value.step_id == "classify"
        assert exc.value.model == "openrouter/slow"
        assert "classify" in str(exc.value)
        assert "openrouter/slow" in str(exc.value)

    @pytest.mark.asyncio
    async def test_the_chain_walk_stops_instead_of_trying_the_next_model(self, monkeypatch):
        """A fake model that sleeps past the remaining budget must not cost a second one."""
        monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MIN", 0.0)
        monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MAX", 0.0)
        fresh = ModelBreaker(on_open=None)
        monkeypatch.setattr(llm_client, "get_model_breaker", lambda: fresh)

        tried: list[str] = []

        async def _slow(**kwargs):
            tried.append(kwargs["model"])
            await asyncio.sleep(10)

        client = LLMClient()
        with (
            patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
            patch("robothor.engine.llm_client.litellm.acompletion", side_effect=_slow),
            workflow_budget.workflow_deadline("email-pipeline", 0.4),
            workflow_budget.step_scope("classify"),
        ):
            with pytest.raises(WorkflowDeadlineError) as exc:
                await client._call_llm(
                    [{"role": "user", "content": "hi"}],
                    ["openrouter/first", "openrouter/second"],
                    [],
                    broken_models=set(),
                    timeout_override=300.0,
                )

        assert tried == ["openrouter/first"], (
            f"the chain advanced past the workflow deadline: {tried}"
        )
        assert exc.value.step_id == "classify"

    @pytest.mark.asyncio
    async def test_the_run_records_timeout_naming_the_step(self, no_alerts):
        """The operator must be able to read which step ate the budget."""

        async def _hang(**_kwargs):
            await asyncio.sleep(30)

        runner = MagicMock()
        runner.execute = AsyncMock(side_effect=_hang)

        data = _two_step_pipeline(timeout_seconds=1)
        engine = _engine(data, runner=runner)

        with (
            patch(
                "robothor.engine.config.load_agent_config_or_reason",
                return_value=(_agent_config("classifier"), ""),
            ),
            patch("robothor.engine.delivery.deliver", new=AsyncMock()),
            _capture_sql(),
        ):
            run = await engine.execute(data["id"], trigger_type="cron")

        assert run.status == RunStatus.TIMEOUT
        assert "classify" in (run.error_message or ""), run.error_message


# ── 4. the deadline's identity survives every frame above it ───────────


class TestTheDeadlineIdentitySurvives:
    """Review C1: `WorkflowDeadlineError` subclasses `TimeoutError`, so
    ``runner.execute``'s cancel arm caught it and ``_cancel_outcome`` rewrote
    it as "Circuit-breaker hard timeout (3600s)". The step landed `failed`, the
    run landed `failed`, `timeout` was never written, migration 119's new value
    was unused on the primary path, and the agent run was stamped
    ``RunStatus.TIMEOUT`` with a circuit-breaker reason — the exact metric
    corruption ``GENUINE_TIMEOUT_SQL`` exists to prevent.
    """

    def test_a_workflow_deadline_is_not_a_circuit_breaker_timeout(self):
        """The classification is made from evidence, and the evidence here is
        that the agent's own clock never fired — the workflow's did."""
        from robothor.engine.analytics import EXTERNAL_CANCEL_PREFIX
        from robothor.engine.cancel_outcome import _cancel_outcome

        deadline = WorkflowDeadlineError("email-pipeline", "classify", "openrouter/x", 0.0)
        outcome = _cancel_outcome(
            timed_out=True,
            declared_timeout_seconds=0,
            effective_ceiling=3600,
            last_activity="llm_inflight:openrouter/x",
            workflow_deadline=str(deadline),
        )
        assert outcome.status is RunStatus.CANCELLED, (
            "a healthy agent cut short by its workflow's budget is not a timeout the "
            "agent earned — counting it as one is what GENUINE_TIMEOUT_SQL forbids"
        )
        assert "Circuit-breaker" not in outcome.reason
        assert not outcome.reason.startswith(EXTERNAL_CANCEL_PREFIX)
        for fragment in ("email-pipeline", "classify", "openrouter/x"):
            assert fragment in outcome.reason, outcome.reason

    def test_a_watchdog_abort_reason_cannot_mask_the_deadline(self):
        """`reason = abort_reason or _outcome.reason` and
        `watchdog_fired=bool(abort_reason)` are two more places the deadline's
        identity could be overwritten on its way to the row — and the second
        would re-stamp it `timeout` after the classification said otherwise."""
        from robothor.engine.cancel_outcome import terminal_run

        deadline = str(WorkflowDeadlineError("email-pipeline", "classify", "openrouter/x", 0.0))
        abort_reason = "watchdog: no progress for 300s"
        # The runner's own precedence, as written at the call site.
        reason = deadline or abort_reason
        assert reason == deadline

        session = MagicMock()
        outcome = MagicMock()
        outcome.status = RunStatus.CANCELLED
        terminal_run(session, outcome, reason, None, bool(abort_reason) and not deadline)
        session.cancelled.assert_called_once()
        session.timeout.assert_not_called()

    def test_the_deadline_propagates_to_the_caller_that_can_name_the_step(self):
        """Only the workflow engine knows which step this was. The runner must
        write its own row and then let the exception through, exactly as it
        already does for an outer cancellation."""
        from robothor.engine.workflow_budget import propagates_to_caller

        assert propagates_to_caller(asyncio.CancelledError())
        assert propagates_to_caller(WorkflowDeadlineError("wf", "s", "m", 0.0))
        # A run's OWN hard cap still returns a finished run rather than raising.
        assert not propagates_to_caller(TimeoutError("hard cap"))

    @pytest.mark.asyncio
    async def test_the_persisted_step_row_is_timeout_and_names_what_was_in_flight(self, no_alerts):
        """The claim, read back off the wire rather than out of memory."""
        deadline = WorkflowDeadlineError("budget-test-pipeline", "classify", INCIDENT_CHAIN[0], 0.0)

        async def _blow_the_budget(**_kwargs):
            raise deadline

        runner = MagicMock()
        runner.execute = AsyncMock(side_effect=_blow_the_budget)

        data = _two_step_pipeline(timeout_seconds=900)
        engine = _engine(data, runner=runner)

        with (
            patch(
                "robothor.engine.config.load_agent_config_or_reason",
                return_value=(_agent_config("classifier"), ""),
            ),
            patch("robothor.engine.delivery.deliver", new=AsyncMock()),
            _capture_sql() as statements,
        ):
            run = await engine.execute(data["id"], trigger_type="cron")

        assert run.status == RunStatus.TIMEOUT, (
            f"the run recorded {run.status.value}, not timeout: {run.error_message}"
        )
        closed = [
            params
            for sql, params in _step_rows(statements)
            if sql.startswith("UPDATE") and WorkflowStepStatus.TIMEOUT.value in params
        ]
        assert closed, "no workflow_run_steps row was written as 'timeout'"
        written = " ".join(str(p) for p in closed[0])
        for fragment in ("budget-test-pipeline", "classify", INCIDENT_CHAIN[0]):
            assert fragment in written, f"{fragment!r} missing from the persisted row: {written}"
        assert "Circuit-breaker" not in written

        # The second step never runs: the budget that killed the first is the
        # same budget the second would have to spend.
        assert "respond" not in written

    @pytest.mark.asyncio
    async def test_the_run_is_not_recorded_as_a_plain_step_failure(self, no_alerts):
        async def _blow_the_budget(**_kwargs):
            raise WorkflowDeadlineError("budget-test-pipeline", "classify", "openrouter/x", 0.0)

        runner = MagicMock()
        runner.execute = AsyncMock(side_effect=_blow_the_budget)
        data = _two_step_pipeline(timeout_seconds=900)
        engine = _engine(data, runner=runner)

        with (
            patch(
                "robothor.engine.config.load_agent_config_or_reason",
                return_value=(_agent_config("classifier"), ""),
            ),
            patch("robothor.engine.delivery.deliver", new=AsyncMock()),
            _capture_sql(),
        ):
            run = await engine.execute(data["id"], trigger_type="cron")

        assert run.status == RunStatus.TIMEOUT
        assert run.step_results[-1].status == WorkflowStepStatus.TIMEOUT
        assert "classify" in (run.error_message or "")


# ── 5. review follow-ups ───────────────────────────────────────────────


class TestTheLoaderSaysWhatItCouldNotCheck:
    """Review I3: on a platform checkout every agent manifest is gitignored,
    so every chain resolved to `[]`, `step_chain_allowance([])` returned 0, and
    the check produced no output at all — an inert control that looked exactly
    like a clean one."""

    def test_an_unresolvable_chain_is_reported_not_swallowed(self, tmp_path, caplog):
        import yaml

        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "pipeline.yaml").write_text(yaml.safe_dump(_two_step_pipeline(900)))

        config = MagicMock()
        config.tenant_id = TENANT
        config.manifest_dir = tmp_path / "agents"  # deliberately empty
        engine = WorkflowEngine(config, runner=MagicMock())

        with (
            patch("robothor.engine.config.load_agent_config", return_value=None),
            caplog.at_level(logging.WARNING, logger="robothor.engine.workflow"),
        ):
            engine.load_workflows(wf_dir)

        said = [r.getMessage() for r in caplog.records if "unresolved" in r.getMessage()]
        assert said, f"an unresolvable chain was checked silently: {caplog.text}"
        assert any("2" in line for line in said), said

    def test_a_fully_resolved_workflow_reports_what_it_checked(self, tmp_path, caplog):
        import yaml

        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "pipeline.yaml").write_text(yaml.safe_dump(_two_step_pipeline(2400)))

        config = MagicMock()
        config.tenant_id = TENANT
        config.manifest_dir = tmp_path / "agents"
        engine = WorkflowEngine(config, runner=MagicMock())

        with (
            patch(
                "robothor.engine.config.load_agent_config",
                side_effect=lambda agent_id, *_a, **_k: _agent_config(agent_id),
            ),
            caplog.at_level(logging.INFO, logger="robothor.engine.workflow"),
        ):
            engine.load_workflows(wf_dir)

        checked = [r.getMessage() for r in caplog.records if "agent step" in r.getMessage()]
        assert checked, f"the loader did not say what it checked: {caplog.text}"
        assert any("unresolved" not in line for line in checked)


class TestTheAllowanceMatchesTheRuntime:
    def test_the_local_primary_is_billed_its_own_retry_count(self):
        """Review M4: `_call_llm` gives a LOCAL primary
        ``1 + LOCAL_CAPACITY_RETRIES`` attempts, not ``1 +
        TRANSIENT_RETRIES_PER_MODEL``. Billing it one retry under-counts by
        three full Ollama allowances, which is a MISSED warning, not a
        conservative one."""
        from robothor.engine.llm_client import LOCAL_CAPACITY_RETRIES

        chain = ["ollama_chat/qwen3.8:27b", "openrouter/cloud"]
        # 600 (primary) + 300 (cloud) + LOCAL_CAPACITY_RETRIES x 600
        assert step_chain_allowance(chain) == 600 + 300 + LOCAL_CAPACITY_RETRIES * 600

    def test_static_and_runtime_agree_on_which_models_get_the_ollama_timeout(self):
        """Review M3: the docstring claimed the two could not drift; they had.
        `workflow_budget` matched `ollama/` too, the runtime did not."""
        from robothor.engine.llm_client import LLM_REQUEST_TIMEOUT_BATCH, _per_call_timeout

        for model in ("ollama/qwen3:8b", "ollama_chat/qwen3.8:27b", "openrouter/x"):
            runtime = _per_call_timeout(model, float(LLM_REQUEST_TIMEOUT_BATCH))
            assert workflow_budget.model_call_allowance(model) == int(runtime), model


class TestTheShippedWorkflowsSatisfyTheShippedCheck:
    """Review I4: `docs/workflows/*.yaml` are TRACKED platform files — only
    `docs/workflows/delphi/` and `.../retired/` are gitignored. The first
    version of this PR shipped a validator whose first three warnings fired
    against files in the same commit, and left the incident's own 900s budget
    in the platform tree on the grounds that it was instance data."""

    def test_every_tracked_workflow_clears_the_floor_for_a_four_model_chain(self):
        import yaml

        from robothor.engine.workflow import parse_workflow

        wf_dir = Path(__file__).resolve().parents[3] / "docs" / "workflows"
        floor = step_chain_allowance(INCIDENT_CHAIN)
        offenders = []
        for path in sorted(wf_dir.glob("*.yaml")):
            data = yaml.safe_load(path.read_text())
            if not (data and isinstance(data, dict) and "id" in data):
                continue
            wf = parse_workflow(data)
            if not any(s.type == WorkflowStepType.AGENT for s in wf.steps):
                continue
            if wf.timeout_seconds <= floor:
                offenders.append(f"{path.name}: {wf.timeout_seconds}s <= {floor}s")
        assert not offenders, (
            "the platform ships workflows whose own budget its own check rejects: "
            f"{offenders}. Raise the budget or say in docs/SYSTEM_ARCHITECTURE.md why not."
        )


class TestParallelBranchesDoNotOverwriteEachOther:
    """Review M6: `_in_flight_step` was a single key on the shared
    `run.context`, set and popped inside `_execute_single_step`, which
    `_execute_parallel` calls concurrently under `asyncio.gather`. The last
    branch to start overwrote the others and the first to finish popped the key
    for all of them, so a timed-out parallel workflow named the wrong branch."""

    @pytest.mark.asyncio
    async def test_a_slow_branch_is_still_named_after_a_fast_one_finishes(self, no_alerts):
        data = {
            "id": "budget-test-parallel",
            "name": "Parallel",
            "timeout_seconds": 1,
            "steps": [
                {
                    "id": "fan",
                    "type": "parallel",
                    # Order matters: the slow branch starts FIRST and is still
                    # in flight when the quick one finishes and pops the key.
                    "parallel_steps": [
                        {"id": "slow", "type": "agent", "agent_id": "classifier"},
                        {"id": "quick", "type": "transform", "expression": "'done'"},
                    ],
                }
            ],
        }

        async def _hang(**_kwargs):
            await asyncio.sleep(30)

        runner = MagicMock()
        runner.execute = AsyncMock(side_effect=_hang)
        engine = _engine(data, runner=runner)

        with (
            patch(
                "robothor.engine.config.load_agent_config_or_reason",
                return_value=(_agent_config("classifier"), ""),
            ),
            patch("robothor.engine.delivery.deliver", new=AsyncMock()),
            _capture_sql(),
        ):
            run = await engine.execute(data["id"], trigger_type="cron")

        assert run.status == RunStatus.TIMEOUT
        assert "slow" in (run.error_message or ""), (
            f"the fast branch's completion erased the slow branch: {run.error_message}"
        )


class TestTheStreamingClampIsInsideItsRetryLoop:
    """Review I1: `_call_llm` got the in-loop clamp, `_call_llm_streaming` did
    not — the same hole, left open in the sibling. Workflow steps take the
    non-streaming path today, which is luck, not a guard."""

    def test_both_chain_walks_clamp_inside_their_retry_loop(self):
        import ast
        import inspect
        import textwrap

        from robothor.engine.llm_client import LLMClient

        for name in ("_call_llm", "_call_llm_streaming"):
            src = textwrap.dedent(inspect.getsource(getattr(LLMClient, name)))
            tree = ast.parse(src)
            clamps_in_loops = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.While)
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "bound_call_timeout"
            ]
            assert clamps_in_loops, (
                f"{name} clamps the workflow deadline OUTSIDE its retry loop: a "
                "rotation or transient retry re-enters with a stale, possibly "
                "already-elapsed timeout and the chain advances past the deadline"
            )


class TestTheMessageNamesTheModelItRefused:
    def test_the_refused_model_is_named_and_the_skip_is_logged(self, caplog):
        """Review M5: the message said a model was "in flight" when it was the
        model the deadline REFUSED to dial, and the models the walk never
        reached produced no log line at all."""
        with (
            workflow_budget.workflow_deadline("wf", 0.0),
            workflow_budget.step_scope("classify"),
            caplog.at_level(logging.INFO, logger="robothor.engine.workflow_budget"),
        ):
            with pytest.raises(WorkflowDeadlineError) as exc:
                workflow_budget.bound_call_timeout(300.0, "openrouter/never-dialled")

        assert "in flight" not in str(exc.value), str(exc.value)
        assert "openrouter/never-dialled" in str(exc.value)
        assert any(
            "openrouter/never-dialled" in r.getMessage() and "skipping" in r.getMessage()
            for r in caplog.records
        ), f"the refused model produced no skip line: {caplog.text}"


# ── helpers ────────────────────────────────────────────────────────────


def _agent_config(agent_id: str) -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        name=agent_id,
        model_primary=INCIDENT_CHAIN[0],
        model_fallbacks=list(INCIDENT_CHAIN[1:]),
    )


def test_budget_issue_is_a_value_object():
    """Keeps BudgetIssue imported for readers of this module."""
    issue = BudgetIssue("wf", "s", "a", 1800, 900, "warning")
    assert issue.allowance_seconds > issue.budget_seconds
