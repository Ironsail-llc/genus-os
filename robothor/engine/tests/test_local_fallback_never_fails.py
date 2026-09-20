"""The local fallback answers, or the operator is told why in a sentence.

The incident, 2026-09-16 ~21:49Z, production, agent main, trigger telegram.
The operator asked for a task and got back:

    Failure: error. All models failed to respond.

The cloud key had hit its weekly cap, so from step 1 the run was already on
the last fallback — a local Ollama model — and steps 1..12 answered on it
fine. Step 13 produced a large tool result; step 14 failed five times with
``no user query found in messages``, each treated as a transient 500 with
backoff, and the chain then reported every model failed.

Three defects, one incident:

1. the context budget was sized against the configured PRIMARY, because a
   model skipped for an exhausted credential pool is not in ``broken_models``;
2. an overflow was retried five times against the same oversized messages,
   which cannot ever succeed;
3. the run's last word to the operator was a ``RuntimeError`` repr, on a box
   that still had a working local model sitting idle.

These tests are that incident, in the three places it can be stopped.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from robothor.engine import key_pool
from robothor.engine.context import estimate_tokens
from robothor.engine.context_budget import keep_context_within_budget
from robothor.engine.context_fit import fit_for, next_reachable_model
from robothor.engine.llm_client import LLMClient
from robothor.engine.model_registry import get_model_limits
from robothor.engine.session import ENGINE_CONTEXT_ROLE

CLOUD = "openrouter/deepseek/deepseek-v4.1-flash"
LOCAL = "ollama_chat/qwen3.8:27b"
CHAIN = [CLOUD, "openrouter/xiaomi/mimo-v2.5", LOCAL]


@pytest.fixture
def spent_cloud_key(monkeypatch):
    """Every OpenRouter credential retired, exactly as a weekly cap leaves it.

    Note what is NOT done here: no model is marked broken. That is the whole
    point — the chain walk skips a model whose pool is dead without ever
    calling ``_handle_model_error``, so ``broken_models`` stays empty while the
    run lives entirely on the local tier.
    """
    pool = key_pool.KeyPool(["sk-test-one"])
    pool.retire("sk-test-one", key_pool.Retirement.QUOTA_EXHAUSTED_PERIODIC)
    monkeypatch.setattr(key_pool, "_SHARED", {"OPENROUTER_API_KEY": pool})
    return pool


class TestTheBudgetFollowsTheModelThatWillAnswer:
    def test_a_dead_pool_moves_the_sizing_model_to_the_local_tier(self, spent_cloud_key):
        assert next_reachable_model(CHAIN, set()) == LOCAL

    def test_the_client_agrees_with_it(self, spent_cloud_key):
        """One answer to "which model", or the two paths drift apart."""
        assert LLMClient.sizing_model(CHAIN, set()) == LOCAL

    def test_a_healthy_pool_still_sizes_against_the_primary(self):
        assert next_reachable_model(CHAIN, set()) == CHAIN[0]

    def test_an_open_breaker_also_moves_it(self, monkeypatch):
        from robothor.engine import model_breaker

        breaker = model_breaker.get_model_breaker()
        monkeypatch.setattr(
            breaker, "is_open", lambda model: model.startswith("openrouter/"), raising=False
        )
        assert next_reachable_model(CHAIN, set()) == LOCAL

    def test_everything_out_falls_back_to_the_first(self, spent_cloud_key, monkeypatch):
        """Never empty: the caller needs SOME window to size against."""
        assert next_reachable_model(CHAIN, {LOCAL}) == CHAIN[0]

    def test_the_threshold_is_the_local_one_not_the_primarys(self, spent_cloud_key):
        assert (
            fit_for(next_reachable_model(CHAIN, set())).threshold
            <= get_model_limits(LOCAL).max_input_tokens // 2
        )


def _answer():
    """The shape litellm returns, with enough for the caller to unwrap."""
    message = SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1),
        model=LOCAL,
    )


def _long_conversation(turns: int) -> list[dict]:
    """~34k tokens of tool traffic — the shape of the run that died."""
    messages: list[dict] = [
        {"role": "system", "content": "You are the main agent."},
        {"role": "user", "content": "Plan tomorrow and file the tasks."},
    ]
    for i in range(turns):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call-{i}",
                        "type": "function",
                        "function": {"name": "search_records", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"call-{i}", "content": "result " * 2_000})
    messages.append({"role": "user", "content": "So what is the plan?"})
    return messages


class TestALongConversationOnTheLocalTierIsMadeToFit:
    """The property ``test_local_tier_residency`` pinned the arithmetic of.

    A number that satisfies ``threshold + output <= window`` proves nothing on
    its own: the incident's threshold satisfied it and the conversation still
    overflowed, because the threshold belonged to a different model.
    """

    async def test_the_messages_are_under_the_local_window_before_the_call(
        self, spent_cloud_key, monkeypatch
    ):
        async def _no_llm_compaction(messages, models=None, threshold=None, broken_models=None):
            # Compaction summarises with a MODEL, and on this path every cloud
            # model is unreachable. Returning the messages unchanged is the
            # worst case the deterministic shrink has to cover.
            return messages

        monkeypatch.setattr("robothor.engine.context.maybe_compress", _no_llm_compaction)
        session = SimpleNamespace(
            messages=_long_conversation(24),
            run_id="run-incident",
            thin_previous_tool_results=lambda protect_after_index=0: 0,
        )
        window = get_model_limits(LOCAL).max_input_tokens
        assert estimate_tokens(session.messages) > window, "the fixture must overflow"

        await keep_context_within_budget(
            session,
            SimpleNamespace(id="main", eager_tool_compression=False),
            iteration=14,
            models=CHAIN,
            broken_models=set(),
            hook_registry=None,
            pre_iteration_msg_idx=0,
        )

        assert estimate_tokens(session.messages) <= fit_for(LOCAL).hard_limit

    async def test_the_agent_is_told_what_was_dropped(self, spent_cloud_key, monkeypatch):
        async def _no_llm_compaction(messages, models=None, threshold=None, broken_models=None):
            return messages

        monkeypatch.setattr("robothor.engine.context.maybe_compress", _no_llm_compaction)
        session = SimpleNamespace(
            messages=_long_conversation(24),
            run_id="run-incident",
            thin_previous_tool_results=lambda protect_after_index=0: 0,
        )

        await keep_context_within_budget(
            session,
            SimpleNamespace(id="main", eager_tool_compression=False),
            iteration=14,
            models=CHAIN,
            broken_models=set(),
            hook_registry=None,
            pre_iteration_msg_idx=0,
        )

        from robothor.engine.session import ENGINE_CONTEXT_ROLE

        notes = [
            m["content"]
            for m in session.messages
            if m.get("role") == ENGINE_CONTEXT_ROLE and "context window" in str(m.get("content"))
        ]
        assert notes, "a silent drop is the failure mode being fixed, not the fix"

    async def test_the_last_user_turn_survives(self, spent_cloud_key, monkeypatch):
        """Ollama's error IS the absence of a user turn. Never ship one."""

        async def _no_llm_compaction(messages, models=None, threshold=None, broken_models=None):
            return messages

        monkeypatch.setattr("robothor.engine.context.maybe_compress", _no_llm_compaction)
        session = SimpleNamespace(
            messages=_long_conversation(40),
            run_id="run-incident",
            thin_previous_tool_results=lambda protect_after_index=0: 0,
        )

        await keep_context_within_budget(
            session,
            SimpleNamespace(id="main", eager_tool_compression=False),
            iteration=14,
            models=CHAIN,
            broken_models=set(),
            hook_registry=None,
            pre_iteration_msg_idx=0,
        )

        assert any(m.get("role") == "user" for m in session.messages)


