"""Observe streamed provider completion separately from connection creation."""

import time


class ObservedStream:
    def __init__(self, stream, call, started):
        self.stream = stream
        self.iterator = stream.__aiter__()
        self.call = call
        self.started = started
        call["stream_completed"] = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.iterator.__anext__()
        except StopAsyncIteration:
            self.call["stream_completed"] = True
            raise
        except BaseException as exc:
            self.call["stream_error_type"] = type(exc).__name__
            raise
        finally:
            self.call["stream_elapsed_ms"] = (time.perf_counter() - self.started) * 1000

    async def aclose(self):
        close = getattr(self.stream, "aclose", None)
        if close is not None:
            await close()
