"""An attempt row is telemetry. It must not read as a run that errored.

Hostile review of #531, C1/C2/I1 + the consumer minors. The PR writes one
`agent_run_steps` row per failed LLM attempt — `step_type='llm_call'` with an
`error_message` — into columns three live consumers already read as "this run
went wrong":

- `run_lifecycle.py:646` counts every step with an `error_message` and hands
  the number to the verifier, whose default criteria is *"Task completed
  successfully without errors."* A failed verdict re-runs the whole agent loop.
  On ~93% of event-triggered runs the reasoning-only retry is the COMMON path,
  so a self-healed run would buy a second agent loop for nothing.
- `judge.py:462-468` renders the same rows to the goal judge as `tool_errors`
  and verbatim `error_steps:` lines — an LLM retry presented as a tool failure,
  the same shape as the 2026-08-27 incident where deploys counted as agent
  timeouts and flipped main's goal FAIL→PASS.
- `session.py` appended the model to `models_attempted`, whose documented
  meaning (`detectors.py:879-881`) is *the models that actually served an LLM
  call*. `check_primary_model_unreached` decides "reached" by membership, so an
  always-failing primary would satisfy it and the detector could never fire.

The invariant these all rest on: `record_llm_call` never sets `error_message`,
so an `llm_call` step that has one is an attempt row and nothing else.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.llm_attempts import (
    OUTCOME_REASONING_ONLY,
    OUTCOME_REASONING_ONLY_RETRY,
    is_attempt_step,
)
from robothor.engine.llm_client import LLMClient
from robothor.engine.models import StepType
from robothor.engine.run_lifecycle import RunLifecycleMixin
from robothor.engine.run_llm_calls import LLMCallMixin
from robothor.engine.session import AgentSession

THINKING_MODEL = "openrouter/deepseek/deepseek-v4.1-flash"


def _reply(*, content: str, reasoning: str | None = None, tokens: int = 40) -> Any:
    message = SimpleNamespace(content=content, tool_calls=None)
    if reasoning is not None:
        message.reasoning_content = reasoning
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="length")],
        usage=SimpleNamespace(
            prompt_tokens=900,
            completion_tokens=tokens,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=tokens),
        ),
        model=THINKING_MODEL,
    )


class _LLMRunner(LLMCallMixin):
    """The mixin's whole contract, so the rows come from the REAL dispatch."""

    def __init__(self) -> None:
        self._llm = LLMClient()
        self.config = SimpleNamespace()

    @property
    def _active_watchdog(self) -> Any:
        return None

    def _response_cost(self, **_kwargs: Any) -> float:
        return 0.0


async def _self_healed_session() -> AgentSession:
    """A run the engine ACTUALLY self-healed: reasoning-only, then an answer.

    Built by driving `_llm_call_and_record`, not by hand-writing the rows —
    a hand-built row proves the consumers filter something, never that the
    engine emits it (review N2).
    """
    session = AgentSession(agent_id="test-agent")
    runner = _LLMRunner()
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch(
            "robothor.engine.llm_client.litellm.acompletion",
            new=AsyncMock(
                side_effect=[
                    _reply(content="", reasoning="thinking about it…", tokens=9800),
                    _reply(content="done"),
                ]
            ),
        ),
        patch("robothor.engine.llm_client.TRANSIENT_RETRY_JITTER_MIN", 0.0),
        patch("robothor.engine.llm_client.TRANSIENT_RETRY_JITTER_MAX", 0.0),
    ):
        await runner._llm_call_and_record(session, [THINKING_MODEL], [], None, set(), 0.3)
    return session


@pytest.mark.asyncio
async def test_the_engine_records_the_self_heal_class() -> None:
    """N2: `reasoning_only_retry` had no caller — the class was never emitted.

    The production verification query cannot tell a self-heal from a failure
    unless the engine writes the distinction down.
    """
    session = await _self_healed_session()
    attempt, success = session.run.steps
    assert attempt.error_message is not None
    assert attempt.error_message.startswith(f"{OUTCOME_REASONING_ONLY_RETRY}:"), (
        f"the re-asked attempt must say it self-healed, got {attempt.error_message!r}"
    )
    assert success.error_message is None


