"""The context budget belongs to the model that will actually be called.

2026-09-16, production run on the main agent, trigger telegram. The cloud
provider's key had hit its weekly cap, so from step 1 the run was already
answering on the last fallback, a local Ollama model with a 65,536-token
window. Step 14 failed five times with

    Ollama_chatException - {"error":"no user query found in messages"}

which is what Ollama answers when the conversation is longer than ``num_ctx``:
it truncates from the front to make it fit and, when the tail alone overflows,
no user turn survives the truncation. Each failure was classified as a
transient 500, backed off, retried on the same oversized messages, and the run
ended on ``RuntimeError("All models failed to respond")`` — which the operator
read in his chat.

The compaction threshold was derived from ``sizing_model``, which consults
``broken_models`` only. A model skipped because its credential pool is
exhausted is never IN ``broken_models`` (``_call_llm`` breaks out of the spent
branch without calling ``_handle_model_error``), so a run living entirely on
the local tier still sized its context against the 1M-token primary. The
registry comment promising "proactive compaction fires at min(0.5 x
max_input_tokens, 80_000)" was true of a number nobody was measuring against
the model being dialled.

So: one module that answers "what can the NEXT call hold, and how do we make
these messages fit it", and a positive control that the answer changes when
the credential pool dies rather than only when a model errors.
"""

from __future__ import annotations

import pytest

from robothor.engine.context_fit import (
    ContextFit,
    fit_for,
    is_context_overflow,
    shrink_to_fit,
)
from robothor.engine.session import ENGINE_CONTEXT_ROLE

LOCAL = "ollama_chat/qwen3.8:27b"
CLOUD = "openrouter/xiaomi/mimo-v2.5"


class _BoomError(Exception):
    """A provider error carrying only a message, like litellm's."""


class TestOverflowIsNotAFiveHundred:
    """Every shape of "your conversation does not fit" the fleet can see."""

    @pytest.mark.parametrize(
        "message",
        [
            # Ollama, reproduced against the real server (probe_ollama_ctx.py).
            'Ollama_chatException - {"error":"no user query found in messages"}',
            # OpenAI-shaped.
            "This model's maximum context length is 8192 tokens, however you "
            "requested 10000 tokens. Please reduce the length of the messages.",
            # Anthropic-shaped.
            "prompt is too long: 215000 tokens > 200000 maximum",
            # Generic provider phrasing.
            "context length exceeded",
            "input length and `max_tokens` exceed context limit",
        ],
    )
    def test_the_known_shapes_are_overflow(self, message):
        assert is_context_overflow(_BoomError(message))

    def test_litellms_own_class_counts_whatever_it_says(self):
        """Classified by TYPE, so a future wording change cannot un-classify it."""

        class ContextWindowExceededError(Exception):
            pass

        assert is_context_overflow(ContextWindowExceededError("nothing useful here"))

    @pytest.mark.parametrize(
        "message",
        [
            # The hostile case: prose that merely mentions the word.
            "Internal server error while building the context for your request",
            "the model is overloaded, please retry",
            "connection reset by peer",
            "context switch failed on the worker",
        ],
    )
    def test_prose_mentioning_context_is_not_overflow(self, message):
        assert not is_context_overflow(_BoomError(message))

    def test_none_and_empty_are_not_overflow(self):
        assert not is_context_overflow(_BoomError(""))


class TestTheBudgetFollowsTheModel:
    def test_the_local_window_is_the_local_models(self):
        fit = fit_for(LOCAL)
        assert fit.window == 65_536
        assert fit.threshold <= fit.window

    def test_a_big_cloud_window_gets_a_bigger_budget(self):
        assert fit_for(CLOUD).window > fit_for(LOCAL).window

    def test_the_hard_limit_leaves_room_for_the_answer(self):
        """The invariant the incident violated: input + output <= window."""
        fit = fit_for(LOCAL)
        assert fit.hard_limit + fit.reserved_output <= fit.window
        assert fit.threshold < fit.hard_limit


def _msgs(tool_result_chars: int, count: int) -> list[dict]:
    """A conversation shaped like the one that failed: head, user, tool tail."""
    messages: list[dict] = [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "Do the task."},
    ]
    for i in range(count):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append(
            {"role": "tool", "tool_call_id": f"c{i}", "content": "x" * tool_result_chars}
        )
    messages.append({"role": "user", "content": "What did you find?"})
    messages.append({"role": ENGINE_CONTEXT_ROLE, "content": "[SYSTEM] budget check-in."})
    return messages


class TestShrinkingToFit:
    def test_it_drops_tool_results_oldest_first(self):
        fit = ContextFit(model=LOCAL, window=2_000, threshold=500, reserved_output=500)
        outcome = shrink_to_fit(_msgs(4_000, 6), fit)
        kept = [m for m in outcome.messages if m.get("role") == "tool"]
        assert outcome.dropped > 0
        # The newest tool result survives longest.
        assert len(kept[-1]["content"]) >= len(kept[0]["content"])

    def test_it_keeps_the_protected_head_and_the_last_user_turn(self):
        fit = ContextFit(model=LOCAL, window=1_200, threshold=300, reserved_output=400)
        outcome = shrink_to_fit(_msgs(8_000, 8), fit)
        assert outcome.messages[0]["content"] == "You are terse."
        assert any(
            m.get("role") == "user" and m.get("content") == "What did you find?"
            for m in outcome.messages
        ), "the turn the model has to answer must survive"

    def test_it_fits_even_one_tool_result_larger_than_the_window(self):
        """The hostile case: 80k tokens arriving in a single step."""
        fit = ContextFit(model=LOCAL, window=4_000, threshold=1_000, reserved_output=1_000)
        outcome = shrink_to_fit(_msgs(320_000, 1), fit)
        assert outcome.tokens_after <= fit.hard_limit

    def test_it_fits_a_two_hundred_thousand_token_conversation(self):
        fit = ContextFit(model=LOCAL, window=65_536, threshold=32_768, reserved_output=8_192)
        outcome = shrink_to_fit(_msgs(4_000, 200), fit)
        assert outcome.tokens_after <= fit.hard_limit

    def test_every_tool_message_keeps_its_call(self):
        """A dropped result must stay a result: providers reject a dangling call."""
        fit = ContextFit(model=LOCAL, window=1_200, threshold=300, reserved_output=400)
        outcome = shrink_to_fit(_msgs(8_000, 8), fit)
        call_ids = {tc["id"] for m in outcome.messages for tc in (m.get("tool_calls") or [])}
        result_ids = {m["tool_call_id"] for m in outcome.messages if m.get("role") == "tool"}
        assert result_ids <= call_ids

    def test_a_conversation_that_already_fits_is_untouched(self):
        fit = ContextFit(model=LOCAL, window=65_536, threshold=32_768, reserved_output=8_192)
        before = _msgs(100, 2)
        outcome = shrink_to_fit(before, fit)
        assert outcome.dropped == 0
        assert outcome.messages == before
        assert outcome.note is None

    def test_it_says_so_in_a_developer_note(self):
        fit = ContextFit(model=LOCAL, window=1_200, threshold=300, reserved_output=400)
        outcome = shrink_to_fit(_msgs(8_000, 8), fit)
        assert outcome.note is not None
        assert LOCAL in outcome.note
        assert "tool result" in outcome.note
