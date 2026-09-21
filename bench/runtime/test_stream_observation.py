from unittest.mock import AsyncMock

import pytest

from bench.runtime.stream_observation import ObservedStream


@pytest.mark.parametrize("fails", [False, True])
async def test_records_consumption_failure_or_completion_and_closes(fails):
    async def values():
        yield "first"
        if fails:
            raise OSError("lost stream")

    source = values()
    call = {}
    observed = ObservedStream(source, call, 0)
    assert await anext(observed) == "first"
    with pytest.raises(OSError if fails else StopAsyncIteration):
        await anext(observed)
    assert call["stream_completed"] is not fails
    assert call.get("stream_error_type") == ("OSError" if fails else None)
    assert call["stream_elapsed_ms"] > 0
    observed.stream = type("Closing", (), {"aclose": AsyncMock()})()
    await observed.aclose()
    observed.stream.aclose.assert_awaited_once()


def test_chunk_telemetry_separates_reasoning_answer_and_reported_usage():
    from types import SimpleNamespace

    from bench.runtime.stream_observation import observe_chunk

    call = {}
    observe_chunk({"choices": [{"delta": {"role": "assistant"}}]}, call, 1)
    assert call == {}
    observe_chunk({"choices": [{"delta": {"reasoning_content": "private"}}]}, call, 5)
    observe_chunk(
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=["private arguments"]))]
        ),
        call,
        10,
    )
    observe_chunk({"choices": [{"delta": {"content": "private answer"}}]}, call, 20)
    observe_chunk(
        {
            "usage": {
                "prompt_tokens": 30,
                "completion_tokens": 40,
                "total_tokens": 70,
                "completion_tokens_details": {"reasoning_tokens": 25},
            }
        },
        call,
        25,
    )
    assert call == {
        "first_reasoning_ms": 5,
        "first_answer_ms": 10,
        "reported_usage": {
            "prompt_tokens": 30,
            "completion_tokens": 40,
            "total_tokens": 70,
            "reasoning_tokens": 25,
        },
    }
    assert "private" not in str(call)
