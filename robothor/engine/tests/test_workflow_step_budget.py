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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import llm_client, workflow_budget
from robothor.engine.llm_client import LLMClient
from robothor.engine.model_breaker import ModelBreaker
from robothor.engine.models import (
    AgentConfig,
    RunStatus,
    WorkflowStepStatus,
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