class TestTheFirstCallOfARunIsBudgetedToo:
    """`keep_context_within_budget` returns early on iteration 0, so the loop
    does not look at the FIRST call of a run — and a run resumed from a journal
    or a long Telegram history starts with the whole conversation already in
    hand. The pre-flight every path shares is what has to catch that, and it
    was sizing with the flat estimator and never touching the ceiling: the
    measured shape ships ~110k estimated tokens at a 65,536-token model with no
    note and no drop (hostile review, M4).
    """

    async def test_it_is_brought_under_the_ceiling_before_it_is_sent(
        self, spent_cloud_key, monkeypatch
    ):
        from unittest.mock import AsyncMock, patch

        async def _no_llm_compaction(messages, models=None, threshold=None, broken_models=None):
            return messages

        monkeypatch.setattr("robothor.engine.context.maybe_compress", _no_llm_compaction)
        messages = _long_conversation(30)
        assert estimate_tokens(messages) > get_model_limits(LOCAL).max_input_tokens

        sent: list[int] = []

        async def _record(**kwargs):
            sent.append(estimate_tokens(kwargs["messages"]))
            return _answer()

        with patch(
            "robothor.engine.llm_client.litellm.acompletion", AsyncMock(side_effect=_record)
        ):
            await LLMClient()._call_llm(messages, CHAIN, [], broken_models=set())

        assert sent, "nothing was dialled"
        assert sent[0] <= fit_for(LOCAL).hard_limit, (
            f"the first call shipped {sent[0]} tokens at a "
            f"{fit_for(LOCAL).hard_limit}-token ceiling"
        )

    async def test_the_agent_is_told_what_the_pre_flight_dropped(
        self, spent_cloud_key, monkeypatch
    ):
        async def _no_llm_compaction(messages, models=None, threshold=None, broken_models=None):
            return messages

        monkeypatch.setattr("robothor.engine.context.maybe_compress", _no_llm_compaction)
        messages = _long_conversation(30)

        await LLMClient()._prepare_llm_call(messages, CHAIN, set())

        assert any(
            m.get("role") == ENGINE_CONTEXT_ROLE and "context window" in str(m.get("content"))
            for m in messages
        ), "a silent drop is the failure mode being fixed, not the fix"

    async def test_a_conversation_that_fits_is_left_alone(self, spent_cloud_key, monkeypatch):
        async def _no_llm_compaction(messages, models=None, threshold=None, broken_models=None):
            return messages

        monkeypatch.setattr("robothor.engine.context.maybe_compress", _no_llm_compaction)
        messages = [{"role": "user", "content": "hello"}]

        await LLMClient()._prepare_llm_call(messages, CHAIN, set())

        assert messages == [{"role": "user", "content": "hello"}]


