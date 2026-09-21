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
