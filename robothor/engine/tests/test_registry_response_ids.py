"""Two models in use resolve under every id they are seen by, and are priced.

Vertex reports a Gemini response under the bare id (``gemini-3.8-flash``), not
the routed one the registry carries (``vertex_ai/gemini-3.8-flash``). Cost and
context sizing look the response id up, so every such call logged "Unknown
model", was sized at the 128K fallback and was priced at $0. The OpenAI row
already has its bare alias for the same reason.

Neither model is in litellm's bundled price table, and the legacy pricing path
registered only two hand-written rows, so ``completion_cost`` failed (and
warned) on every call. A curated row litellm does not know is now registered
either way; a model litellm does know keeps litellm's own figures.
"""

from __future__ import annotations

import logging

import pytest

from robothor.engine import model_registry
from robothor.engine.model_registry import get_model_limits

MODELS = ("vertex_ai/gemini-3.8-flash", "gemini-3.8-flash", "openai/gpt-6-luna", "gpt-6-luna")


@pytest.mark.parametrize("model_id", MODELS)
def test_each_id_resolves_without_an_unknown_model_warning(model_id, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="robothor.engine.model_registry"):
        limits = get_model_limits(model_id)
    assert limits is not model_registry._FALLBACK
    assert limits.input_cost_per_token > 0
    assert "Unknown model" not in caplog.text


def test_the_bare_gemini_id_is_the_vertex_row() -> None:
    assert get_model_limits("gemini-3.8-flash") == get_model_limits("vertex_ai/gemini-3.8-flash")


@pytest.mark.parametrize("model_id", MODELS)
def test_a_response_under_each_id_is_priced(model_id) -> None:
    from robothor.engine.llm_client import LLMClient

    assert LLMClient()._calculate_cost(model_id, 1_000, 100) > 0