# ── An overflow is not a five hundred ────────────────────────────────


OVERFLOW = 'Ollama_chatException - {"error":"no user query found in messages"}'


def _overflow_error() -> Exception:
    """Exactly what litellm raised in the incident: status 500, no clue."""
    error = Exception(OVERFLOW)
    error.status_code = 500
    return error


@pytest.fixture
def no_backoff(monkeypatch):
    """Fail the test if anything sleeps: a deterministic failure must not wait."""
    from unittest.mock import AsyncMock

    from robothor.engine import llm_client, model_breaker

    # These tests inject errors into the answering provider. Summarization
    # uses the same LiteLLM entrypoint and must not consume that injection.
    async def no_summary(messages, **kwargs):
        return messages

    monkeypatch.setattr("robothor.engine.context.maybe_compress", no_summary)
    slept = AsyncMock()
    monkeypatch.setattr(llm_client.asyncio, "sleep", slept)
    fresh = model_breaker.ModelBreaker(on_open=None)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: fresh)
    return slept


class TestAnOverflowShrinksAndRetriesOnce:
    async def test_the_same_model_is_retried_with_smaller_messages(self, no_backoff):
        from unittest.mock import AsyncMock, patch

        from robothor.engine.llm_client import LLMClient

        messages = _long_conversation(30)
        answer = object()
        sent: list[int] = []
        dialled: list[str] = []

        async def _record(**kwargs):
            # Measured AT CALL TIME: the chain hands the same list object to
            # both attempts, so anything read afterwards shows the shrink twice
            # and a broken retry would look fixed.
            sent.append(estimate_tokens(kwargs["messages"]))
            dialled.append(kwargs["model"])
            if len(sent) == 1:
                raise _overflow_error()
            return answer

        acompletion = AsyncMock(side_effect=_record)
        with patch("robothor.engine.llm_client.litellm.acompletion", acompletion):
            result = await LLMClient()._call_llm(messages, [LOCAL], [], broken_models=set())

        assert result is answer
        assert dialled == [LOCAL, LOCAL]
        assert sent[1] < sent[0], "retried the same oversized conversation"
        assert sent[1] <= fit_for(LOCAL).hard_limit
        no_backoff.assert_not_called()

    async def test_a_second_overflow_advances_instead_of_retrying_five_times(self, no_backoff):
        """The incident: five attempts, five backoffs, one guaranteed failure."""
        from unittest.mock import AsyncMock, patch

        from robothor.engine.llm_client import LLMClient

        acompletion = AsyncMock(side_effect=_overflow_error())
        with patch("robothor.engine.llm_client.litellm.acompletion", acompletion):
            result = await LLMClient()._call_llm(
                _long_conversation(30), [LOCAL], [], broken_models=set()
            )

        assert result is None
        assert acompletion.call_count <= 2, (
            "an overflow gets ONE shrink-and-retry, not a backoff loop"
        )

    async def test_the_shrink_is_visible_to_the_run(self, no_backoff):
        """The messages the session holds are the ones that fit — in place."""
        from unittest.mock import AsyncMock, patch

        from robothor.engine.llm_client import LLMClient

        messages = _long_conversation(30)
        before = estimate_tokens(messages)
        acompletion = AsyncMock(side_effect=[_overflow_error(), object()])
        with patch("robothor.engine.llm_client.litellm.acompletion", acompletion):
            await LLMClient()._call_llm(messages, [LOCAL], [], broken_models=set())

        assert estimate_tokens(messages) < before

    async def test_it_is_counted_where_the_doctor_can_see_it(self, no_backoff, monkeypatch):
        from unittest.mock import AsyncMock, patch

        from robothor.engine import context_fit
        from robothor.engine.llm_client import LLMClient

        rows: list[tuple] = []
        monkeypatch.setattr(
            context_fit,
            "log_guardrail_event",
            lambda run_id, guardrail_name, action, **kw: rows.append((guardrail_name, action)),
        )
        monkeypatch.setattr(context_fit, "_current_run_id", lambda: "run-1")
        acompletion = AsyncMock(side_effect=[_overflow_error(), object()])
        with patch("robothor.engine.llm_client.litellm.acompletion", acompletion):
            await LLMClient()._call_llm(_long_conversation(30), [LOCAL], [], broken_models=set())

        # Name AND action: `flags/evidence.py` keys on the name, and the action
        # is what tells a shrink apart from a policy warning in the same table.
        assert rows == [("context_overflow", "context_overflow")]

    def test_the_evidence_row_carries_the_before_and_after(self, monkeypatch):
        """A control's row has to say what it DID, or the table counts firings
        of something that changed nothing (hostile review, probe p17)."""
        from robothor.engine import context_fit

        rows: list[dict] = []
        monkeypatch.setattr(
            context_fit,
            "log_guardrail_event",
            lambda run_id, guardrail_name, action, **kw: rows.append(kw),
        )
        monkeypatch.setattr(context_fit, "_current_run_id", lambda: "run-1")
        messages = _long_conversation(30)

        assert context_fit.shrink_after_overflow(messages, LOCAL) is True

        reason = rows[0]["reason"]
        before, after = (int(part) for part in re.findall(r"~(\d+)", reason))
        assert after < before, reason
        assert str(estimate_tokens(messages)) in reason or after <= fit_for(LOCAL).hard_limit

    def test_it_refuses_to_retry_when_nothing_could_be_dropped(self, monkeypatch):
        """The one permitted retry is never spent on the same bytes."""
        from robothor.engine import context_fit

        monkeypatch.setattr(context_fit, "_current_run_id", lambda: None)
        # A protected head alone far larger than the window: there is nothing
        # to drop that would make this fit.
        messages = [
            {"role": "system", "content": "s" * 4_000_000},
            {"role": "user", "content": "?"},
        ]
        tiny = context_fit.ContextFit(model=LOCAL, window=200, threshold=50, reserved_output=100)
        monkeypatch.setattr(context_fit, "fit_for", lambda model: tiny)

        assert context_fit.shrink_after_overflow(messages, LOCAL) is False

    def test_the_attempt_row_says_context_overflow(self):
        """A run's own summary must not file this as a generic error_500."""
        from robothor.engine.llm_attempts import classify_error

        assert classify_error(_overflow_error()) == "context_overflow"


