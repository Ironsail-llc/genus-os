"""``view_image`` must never claim the agent looked at something it did not.

Before this, the tool returned image content blocks for every model. When the
model was text-only, ``llm_client.strip_image_blocks`` deleted them one layer
down and the agent was left holding a caption where a picture had been — it
believed it had looked, and it had seen nothing. The registry now carries the
capability (``test_model_accepts_images.py``); this is what the tool does with
it, and the field the agent reads to know which of the two happened.
"""

from __future__ import annotations

import pytest

from robothor.engine import model_registry as mr


@pytest.fixture(autouse=True)
def _clean_capability_state():
    mr.reset_image_discoveries()
    token = mr.note_active_model("")
    yield
    mr.reset_active_model(token)
    mr.reset_image_discoveries()


def _ctx(tmp_path):
    """A run context with a workspace — what production always passes.

    Round-1 review I-4: `view_image` now judges containment on the resolved
    path, the way `analyze_image` always has, so a call with no workspace is a
    shape production never uses.
    """
    return type("Ctx", (), {"workspace": str(tmp_path), "run_id": "r1", "agent_id": "probe"})()


async def _call(args, ctx=None):
    from robothor.engine.tools.dispatch import _collect_handlers

    return await _collect_handlers()["view_image"](args, ctx)


def _png(tmp_path, name="x.png", size=(40, 30)):
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", size, (200, 30, 30)).save(path)
    return path


class TestAcceptingModel:
    @pytest.mark.asyncio
    async def test_the_picture_itself_is_returned(self, tmp_path) -> None:
        token = mr.note_active_model("openrouter/anthropic/claude-sonnet-4.6")
        try:
            out = await _call({"path": str(_png(tmp_path))}, _ctx(tmp_path))
        finally:
            mr.reset_active_model(token)
        assert out.get("image_base64")
        assert out["seen_by"] == "primary"

    @pytest.mark.asyncio
    async def test_an_undeclared_model_still_gets_the_picture(self, tmp_path) -> None:
        """`unknown` is not `rejects`: a multimodal model that simply is not in
        the curated table must keep being shown pictures."""
        token = mr.note_active_model("acme/never-heard-of-it-v9")
        try:
            out = await _call({"path": str(_png(tmp_path))}, _ctx(tmp_path))
        finally:
            mr.reset_active_model(token)
        assert out.get("image_base64")
        assert out["seen_by"] == "primary"
        assert "not confirmed" in (out.get("note") or "").lower()


class TestRejectingModel:
    @pytest.mark.asyncio
    async def test_no_blocks_are_returned_for_a_model_that_cannot_see(
        self, tmp_path, monkeypatch
    ) -> None:
        async def fake_vlm(data, prompt="", **kw):
            return "a red rectangle"

        monkeypatch.setattr("robothor.engine.tools.handlers.images.describe_image_bytes", fake_vlm)
        token = mr.note_active_model("ollama_chat/qwen3:8b")
        try:
            out = await _call({"path": str(_png(tmp_path))}, _ctx(tmp_path))
        finally:
            mr.reset_active_model(token)
        assert "image_base64" not in out, "a block the client will only strip is a lie"
        assert out["seen_by"] == "vision-model"
        assert out["description"] == "a red rectangle"
        # `model` is whichever vision model answered and is empty when the
        # local rung is faked away; `primary_model` is the one that could not
        # look, which is what this test is about.
        assert out["primary_model"]
        assert out["backend"] == "local"

    @pytest.mark.asyncio
    async def test_a_runtime_discovery_switches_the_answer(self, tmp_path, monkeypatch) -> None:
        async def fake_vlm(data, prompt="", **kw):
            return "a red rectangle"

        monkeypatch.setattr("robothor.engine.tools.handlers.images.describe_image_bytes", fake_vlm)
        token = mr.note_active_model("openrouter/anthropic/claude-sonnet-4.6")
        try:
            first = await _call({"path": str(_png(tmp_path))}, _ctx(tmp_path))
            assert first["seen_by"] == "primary"
            mr.note_image_rejection("openrouter/anthropic/claude-sonnet-4.6")
            second = await _call({"path": str(_png(tmp_path))}, _ctx(tmp_path))
        finally:
            mr.reset_active_model(token)
        assert second["seen_by"] == "vision-model"

    @pytest.mark.asyncio
    async def test_no_vision_model_is_said_plainly_never_faked(self, tmp_path, monkeypatch) -> None:
        async def broken_vlm(data, prompt="", **kw):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(
            "robothor.engine.tools.handlers.images.describe_image_bytes", broken_vlm
        )
        token = mr.note_active_model("ollama_chat/qwen3:8b")
        try:
            out = await _call({"path": str(_png(tmp_path))}, _ctx(tmp_path))
        finally:
            mr.reset_active_model(token)
        assert "image_base64" not in out
        assert out["seen_by"] == "nobody"
        assert "error" in out
