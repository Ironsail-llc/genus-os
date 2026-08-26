"""An agent can look at a picture.

Measured gap, 2026-08-25: four WildClawBench Code Intelligence tasks hand the
agent a PNG and ask it to read a grid off it. Genus scored 0 on every one
(OpenClaw: 93/88/30/22) — not because the model cannot see (GLM 5.2 is
multimodal and the OTHER harness fed it the image), but because nothing in
this engine could put an image in front of the agent's own model.

The plumbing already existed and was locked to two tools. `session.py`
emitted a proper `image_url` content block, but only when the tool name was
literally `desktop_screenshot` or `browser`. The one other place an image
reached any model was inside the PDF handler, which side-calls a hardcoded
Gemini and hands the agent back text.

Two changes: the emit rule becomes a documented output convention any tool
can use, and `view_image` is the tool that uses it. This is a production
capability, not a benchmark fix — a photo in Telegram, a chart in an email,
a screenshot in a bug report all needed it.
"""

from __future__ import annotations

import base64

import pytest


async def _call(args):
    """Invoke the tool exactly as dispatch does: through the registered map,
    with (args, ctx). Calling the function directly with one argument is how
    a TypeError reached production while every unit test stayed green."""
    from robothor.engine.tools.dispatch import _collect_handlers

    return await _collect_handlers()["view_image"](args, None)


def _png(tmp_path, name="x.png", size=(40, 30), color=(200, 30, 30)):
    from PIL import Image

    p = tmp_path / name
    Image.new("RGB", size, color).save(p)
    return p


class TestViewImageTool:
    @pytest.mark.asyncio
    async def test_returns_the_image_convention(self, tmp_path):
        result = await _call({"path": str(_png(tmp_path))})
        assert result["image_mime"] == "image/png"
        assert result["width"] == 40
        assert result["height"] == 30
        base64.b64decode(result["image_base64"])  # decodes cleanly

    @pytest.mark.asyncio
    async def test_a_missing_file_is_an_error_not_a_crash(self, tmp_path):
        result = await _call({"path": str(tmp_path / "nope.png")})
        assert "error" in result
        assert "image_base64" not in result

    @pytest.mark.asyncio
    async def test_a_non_image_is_refused_by_content_not_extension(self, tmp_path):
        """A .png that is really a text file must not reach the model as one."""
        fake = tmp_path / "lies.png"
        fake.write_text("this is not a png", encoding="utf-8")
        result = await _call({"path": str(fake)})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_a_huge_image_is_downscaled_not_refused(self, tmp_path):
        """API payload limits are real; a 6000px screenshot must still be
        viewable. Downscaling keeps the capability instead of erroring."""
        from robothor.engine.tools.handlers.images import MAX_DIMENSION

        big = _png(tmp_path, "big.png", size=(6000, 400))
        result = await _call({"path": str(big)})
        assert "error" not in result
        assert max(result["width"], result["height"]) <= MAX_DIMENSION
        assert result["original_width"] == 6000

    @pytest.mark.asyncio
    async def test_a_small_image_is_not_touched(self, tmp_path):
        result = await _call({"path": str(_png(tmp_path))})
        assert "original_width" not in result


class TestTheImageConventionReachesTheModel:
    """The emit rule is the output SHAPE, not a list of blessed tool names."""

    def _session(self):
        from robothor.engine.models import TriggerType
        from robothor.engine.session import AgentSession

        s = AgentSession("probe", TriggerType.CRON, "test", "test-tenant")
        s.start("", "hello", [])
        return s

    def test_any_tool_carrying_the_convention_emits_an_image_block(self, tmp_path):
        session = self._session()
        payload = {
            "image_base64": base64.b64encode(b"fake").decode("ascii"),
            "image_mime": "image/png",
            "width": 10,
            "height": 10,
        }
        session.record_tool_call(
            tool_name="view_image",
            tool_input={"path": "x.png"},
            tool_output=payload,
            tool_call_id="c1",
            duration_ms=1,
        )
        content = session.messages[-1]["content"]
        assert isinstance(content, list), "the tool result was flattened to text"
        assert content[0]["type"] == "image_url"
        assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_screenshot_tools_still_work(self, tmp_path):
        """The two original callers keep their existing output shape."""
        session = self._session()
        session.record_tool_call(
            tool_name="desktop_screenshot",
            tool_input={},
            tool_output={
                "screenshot_base64": base64.b64encode(b"fake").decode("ascii"),
                "width": 800,
                "height": 600,
            },
            tool_call_id="c2",
            duration_ms=1,
        )
        content = session.messages[-1]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "image_url"

    def test_an_ordinary_tool_result_is_still_json_text(self):
        session = self._session()
        session.record_tool_call(
            tool_name="read_file",
            tool_input={"path": "a.txt"},
            tool_output={"content": "hello"},
            tool_call_id="c3",
            duration_ms=1,
        )
        assert isinstance(session.messages[-1]["content"], str)


class TestTheToolIsRegistered:
    def test_view_image_has_a_schema(self):
        from robothor.engine.tools.schemas import get_engine_schemas

        assert "view_image" in get_engine_schemas()

    def test_view_image_dispatches(self):
        """Registered in the real handler map, not merely importable."""
        from robothor.engine.tools.dispatch import _collect_handlers

        assert "view_image" in _collect_handlers()