# ── Positive control, against the REAL local server ──────────────────


def _incident_shaped(turns: int = 8) -> list[dict]:
    """The conversation that actually failed, and nothing less will do.

    MEASURED against the real server while writing these tests, in two
    corrections:

    1. An oversized conversation that still ends on a **user** turn is
       ANSWERED. Ollama truncates from the front, the user turn survives, and
       the only evidence is a silently shortened context — no error at all.
    2. Putting tool traffic after the user turn is not enough either, as long
       as the user turn is still inside the surviving window.

    The error needs the surviving tail to hold NO user turn, which happens when
    ONE tool result is larger than the whole window. That is step 13 of the run
    that died ("a big tool_call result"), and it is the hostile case in one: a
    single step longer than a 65,536-token model's entire context.

    The size below is in REAL tokens, not the engine's estimate. Measured here:
    ``estimate_tokens`` (chars / 4) over-counts word-shaped filler by about
    1.8x, so a fixture built to the estimate fitted the window comfortably and
    the server answered it. The engine's estimate erring high is the safe
    direction — it compacts sooner than it must — but it is an estimate, which
    is why the overflow classifier and the 0.75 distrust factor exist at all.
    """
    messages = _long_conversation(turns)
    messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-tail",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        }
    )
    # ~80k REAL tokens (one word, one token): larger than the model's entire
    # 65,536-token window on its own, so nothing else survives truncation.
    messages.append({"role": "tool", "tool_call_id": "call-tail", "content": "result " * 80_000})
    messages.append({"role": ENGINE_CONTEXT_ROLE, "content": "[SYSTEM] Time budget check-in."})
    return messages


