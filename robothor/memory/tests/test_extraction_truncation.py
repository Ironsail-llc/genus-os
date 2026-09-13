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

    async def test_the_extraction_budget_itself_is_unchanged(self, monkeypatch) -> None:
        """Population 2 (genuine extraction overflow) is not what this fixes."""
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
