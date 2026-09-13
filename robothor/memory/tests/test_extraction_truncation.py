"""A truncated extraction is a failure, not an empty result.

`_openrouter_chat` reads `choices[0].message.content` and never looks at
`finish_reason`. When the model runs out of budget mid-JSON it returns a long,
unparseable string, which the caller scores as "no facts in this conversation".

Measured on production over 7 days:

    122 extractions attempted
     72 parsed ZERO facts   (59%)
     21 abandoned after 3 attempts

The response sizes prove truncation rather than genuinely empty conversations —
the two populations barely overlap, and every zero-fact response is piled
against the ceiling:

    zero facts parsed : min 2654  median 3407  max 3863 chars
    facts parsed      : min  491  median 1554  max 3394 chars

`max_tokens=1024` is roughly 3.5-4k characters. The failures are not short.

This is the same defect as the starved benchmark judge (#335): a budget too
small for the work, and the resulting silence recorded as the content's fault
rather than the budget's. The remedy is the same — enough room to answer, and a
truncation treated as a retryable failure instead of an answer.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from robothor.memory import facts as facts_mod
from robothor.memory import generation


class TestTokenBudget:
    def test_extraction_budget_is_not_starved(self) -> None:
        """1024 truncated 59% of real extractions."""
        import inspect

        src = inspect.getsource(facts_mod)
        assert "max_tokens=1024" not in src, (
            "the extraction budget that truncated 59% of production extractions is back"
        )

    def test_the_budget_is_a_named_constant(self) -> None:
        """A bare literal is how this sat unexamined; name it so it is reviewable."""
        assert hasattr(generation, "EXTRACTION_MAX_TOKENS")
        assert generation.EXTRACTION_MAX_TOKENS >= 4096


class TestTruncationIsAFailure:
    def test_length_finish_reason_raises(self) -> None:
        """A truncated response must not be mistaken for a real answer."""
        payload = {
            "choices": [
                {
                    "message": {"content": '{"facts": [{"fact": "half a fa'},
                    "finish_reason": "length",
                }
            ]
        }
        with pytest.raises(RuntimeError, match="truncat"):
            generation.content_from_response(payload, model="test/model", max_tokens=4096)

    def test_a_complete_response_passes_through(self) -> None:
        payload = {"choices": [{"message": {"content": '{"facts": []}'}, "finish_reason": "stop"}]}
        assert (
            generation.content_from_response(payload, model="test/model", max_tokens=4096)
            == '{"facts": []}'
        )

    def test_empty_content_still_raises(self) -> None:
        """The pre-existing contract holds."""
        payload = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
        with pytest.raises(RuntimeError, match="empty content"):
            generation.content_from_response(payload, model="test/model", max_tokens=4096)

    def test_a_missing_finish_reason_is_not_treated_as_truncation(self) -> None:
        """Providers that omit the field must not be failed on suspicion."""
        payload = {"choices": [{"message": {"content": "ok"}}]}
        assert (
            generation.content_from_response(payload, model="test/model", max_tokens=4096) == "ok"
        )

    def test_the_error_names_the_budget_so_it_is_actionable(self) -> None:
        payload = {"choices": [{"message": {"content": "x" * 3400}, "finish_reason": "length"}]}
        with pytest.raises(RuntimeError) as exc:
            generation.content_from_response(payload, model="test/model", max_tokens=4096)
        assert "max_tokens" in str(exc.value)


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Captures the posted OpenRouter payload; returns a canned completion."""

    response_payload: dict = {}
    last_post: dict = {}

    def __init__(self, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def post(self, url: str, json: dict | None = None, headers: dict | None = None):
        type(self).last_post = {"url": url, "json": json, "headers": headers}
        return _FakeResponse(type(self).response_payload)


def _a_small_budget_caller(payload: dict) -> str:
    """Stands in for lifecycle.judge_importance — a 64-token memory caller."""
    return generation.content_from_response(payload, model="test/model", max_tokens=64)


class TestTruncationNamesTheFailingCaller:
    """The message must send the reader to the file that actually ran out.

    27 of 30 production truncations over 8 days were small-budget callers
    (judge_importance at 64, preferences at 150, consolidation at 256) — and
    every one of them reported "raise max_tokens (currently 4096 for
    extraction)", the budget of a caller that had not run. One wrong constant
    sent every investigation to facts.py.
    """

    TRUNCATED = {"choices": [{"message": {"content": "x" * 300}, "finish_reason": "length"}]}

    def test_the_error_names_the_callers_budget(self) -> None:
        with pytest.raises(RuntimeError) as exc:
            generation.content_from_response(self.TRUNCATED, model="test/model", max_tokens=64)
        assert "64" in str(exc.value)

    def test_the_error_does_not_name_another_callers_budget(self) -> None:
        with pytest.raises(RuntimeError) as exc:
            generation.content_from_response(self.TRUNCATED, model="test/model", max_tokens=64)
        assert str(generation.EXTRACTION_MAX_TOKENS) not in str(exc.value)

    def test_the_error_names_the_caller(self) -> None:
        with pytest.raises(RuntimeError) as exc:
            _a_small_budget_caller(self.TRUNCATED)
        assert "_a_small_budget_caller" in str(exc.value)

    def test_extraction_still_reports_its_own_budget(self) -> None:
        with pytest.raises(RuntimeError) as exc:
            generation.content_from_response(
                self.TRUNCATED, model="test/model", max_tokens=generation.EXTRACTION_MAX_TOKENS
            )
        assert str(generation.EXTRACTION_MAX_TOKENS) in str(exc.value)


class TestNoThinkReasoningMargin:
    """`think=False` is a request to the model, not a guarantee.

    mimo-v2.5 is a reasoning model; when it does not fully honour
    `reasoning: {enabled: false}` the reasoning is still charged against
    max_tokens, so a 64-token ceiling is spent before the answer starts —
    finish_reason=length with 0-389 content chars, which is exactly the
    production population (27 of 30 truncations over 8 days). A flat margin
    covers every observed case.
    """

    @staticmethod
    def _install(monkeypatch, payload: dict) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        _FakeAsyncClient.response_payload = payload
        monkeypatch.setattr(generation.httpx, "AsyncClient", _FakeAsyncClient)

    async def test_a_nothink_call_asks_the_remote_for_the_margin(self, monkeypatch) -> None:
        self._install(monkeypatch, {"choices": [{"message": {"content": "0.9"}}]})

        await generation._openrouter_chat(
            [{"role": "user", "content": "rate"}],
            temperature=0.2,
            max_tokens=64,
            format=None,
            think=False,
        )

        payload = _FakeAsyncClient.last_post["json"]
        assert payload["max_tokens"] == 64 + generation.REMOTE_NOTHINK_MARGIN
        assert payload["reasoning"] == {"enabled": False}

    async def test_the_margin_buys_reasoning_room_not_a_bigger_answer(self, monkeypatch) -> None:
        """The caller asked for 64 tokens of answer and gets at most that.

        The extra budget is spent on the reasoning the model emits anyway; it
        is stripped, so the content handed back stays inside the caller's own
        budget.
        """
        reasoning = "<think>" + ("deliberating " * 400) + "</think>"
        self._install(
            monkeypatch,
            {"choices": [{"message": {"content": reasoning + "0.9"}, "finish_reason": "stop"}]},
        )

        result = await generation._openrouter_chat(
            [{"role": "user", "content": "rate"}],
            temperature=0.2,
            max_tokens=64,
            format=None,
            think=False,
        )

        assert result == "0.9"
        # ~4 chars per token: the answer never exceeds the caller's budget.
        assert len(result) <= 64 * 4

    async def test_think_true_keeps_the_full_thinking_overhead(self, monkeypatch) -> None:
        self._install(monkeypatch, {"choices": [{"message": {"content": "0.9"}}]})

        await generation._openrouter_chat(
            [{"role": "user", "content": "rate"}],
            temperature=0.2,
            max_tokens=64,
            format=None,
            think=True,
        )

        payload = _FakeAsyncClient.last_post["json"]
        assert payload["max_tokens"] == 64 + generation.REMOTE_THINKING_OVERHEAD

    async def test_extractions_content_budget_is_unchanged_and_its_request_grows(
        self, monkeypatch
    ) -> None:
        """Extraction passes think=False, so its wire request moves too.

        The constant — the budget extraction asks for its *answer* — is
        untouched at 4096; the request carries the same margin every
        think=False caller now gets, which is 4608 on the wire. Population 2
        (genuine extraction overflow at 4096) is not what this PR fixes, and
        the margin is not a fix for it either.
        """
        assert generation.EXTRACTION_MAX_TOKENS == 4096
        self._install(monkeypatch, {"choices": [{"message": {"content": "[]"}}]})

        await generation._openrouter_chat(
            [{"role": "user", "content": "extract"}],
            temperature=0.2,
            max_tokens=generation.EXTRACTION_MAX_TOKENS,
            format=None,
            think=False,
        )

        payload = _FakeAsyncClient.last_post["json"]
        assert payload["max_tokens"] == 4096 + generation.REMOTE_NOTHINK_MARGIN


class TestAnOverBudgetAnswerIsVisible:
    """The margin is bought on the wire and cannot be enforced on the answer.

    `strip_think_blocks` removes inline reasoning, but a model that emits no
    think block (or returns its reasoning in OpenRouter's separate field) can
    return up to `max_tokens + REMOTE_NOTHINK_MARGIN` tokens of *content*.
    Silently slicing that back would contradict this module's own rule — a
    body cut mid-JSON is not an answer, which is why `finish_reason=length`
    is refused rather than parsed — so the overflow is reported instead of
    hidden, and the caller still gets the whole answer.

    The one caller with real exposure is `lifecycle._consolidate_group`
    (256 tokens, free text, written straight into `consolidated_text`).
    """

    @staticmethod
    def _payload(chars: int) -> dict:
        return {"choices": [{"message": {"content": "x" * chars}, "finish_reason": "stop"}]}

    def test_an_over_budget_answer_is_logged(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="robothor.memory.generation"):
            content = generation.content_from_response(
                self._payload(2000), model="test/model", max_tokens=64
            )

        assert len(content) == 2000, "the answer must not be silently truncated"
        assert any(
            generation.ANSWER_OVER_BUDGET_MARKER in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_the_overflow_line_names_the_caller_and_the_budget(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="robothor.memory.generation"):
            generation.content_from_response(self._payload(2000), model="test/model", max_tokens=64)

        line = next(
            r.getMessage()
            for r in caplog.records
            if generation.ANSWER_OVER_BUDGET_MARKER in r.getMessage()
        )
        assert "64" in line
        assert "test_the_overflow_line_names_the_caller_and_the_budget" in line

    def test_an_answer_inside_the_budget_says_nothing(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="robothor.memory.generation"):
            generation.content_from_response(self._payload(40), model="test/model", max_tokens=64)

        assert not [
            r for r in caplog.records if generation.ANSWER_OVER_BUDGET_MARKER in r.getMessage()
        ]


def _label_from_an_asyncio_frame() -> str:
    """Call `_memory_caller()` from a frame that belongs to the event loop.

    This is what Python 3.11 produces for `lifecycle.judge_importance` — the
    flagship 64-token caller — because `asyncio.wait_for` wraps the coroutine
    in `ensure_future` there, so the frame chain above the generation is the
    loop, not the caller.
    """
    namespace: dict = {"__name__": "asyncio.tasks", "generation": generation}
    exec("def __step():\n    return generation._memory_caller()\n", namespace)  # noqa: S102
    return str(namespace["__step"]())


async def _label_from_a_memory_caller() -> str:
    return generation._memory_caller()


class TestTheCallerLabelIsNeverConfidentlyWrong:
    """A wrong name is worse than no name.

    The label exists so the operator opens the right file. Walking *past* the
    event loop to whatever called `asyncio.run` produces a plausible, wrong
    answer — the same failure mode as the 4096 constant this PR removed.
    """

    def test_an_event_loop_frame_is_not_named_as_the_caller(self) -> None:
        assert _label_from_an_asyncio_frame() == "a caller"

    async def test_a_retasked_generation_never_names_the_wrong_caller(self) -> None:
        """Runs on every interpreter in the CI matrix: on 3.12+ the chain
        survives `wait_for` and the caller is named; on 3.11 it does not and
        the label degrades. Either is fine — a third answer is not."""
        label = await asyncio.wait_for(_label_from_a_memory_caller(), timeout=5)
        assert label.endswith("._label_from_a_memory_caller") or label == "a caller", label

    def test_a_frame_without_a_module_name_is_skipped(self) -> None:
        """`f_globals` without `__name__` used to yield a bare '.<func>'."""
        namespace: dict = {"generation": generation}
        exec("def anonymous():\n    return generation._memory_caller()\n", namespace)  # noqa: S102
        assert not str(namespace["anonymous"]()).startswith(".")
