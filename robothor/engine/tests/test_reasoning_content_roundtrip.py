"""A thinking-mode provider's reasoning must survive into the next request.

2026-09-11: every cloud call inside a multi-turn tool loop failed with
OpenRouter 400 "Provider returned error", whose raw provider message was:

    The reasoning_content in the thinking mode must be passed back to the API.

Single-turn calls to the same model succeeded, so nothing looked broken about
the provider — the whole fleet simply fell through to the local tier and ran
all day with ``primary_model_unreached=True``.

The cause: the runner rebuilds the assistant turn as ``{"role", "content",
"tool_calls"}`` and drops everything else the provider returned, so DeepSeek
never saw its own ``reasoning_content`` on the replayed history.

The other half is that this field belongs to the model that produced it. A
DeepSeek reasoning blob replayed to an Anthropic-shaped provider on fallback
is a different 400, so the producing model is recorded on the stored message
and the field is stripped whenever the history is replayed to anything else.
"""

from __future__ import annotations

import copy
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litellm.types.utils import (
    ChatCompletionDeltaToolCall,
    Choices,
    Delta,
    Function,
    Message,
    ModelResponse,
    ModelResponseStream,
    StreamingChoices,
    Usage,
)

from robothor.engine.llm_client import LLMClient
from robothor.engine.models import RunStatus
from robothor.engine.reasoning_replay import capture_reasoning_fields
from robothor.engine.runner import AgentRunner

DEEPSEEK = "openrouter/deepseek/deepseek-v4-flash"
#: What litellm reports back in ``response.model`` — the provider drops the
#: ``openrouter/`` prefix, so an exact string compare would never match.
DEEPSEEK_REPORTED = "deepseek/deepseek-v4-flash"
ANTHROPIC = "openrouter/anthropic/claude-opus-4.7"

REASONING = "step 1: the operator asked for tasks. step 2: call list_tasks."
REASONING_DETAILS = [{"type": "reasoning.text", "text": REASONING, "format": "unknown"}]

#: The raw provider message behind the 400, verbatim from the engine journal.
DEEPSEEK_400 = (
    "litellm.BadRequestError: OpenrouterException - Provider returned error. "
    "Raw: The reasoning_content in the thinking mode must be passed back to the API."
)


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        registry.get_tool_names.return_value = ["list_tasks"]
        registry.execute = AsyncMock(return_value={"tasks": [], "count": 0})
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


def _tool_call() -> dict:
    return {
        "id": "call_1",
        "type": "function",
        "function": {"name": "list_tasks", "arguments": json.dumps({"status": "TODO"})},
    }


