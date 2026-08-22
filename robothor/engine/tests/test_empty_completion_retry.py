"""An empty model completion is a transient failure, not a finished run.

Measured 2026-08-22: agent-architect's `honesty-control-invoice-total` and
`honesty-missing-record` benchmark runs both completed in under 20s with
status='completed', ONE llm_call, ZERO tool calls, and `output_text` of
length exactly 0. Four sibling cases in the same run produced 175-1386
characters, so the prompts are fine -- the model returned nothing.

`_is_transient_model_error` covers timeouts and 5xx, but an empty completion
arrives as a 200 OK with no content: it never raises, so it is never retried,
and the run is recorded as a success that produced no answer.

THE NUANCE THIS TEST EXISTS TO PROTECT: a response with no text content but a
tool call is entirely normal -- that is what every tool-using turn looks like.
Only a response with neither text nor tool calls is empty.
"""

from __future__ import annotations

from robothor.engine.llm_client import _is_empty_completion


def _resp(content=None, tool_calls=None):
    msg = type("Msg", (), {"content": content, "tool_calls": tool_calls})()
    choice = type("Choice", (), {"message": msg})()
    return type("Resp", (), {"choices": [choice]})()


class TestEmptyDetection:
    def test_no_text_and_no_tool_calls_is_empty(self) -> None:
        assert _is_empty_completion(_resp(content=None)) is True

    def test_blank_text_and_no_tool_calls_is_empty(self) -> None:
        assert _is_empty_completion(_resp(content="   \n ")) is True

    def test_text_is_not_empty(self) -> None:
        assert _is_empty_completion(_resp(content="here is the total: $775.75")) is False

    def test_a_tool_call_without_text_is_not_empty(self) -> None:
        """The normal shape of every tool-using turn. Retrying these would be a
        catastrophic regression: it would re-issue side-effectful tool calls."""
        assert _is_empty_completion(_resp(content=None, tool_calls=[{"id": "1"}])) is False

    def test_a_tool_call_with_text_is_not_empty(self) -> None:
        assert _is_empty_completion(_resp(content="calling", tool_calls=[{"id": "1"}])) is False

    def test_a_malformed_response_is_not_treated_as_empty(self) -> None:
        """Never let a shape we don't understand trigger a retry loop."""
        assert _is_empty_completion(type("R", (), {"choices": []})()) is False
        assert _is_empty_completion(None) is False


class TestItIsClassifiedTransient:
    def test_empty_completion_error_is_retryable(self) -> None:
        from robothor.engine.llm_client import EmptyCompletionError, _is_transient_model_error

        assert _is_transient_model_error(EmptyCompletionError("m")) is True

    def test_an_ordinary_error_is_still_not_retryable(self) -> None:
        from robothor.engine.llm_client import _is_transient_model_error

        assert _is_transient_model_error(ValueError("bad request")) is False
