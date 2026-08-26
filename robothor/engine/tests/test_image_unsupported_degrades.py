"""Looking at a picture must never destroy a run.

2026-08-25: `view_image` shipped, and the first real benchmark run using it
died in EIGHT SECONDS. GLM 5.2 is text-only on OpenRouter, which answers an
image content block with `404 No endpoints found that support image input`.
Every model in the chain failed the same way, the runner raised "All models
failed to respond", and the whole run was lost — because the agent looked at
a file.

That is a production hazard, not a benchmark one: any agent whose model (or
whose fallback) is text-only loses its entire run to one curious tool call.
The fleet primary changes with the model market; the offline tier is
whatever Ollama has.

The fix strips image blocks from the conversation and retries the SAME
model once, leaving the agent a plain-text note saying the picture could not
be shown and to inspect it programmatically instead. Degraded, not dead.
"""

from __future__ import annotations

import pytest

from robothor.engine.llm_client import (
    IMAGE_UNSUPPORTED_NOTE,
    is_image_unsupported_error,
    strip_image_blocks,
)


class TestDetectingTheRefusal:
    def test_openrouter_404_is_recognised(self):
        e = Exception(
            'OpenrouterException - {"error":{"message":"No endpoints found '
            'that support image input","code":404}}'
        )
        assert is_image_unsupported_error(e)

    def test_other_provider_phrasings_are_recognised(self):
        for msg in (
            "Invalid content type. image_url is not supported by this model.",
            "This model does not support image input",
            "image input is not supported for this model",
        ):
            assert is_image_unsupported_error(Exception(msg)), msg

    def test_an_unrelated_404_is_not(self):
        assert not is_image_unsupported_error(Exception("404 model not found"))

    def test_an_unrelated_failure_is_not(self):
        assert not is_image_unsupported_error(Exception("rate limit exceeded"))


class TestStrippingImages:
    def test_image_blocks_become_a_plain_note(self):
        messages = [
            {"role": "user", "content": "look at this"},
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    {"type": "text", "text": "Image (40x30)"},
                ],
            },
        ]
        out, changed = strip_image_blocks(messages)
        assert changed
        content = out[1]["content"]
        assert isinstance(content, str)
        assert IMAGE_UNSUPPORTED_NOTE in content
        assert "Image (40x30)" in content, "the caption is context and must survive"
        assert "AAAA" not in content, "base64 must not be re-sent as text"

    def test_the_original_list_is_not_mutated(self):
        messages = [
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
                ],
            }
        ]
        strip_image_blocks(messages)
        assert isinstance(messages[0]["content"], list), "caller's messages were mutated"

    def test_a_conversation_without_images_is_untouched(self):
        messages = [{"role": "user", "content": "plain"}]
        out, changed = strip_image_blocks(messages)
        assert not changed
        assert out == messages


class TestTheRetryIsWired:
    def test_the_dispatch_path_retries_on_this_error(self):
        """A guard on the wiring, not just the helpers."""
        from pathlib import Path

        import robothor.engine.llm_client as m

        source = Path(m.__file__).read_text(encoding="utf-8")
        assert "is_image_unsupported_error" in source
        assert "strip_image_blocks" in source

    @pytest.mark.asyncio
    async def test_a_text_only_model_still_completes_the_call(self, monkeypatch):
        """End to end through the dispatcher: first call 404s on the image,
        the retry without it succeeds, and the run continues."""
        from robothor.engine.llm_client import LLMClient

        calls: list[list[dict]] = []

        async def fake_acompletion(**kwargs):
            calls.append(kwargs["messages"])
            if len(calls) == 1:
                raise Exception(
                    'OpenrouterException - {"error":{"message":"No endpoints '
                    'found that support image input","code":404}}'
                )

            class _R:
                model = "test"
                choices = [
                    type(
                        "C", (), {"message": type("M", (), {"content": "ok", "tool_calls": None})()}
                    )()
                ]
                usage = type("U", (), {"prompt_tokens": 5, "completion_tokens": 2})()

            return _R()

        monkeypatch.setattr("litellm.acompletion", fake_acompletion)
        client = LLMClient.__new__(LLMClient)
        messages = [
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
                ],
            }
        ]
        # kwargs carries `messages`, exactly as the real call site builds it.
        result = await client._call_with_image_fallback(  # type: ignore[attr-defined]
            model="openrouter/z-ai/glm-5.2",
            messages=messages,
            kwargs={"model": "openrouter/z-ai/glm-5.2", "messages": messages},
        )
        assert result is not None
        assert len(calls) == 2, "the image-free retry never happened"
        assert isinstance(calls[1][0]["content"], str), "the retry still carried an image"
