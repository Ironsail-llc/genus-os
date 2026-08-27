"""The engine must state its own residency policy to the local server.

2026-08-27. `_build_llm_kwargs` sent NO keep_alive and NO num_ctx for local
models, so the fleet's offline tier inherited the server default (10m — shorter
than several agents' cron cadence, meaning every cron paid a ~34s cold load)
and sat at its FULL 262144-token context, occupying 18.9GB of a shared pool
that also held a pinned embedder, a reranker and a 1.7B. The memory client
already does this properly (robothor/llm/ollama.py); the engine never did.

The dangerous half is `num_ctx`, and it is why this could not ship as a config
change. `proactive_compaction_threshold` fires at min(0.5 * max_input_tokens,
80_000) — driven by the REGISTRY. Shrink the server's window without shrinking
the registry and the engine keeps filling context toward 80k against a smaller
server window: silent server-side truncation, far harder to diagnose than the
memory it saves. The two numbers must move together, and the invariant test
below is what keeps them together.
"""

from __future__ import annotations

import pytest

from robothor.engine.llm_client import LLMClient
from robothor.engine.model_registry import get_model_limits, get_output_tokens
from robothor.engine.run_budget import proactive_compaction_threshold

LOCAL = "ollama_chat/qwen3.8:27b"
CLOUD = "openrouter/xiaomi/mimo-v2.5"


def _kwargs(model: str) -> dict:
    return LLMClient._build_llm_kwargs(
        model, [{"role": "user", "content": "hi"}], [], 10, 0.3
    )


class TestTheEngineDeclaresResidency:
    def test_a_local_model_gets_keep_alive(self):
        assert "keep_alive" in _kwargs(LOCAL)

    def test_a_local_model_gets_num_ctx(self):
        assert "num_ctx" in _kwargs(LOCAL)

    def test_a_cloud_model_gets_neither(self):
        k = _kwargs(CLOUD)
        assert "keep_alive" not in k
        assert "num_ctx" not in k

    def test_num_ctx_equals_the_registry_window(self):
        """The anti-truncation invariant: one number, two consumers."""
        assert _kwargs(LOCAL)["num_ctx"] == get_model_limits(LOCAL).max_input_tokens


class TestTheWindowCannotBeShrunkAlone:
    """The test that stops someone reclaiming GPU memory and causing silent
    truncation months later."""

    @pytest.mark.parametrize("model", [LOCAL, "ollama_chat/qwen3:8b"])
    def test_compaction_fires_before_the_window_overflows(self, model):
        window = get_model_limits(model).max_input_tokens
        threshold = proactive_compaction_threshold(window)
        output = get_output_tokens(model, threshold)
        assert threshold + output <= window, (
            f"{model}: compaction waits until {threshold} input tokens and then asks for "
            f"{output} output, which overflows the {window}-token window the engine tells "
            f"the server to allocate — the server would truncate silently"
        )

    def test_the_local_window_is_no_longer_the_full_262144(self):
        """18.9GB of a shared pool, for a model that never sees 262k tokens."""
        assert get_model_limits(LOCAL).max_input_tokens < 262_144


class TestLitellmActuallyForwardsThem:
    """Anti-inertness. Without this, a litellm upgrade can start dropping these
    kwargs and every test above still passes while the GPU quietly goes back to
    18.9GB and the cold loads return."""

    def test_get_optional_params_keeps_both(self):
        from litellm.utils import get_optional_params

        params = get_optional_params(
            model="qwen3.8:27b",
            custom_llm_provider="ollama_chat",
            num_ctx=32768,
            keep_alive="30m",
            max_tokens=1024,
            temperature=0.3,
        )
        assert params.get("num_ctx") == 32768
        assert params.get("keep_alive") == "30m"

    def test_the_transform_puts_them_where_ollama_reads_them(self):
        from litellm.llms.ollama.chat.transformation import OllamaChatConfig

        req = OllamaChatConfig().transform_request(
            model="qwen3.8:27b",
            messages=[{"role": "user", "content": "hi"}],
            optional_params={"num_ctx": 32768, "keep_alive": "30m"},
            litellm_params={},
            headers={},
        )
        assert req.get("keep_alive") == "30m", "keep_alive must be top-level"
        assert req.get("options", {}).get("num_ctx") == 32768, "num_ctx must be in options"