@pytest.mark.asyncio
async def test_a_reasoning_only_reply_that_never_heals_keeps_the_plain_class() -> None:
    """The two classes must not collapse into one."""
    session = AgentSession(agent_id="test-agent")
    runner = _LLMRunner()
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch(
            "robothor.engine.llm_client.litellm.acompletion",
            new=AsyncMock(return_value=_reply(content="", reasoning="…", tokens=9800)),
        ),
        patch("robothor.engine.llm_client.TRANSIENT_RETRY_JITTER_MIN", 0.0),
        patch("robothor.engine.llm_client.TRANSIENT_RETRY_JITTER_MAX", 0.0),
    ):
        await runner._llm_call_and_record(session, [THINKING_MODEL], [], None, set(), 0.3)
    classes = [str(s.error_message).split(":")[0] for s in session.run.steps]
    assert classes.count(OUTCOME_REASONING_ONLY_RETRY) == 1, "the re-ask, once"
    assert OUTCOME_REASONING_ONLY in classes, "and the one that gave up, plainly"


class _Runner(RunLifecycleMixin):
    def __init__(self) -> None:
        self.config = SimpleNamespace()
        self.loops = 0

    async def _run_loop(self, *args: Any, **kwargs: Any) -> Any:
        self.loops += 1
        return None


# ─── the predicate ──────────────────────────────────────────────────────


class TestIsAttemptStep:
    @pytest.mark.asyncio
    async def test_an_llm_call_with_an_error_message_is_an_attempt(self) -> None:
        session = await _self_healed_session()
        attempt, success = session.run.steps
        assert is_attempt_step(attempt) is True
        assert is_attempt_step(success) is False

    def test_a_failed_tool_call_is_not_an_attempt(self) -> None:
        session = AgentSession(agent_id="test-agent")
        step = session.record_tool_call(
            tool_name="send_email",
            tool_input={},
            tool_output={},
            tool_call_id="1",
            error_message="SMTP refused",
        )
        assert is_attempt_step(step) is False

    def test_it_reads_a_database_row_too(self) -> None:
        assert is_attempt_step({"step_type": "llm_call", "error_message": "empty: …"}) is True
        assert is_attempt_step({"step_type": "llm_call", "error_message": None}) is False
        assert is_attempt_step({"step_type": "tool_call", "error_message": "boom"}) is False


# ─── C1: the verifier ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_self_healed_run_reports_zero_errors_to_the_verifier() -> None:
    runner = _Runner()
    session = await _self_healed_session()
    seen: dict[str, Any] = {}

    async def _verify(output, criteria, error_count, model, fallback_models=None):
        seen["error_count"] = error_count
        return SimpleNamespace(passed=True, issues=[], suggestions=[])

    with patch("robothor.engine.verifier.verify_output", new=_verify):
        out = await runner._run_verification(
            agent_config=SimpleNamespace(verification_prompt="did it work?"),
            session=session,
            models=["openrouter/primary"],
            tool_schemas=[],
            output_text="done",
            on_content=None,
            on_tool=None,
        )
    assert seen["error_count"] == 0, "a retried LLM attempt is not a run error"
    assert out == "done"
    assert runner.loops == 0, "a self-healed run must not buy a second agent loop"


@pytest.mark.asyncio
async def test_a_real_tool_error_is_still_counted() -> None:
    runner = _Runner()
    session = await _self_healed_session()
    session.record_tool_call(
        tool_name="send_email",
        tool_input={},
        tool_output={},
        tool_call_id="1",
        error_message="SMTP refused",
    )
    seen: dict[str, Any] = {}

    async def _verify(output, criteria, error_count, model, fallback_models=None):
        seen["error_count"] = error_count
        return SimpleNamespace(passed=True, issues=[], suggestions=[])

    with patch("robothor.engine.verifier.verify_output", new=_verify):
        await runner._run_verification(
            agent_config=SimpleNamespace(verification_prompt=None),
            session=session,
            models=["openrouter/primary"],
            tool_schemas=[],
            output_text="done",
            on_content=None,
            on_tool=None,
        )
    assert seen["error_count"] == 1


# ─── C2: models_attempted ───────────────────────────────────────────────


def test_a_failed_attempt_does_not_claim_the_model_served() -> None:
    """detectors.py:879-881 — that column lists the models that SERVED."""
    session = AgentSession(agent_id="test-agent")
    session.record_llm_attempt(model="openrouter/primary", duration_ms=1, error_message="empty: …")
    assert session.run.models_attempted == []
    assert not session.run.model_used