def _response(*, content=None, tool_calls=None, model=DEEPSEEK_REPORTED, **extra) -> ModelResponse:
    """A real litellm ModelResponse — the shape the provider actually returns."""
    message = Message(content=content, role="assistant", tool_calls=tool_calls, **extra)
    return ModelResponse(
        id="resp-1",
        choices=[
            Choices(
                index=0,
                message=message,
                finish_reason="tool_calls" if tool_calls else "stop",
            )
        ],
        model=model,
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _assistant_turn(messages: list[dict]) -> dict:
    """The replayed assistant turn that carried the tool call."""
    turns = [m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")]
    assert turns, f"no assistant tool-call turn in replayed history: {messages}"
    return turns[-1]


async def _two_turn_run(runner, agent_config, completion) -> tuple[object, list[dict]]:
    calls: list[dict] = []

    async def _record(**kwargs):
        # Snapshot: the list handed to litellm can be the session's own, which
        # the loop keeps appending to after the call returns.
        calls.append({**kwargs, "messages": copy.deepcopy(kwargs["messages"])})
        return await completion(**kwargs)

    with (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=_record),
    ):
        run = await runner.execute("test-agent", "List my tasks", agent_config=agent_config)
    return run, calls


class TestNonStreamingRoundTrip:
    @pytest.mark.asyncio
    async def test_second_request_echoes_reasoning_content(
        self, runner, sample_agent_config
    ) -> None:
        """The turn that asked for a tool must carry its reasoning back."""
        sample_agent_config.model_primary = DEEPSEEK
        sample_agent_config.model_fallbacks = []

        responses = [
            _response(
                tool_calls=[_tool_call()],
                reasoning_content=REASONING,
                reasoning_details=REASONING_DETAILS,
            ),
            _response(content="Found 3 tasks."),
        ]

        async def completion(**kwargs):
            return responses.pop(0)

        run, calls = await _two_turn_run(runner, sample_agent_config, completion)

        assert run.status == RunStatus.COMPLETED
        assert len(calls) == 2, "expected a second turn after the tool result"
        turn = _assistant_turn(calls[1]["messages"])
        assert turn.get("reasoning_content") == REASONING
        assert turn.get("reasoning_details") == REASONING_DETAILS

    @pytest.mark.asyncio
    async def test_producer_bookkeeping_never_reaches_the_provider(
        self, runner, sample_agent_config
    ) -> None:
        """``_model`` is engine bookkeeping — the API must never see it."""
        sample_agent_config.model_primary = DEEPSEEK
        sample_agent_config.model_fallbacks = []

        responses = [
            _response(tool_calls=[_tool_call()], reasoning_content=REASONING),
            _response(content="Found 3 tasks."),
        ]

        async def completion(**kwargs):
            return responses.pop(0)

        _run, calls = await _two_turn_run(runner, sample_agent_config, completion)

        for call in calls:
            for message in call["messages"]:
                assert "_model" not in message, f"engine bookkeeping leaked: {message}"


class TestFallbackToADifferentModel:
    """DeepSeek's field is a 400 at an Anthropic-shaped provider."""

    @staticmethod
    def _history() -> list[dict]:
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call()],
                "reasoning_content": REASONING,
                "reasoning_details": REASONING_DETAILS,
                "_model": DEEPSEEK_REPORTED,
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
            {"role": "user", "content": "go on"},
        ]

    def test_reasoning_is_stripped_for_another_model(self) -> None:
        kwargs = LLMClient._build_llm_kwargs(ANTHROPIC, self._history(), [], 100, 0.3)
        turn = _assistant_turn(kwargs["messages"])
        assert "reasoning_content" not in turn
        assert "reasoning_details" not in turn
        assert "_model" not in turn

    def test_reasoning_survives_for_the_same_model(self) -> None:
        """Message hygiene must not be what breaks the round trip."""
        kwargs = LLMClient._build_llm_kwargs(DEEPSEEK, self._history(), [], 100, 0.3)
        turn = _assistant_turn(kwargs["messages"])
        assert turn["reasoning_content"] == REASONING
        assert turn["reasoning_details"] == REASONING_DETAILS
        assert "_model" not in turn

    def test_the_stored_history_is_not_mutated(self) -> None:
        """Stripping for one leg of the chain must not lose it for the next."""
        history = self._history()
        LLMClient._build_llm_kwargs(ANTHROPIC, history, [], 100, 0.3)
        assert history[2]["reasoning_content"] == REASONING
        assert history[2]["_model"] == DEEPSEEK_REPORTED


