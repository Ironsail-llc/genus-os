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
            generation.content_from_response(payload, model="test/model")

    def test_a_complete_response_passes_through(self) -> None:
        payload = {"choices": [{"message": {"content": '{"facts": []}'}, "finish_reason": "stop"}]}
        assert generation.content_from_response(payload, model="test/model") == '{"facts": []}'

    def test_empty_content_still_raises(self) -> None:
        """The pre-existing contract holds."""
        payload = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
        with pytest.raises(RuntimeError, match="empty content"):
            generation.content_from_response(payload, model="test/model")

    def test_a_missing_finish_reason_is_not_treated_as_truncation(self) -> None:
        """Providers that omit the field must not be failed on suspicion."""
        payload = {"choices": [{"message": {"content": "ok"}}]}
        assert generation.content_from_response(payload, model="test/model") == "ok"

    def test_the_error_names_the_budget_so_it_is_actionable(self) -> None:
        payload = {"choices": [{"message": {"content": "x" * 3400}, "finish_reason": "length"}]}
        with pytest.raises(RuntimeError) as exc:
            generation.content_from_response(payload, model="test/model")
        assert "max_tokens" in str(exc.value)