@pytest.mark.asyncio
async def test_the_primary_unreached_detector_still_sees_a_dead_primary() -> None:
    """An always-failing primary must not satisfy "reached" (C2)."""

    class _R(LLMCallMixin):
        def __init__(self) -> None:
            self._llm = LLMClient()
            self.config = SimpleNamespace()

        @property
        def _active_watchdog(self) -> Any:
            return None

        def _response_cost(self, **_kwargs: Any) -> float:
            return 0.0

    empty = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=None), finish_reason="stop"
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=0,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ),
        model="openrouter/primary",
    )
    runner = _R()
    session = AgentSession(agent_id="test-agent")
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", new=AsyncMock(return_value=empty)),
        patch("robothor.engine.llm_client.TRANSIENT_RETRY_JITTER_MIN", 0.0),
        patch("robothor.engine.llm_client.TRANSIENT_RETRY_JITTER_MAX", 0.0),
    ):
        await runner._llm_call_and_record(session, ["openrouter/primary"], [], None, set(), 0.3)

    assert session.run.models_attempted == [], (
        "a primary that never answered must stay OUT of models_attempted, or "
        "primary_model_unreached_detector can never fire again"
    )
    assert [s for s in session.run.steps if is_attempt_step(s)], (
        "the failure is still fully described — by the attempt rows"
    )


# ─── I1: the goal judge ─────────────────────────────────────────────────


class _FakeCursor:
    """Records the SQL the digest sends, and answers it with canned rows."""

    def __init__(self, step_rows: list[tuple[str, str | None]]) -> None:
        self.step_rows = step_rows
        self.sql: list[str] = []
        self._last = ""

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql.append(sql)
        self._last = sql

    def fetchone(self) -> Any:
        return ("completed", "the output", 1200, "cron", "")

    def fetchall(self) -> Any:
        return self.step_rows


def test_the_judge_digest_query_excludes_attempt_rows() -> None:
    """`tool_errors` must not count an LLM retry (judge.py:462-468).

    Asserted on the statement the digest actually executes, not on the module
    source — a grep over the file passes on the COMMENT that explains the
    filter, which is this codebase's standing weak-wiring-test trap.
    """
    from robothor.engine import judge

    cur = _FakeCursor([("tool_call", None), ("tool_call", "SMTP refused")])
    digest = judge._fetch_run_digest(cur, "run-1", "tenant-1")
    steps_sql = cur.sql[-1]
    assert "agent_run_steps" in steps_sql
    assert "step_type <> 'llm_call'" in steps_sql.split("--")[0] + steps_sql.replace("-- ", ""), (
        "the filter must be in the WHERE clause, not only in a comment"
    )
    assert "WHERE run_id = %s AND step_type <> 'llm_call'" in " ".join(steps_sql.split())
    assert digest is not None
    assert digest.tool_errors == 1


def test_buddy_error_evidence_excludes_attempt_rows() -> None:
    """M3: retries must not crowd the capped evidence list."""
    import inspect

    from robothor.engine import buddy_critic

    source = inspect.getsource(buddy_critic)
    clauses = [
        ln.strip() for ln in source.splitlines() if ln.strip().startswith("AND ") and "--" not in ln
    ]
    assert "AND step_type <> 'llm_call'" in clauses


# ─── M5/M6/M7: counters that treat a row as a turn ──────────────────────


def test_step_llm_call_counters_exclude_attempt_rows() -> None:
    import inspect

    from robothor.engine.tools.handlers import observability

    source = inspect.getsource(observability)
    assert "is_attempt_step" in source, (
        "total_llm_calls counts rows, and an attempt row is not a turn"
    )


def test_wildclaw_request_count_excludes_attempt_rows() -> None:
    from pathlib import Path

    source = Path(__file__).resolve().parents[3] / "bench" / "wildclaw" / "run_one.py"
    assert "is_attempt_step" in source.read_text()


def test_attempt_rows_stay_out_of_the_run_token_aggregates() -> None:
    """The billed response feeds the aggregates; the attempt row is diagnostic."""
    session = AgentSession(agent_id="test-agent")
    session.record_llm_attempt(
        model="openrouter/primary",
        duration_ms=1,
        input_tokens=900,
        output_tokens=9800,
        error_message="empty: …",
    )
    assert session.run.input_tokens == 0
    assert session.run.output_tokens == 0
    step = session.run.steps[0]
    assert step.step_type == StepType.LLM_CALL
    assert step.output_tokens == 9800