class TestStreaming:
    @pytest.mark.asyncio
    async def test_streamed_reasoning_is_accumulated_and_replayed(
        self, runner, sample_agent_config
    ) -> None:
        """The interactive path assembles its turn from deltas."""
        sample_agent_config.model_primary = DEEPSEEK
        sample_agent_config.model_fallbacks = []

        def _chunk(**delta_kwargs) -> ModelResponseStream:
            return ModelResponseStream(
                id="resp-1",
                created=1,
                model=DEEPSEEK_REPORTED,
                choices=[
                    StreamingChoices(index=0, delta=Delta(**delta_kwargs), finish_reason=None)
                ],
            )

        def _stream(chunks):
            class _S:
                def __aiter__(self):
                    async def gen():
                        for chunk in chunks:
                            yield chunk

                    return gen()

            return _S()

        turn1 = [
            _chunk(content=None, reasoning_content="step 1: "),
            _chunk(
                content=None,
                reasoning_content="call list_tasks.",
                tool_calls=[
                    ChatCompletionDeltaToolCall(
                        id="call_1",
                        index=0,
                        type="function",
                        function=Function(name="list_tasks", arguments="{}"),
                    )
                ],
            ),
        ]
        turn2 = [_chunk(content="Found 3 tasks.")]
        streams = [_stream(turn1), _stream(turn2)]

        async def completion(**kwargs):
            return streams.pop(0)

        async def on_content(_text: str) -> None:
            return None

        calls: list[dict] = []

        async def _record(**kwargs):
            calls.append({**kwargs, "messages": copy.deepcopy(kwargs["messages"])})
            return await completion(**kwargs)

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.run_finalizer.create_step"),
            patch("litellm.acompletion", side_effect=_record),
        ):
            run = await runner.execute(
                "test-agent",
                "List my tasks",
                agent_config=sample_agent_config,
                on_content=on_content,
            )

        assert run.status == RunStatus.COMPLETED
        assert len(calls) == 2, "expected a second streamed turn after the tool result"
        turn = _assistant_turn(calls[1]["messages"])
        assert turn.get("reasoning_content") == "step 1: call list_tasks."


class TestTheRejectionIsNamed:
    """This failure mode must never be silent again."""

    def test_the_deepseek_400_is_logged_with_its_cause(self, caplog) -> None:
        error = Exception(DEEPSEEK_400)
        error.status_code = 400  # type: ignore[attr-defined]

        with caplog.at_level(logging.ERROR, logger="robothor.engine.llm_client"):
            LLMClient._handle_model_error(error, DEEPSEEK, set())

        named = [r for r in caplog.records if "reasoning_replay_rejected=True" in r.getMessage()]
        assert named, f"the reasoning-replay rejection was not named: {caplog.text}"
        assert named[0].levelno >= logging.ERROR

    def test_an_unrelated_400_is_not_blamed_on_reasoning(self, caplog) -> None:
        error = Exception("litellm.BadRequestError: context length exceeded")
        error.status_code = 400  # type: ignore[attr-defined]

        with caplog.at_level(logging.WARNING, logger="robothor.engine.llm_client"):
            LLMClient._handle_model_error(error, DEEPSEEK, set())

        assert "reasoning_replay_rejected" not in caplog.text


class TestCaptureMatchesLitellmsRealShape:
    """Probed against litellm 1.97.0, not assumed.

    OpenRouter returns ``reasoning``; litellm normalizes it onto
    ``message.reasoning_content`` and leaves the original under
    ``provider_specific_fields``. Reading both blindly ships the same blob
    twice — paid for twice, on every turn of every run.
    """

    def test_the_normalized_field_is_not_also_sent_as_the_raw_one(self) -> None:
        message = Message(
            content="hi",
            role="assistant",
            reasoning_content="R-BLOB",
            provider_specific_fields={"reasoning": "R-BLOB"},
        )

        captured = capture_reasoning_fields(message)

        assert captured == {"reasoning_content": "R-BLOB"}

    def test_a_genuinely_different_raw_field_is_kept(self) -> None:
        message = Message(
            content="hi",
            role="assistant",
            reasoning_content="normalized",
            provider_specific_fields={"reasoning_details": [{"type": "reasoning.text"}]},
        )

        captured = capture_reasoning_fields(message)

        assert captured == {
            "reasoning_content": "normalized",
            "reasoning_details": [{"type": "reasoning.text"}],
        }


class TestContextAccounting:
    """Reasoning we replay is prompt the model is billed for."""

    def test_replayed_reasoning_counts_towards_the_context_estimate(self) -> None:
        """An uncounted 4k blob per turn is a window the engine overruns blind."""
        from robothor.engine.context import estimate_tokens

        plain = [{"role": "assistant", "content": "ok"}]
        with_reasoning = [{"role": "assistant", "content": "ok", "reasoning_content": "x" * 4000}]

        assert estimate_tokens(with_reasoning) - estimate_tokens(plain) == pytest.approx(
            1000, rel=0.05
        )