@pytest.mark.llm
@pytest.mark.timeout(900)  # a 27B dense model on the local GPU: ~34s cold
class TestAgainstTheRealLocalModel:
    """Fire the actual violation at the actual model. Skipped in CI.

    Every inert control on this instance passed its unit tests. The only thing
    that has ever found one is sending a real violation through the real path
    and watching what happens — so this test hands the local server the
    conversation that produced the incident and requires an answer.

    ``maybe_compress`` is neutralised on purpose. It summarises WITH a model,
    and on the path this change exists for every cloud model is unreachable,
    so what has to work is the deterministic shrink and the single same-model
    retry. Leaving compaction in would prove the wrong thing slowly.
    """

    async def test_the_raw_oversized_call_really_does_fail(self):
        """The negative control. Without the shrink, this IS the incident."""
        import litellm

        from robothor.engine.context_fit import is_context_overflow
        from robothor.engine.llm_client import LLMClient

        kwargs = LLMClient._build_llm_kwargs(LOCAL, _incident_shaped(), [], 10, 0.3)
        with pytest.raises(Exception) as caught:  # noqa: PT011 — litellm's class varies
            await litellm.acompletion(**kwargs)
        assert is_context_overflow(caught.value), caught.value

    async def test_an_oversized_conversation_is_answered_not_refused(self, monkeypatch):
        from unittest.mock import AsyncMock, patch

        from robothor.engine.llm_client import LLMClient

        async def _no_compaction(messages, models=None, threshold=None, broken_models=None):
            return messages

        monkeypatch.setattr("robothor.engine.context.maybe_compress", _no_compaction)
        monkeypatch.setattr("robothor.engine.llm_client.asyncio.sleep", AsyncMock())
        messages = _incident_shaped()
        assert estimate_tokens(messages) > get_model_limits(LOCAL).max_input_tokens

        with patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=10)):
            # The pre-flight is skipped so the OVERSIZED messages reach the
            # server exactly as they did in the incident.
            response = await LLMClient()._call_llm(messages, [LOCAL], [], broken_models=set())

        assert response is not None, "the local tier refused a conversation it should have fitted"
        assert estimate_tokens(messages) <= fit_for(LOCAL).hard_limit
        assert response.choices[0].message.content
