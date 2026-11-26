"""An embedding the server returns must be a usable vector before anyone stores it.

Under load the local embedding server can answer 200 with an empty vector. The
client used to hand that straight back, and the first sign of trouble was
pgvector refusing the SQL (``different vector dimensions 1024 and 0``) deep in
a write or a search. Every ``embedding`` column is ``vector(1024)``, so the
client checks each vector it gets: not empty, 1024 wide. A bad one is retried
once; a second bad one raises an error that names the model and both widths.
The error is an ``httpx.HTTPError``, so the search paths that already degrade
when the embedding service is unavailable degrade the same way here.
"""

from __future__ import annotations

import httpx
import pytest

from robothor.llm import ollama

WIDTH = 1024
GOOD = [0.01] * WIDTH
MODEL = "test-embedder"


@pytest.fixture(autouse=True)
def _seams(monkeypatch):
    """A fixed model name, no real sleeping, and the transport seam reset after."""
    monkeypatch.setenv("ROBOTHOR_EMBEDDING_MODEL", MODEL)
    monkeypatch.setenv("ROBOTHOR_OLLAMA_URL", "http://embedder.test")

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(ollama.asyncio, "sleep", no_sleep)
    yield
    ollama._transport = None


def _serve(*bodies: dict) -> dict[str, int]:
    """Answer each embed call with the next body (the last one repeats)."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = bodies[min(calls["n"], len(bodies) - 1)]
        calls["n"] += 1
        return httpx.Response(200, json=body)

    ollama._transport = httpx.MockTransport(handler)
    return calls


class TestSingleEmbedding:
    async def test_good_vector_is_returned_after_one_call(self):
        calls = _serve({"embeddings": [GOOD]})

        assert await ollama.get_embedding_async("text") == GOOD
        assert calls["n"] == 1

    async def test_empty_vector_is_retried_once_then_succeeds(self):
        calls = _serve({"embeddings": [[]]}, {"embeddings": [GOOD]})

        assert await ollama.get_embedding_async("text") == GOOD
        assert calls["n"] == 2

    async def test_empty_vector_twice_raises_naming_model_and_widths(self):
        calls = _serve({"embeddings": [[]]})

        with pytest.raises(httpx.HTTPError) as caught:
            await ollama.get_embedding_async("text")

        message = str(caught.value)
        assert MODEL in message
        assert "0" in message and str(WIDTH) in message
        assert calls["n"] == 2

    async def test_wrong_width_twice_raises_naming_got_and_expected(self):
        calls = _serve({"embeddings": [[0.5] * 512]})

        with pytest.raises(httpx.HTTPError) as caught:
            await ollama.get_embedding_async("text")

        message = str(caught.value)
        assert MODEL in message
        assert "512" in message and str(WIDTH) in message
        assert calls["n"] == 2

    async def test_no_vector_at_all_is_the_same_clear_error(self):
        """``{"embeddings": []}`` used to surface as a bare IndexError."""
        _serve({"embeddings": []})

        with pytest.raises(httpx.HTTPError, match=MODEL):
            await ollama.get_embedding_async("text")

    async def test_error_is_its_own_named_type(self):
        _serve({"embeddings": [[]]})

        with pytest.raises(httpx.HTTPError) as caught:
            await ollama.get_embedding_async("text")

        assert type(caught.value).__name__ == "InvalidEmbeddingError"

    async def test_explicit_model_is_the_one_named(self):
        _serve({"embeddings": [[]]})

        with pytest.raises(httpx.HTTPError, match="other-embedder"):
            await ollama.get_embedding_async("text", model="other-embedder")


class TestBatchEmbedding:
    async def test_good_batch_is_returned_after_one_call(self):
        calls = _serve({"embeddings": [GOOD, GOOD]})

        assert await ollama.get_embeddings_batch_async(["a", "b"]) == [GOOD, GOOD]
        assert calls["n"] == 1

    async def test_one_empty_vector_retries_the_batch_once_then_succeeds(self):
        calls = _serve({"embeddings": [GOOD, []]}, {"embeddings": [GOOD, GOOD]})

        assert await ollama.get_embeddings_batch_async(["a", "b"]) == [GOOD, GOOD]
        assert calls["n"] == 2

    async def test_empty_vector_twice_raises_and_returns_nothing(self):
        calls = _serve({"embeddings": [GOOD, []]})

        with pytest.raises(httpx.HTTPError) as caught:
            await ollama.get_embeddings_batch_async(["a", "b"])

        message = str(caught.value)
        assert MODEL in message
        assert "0" in message and str(WIDTH) in message
        assert calls["n"] == 2

    async def test_wrong_width_in_a_batch_raises(self):
        _serve({"embeddings": [[0.5] * 768, [0.5] * 768]})

        with pytest.raises(httpx.HTTPError, match="768"):
            await ollama.get_embeddings_batch_async(["a", "b"])

    async def test_single_text_batch_is_validated_too(self):
        _serve({"embeddings": [[]]})

        with pytest.raises(httpx.HTTPError, match=MODEL):
            await ollama.get_embeddings_batch_async(["a"])
