"""`view_image` gave up when the extension did not match the file on disk.

Measured 2026-08-26, in a real benchmark run:

    view_image 找不到 thesis_abstract_page.png —— 实际扩展名是 .jpg

The agent asked for `.png`, the file was `.jpg`, and the tool answered "no
such file". The agent recovered by guessing, but it spent turns doing it and
the run scored 0.43 against a 0.94 baseline.

This is not a niche case. Of the twelve Code Intelligence tasks — the
category where the harness trails OpenClaw by 16.1 points — **eleven are
image tasks**: jigsaw puzzles, connect-the-dots, link-a-pix, OCR
benchmarks. A vision tool that fails on a wrong extension is failing in the
category that decides that gap.

The resolution is deliberately narrow. A same-stem sibling is used only when
exactly ONE exists, and the answer always names the file actually read: a
tool that silently substitutes a different file would be worse than one
that fails, because the agent would reason about an image it never asked for.
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.handlers.images import view_image


def _ctx(tmp_path):
    """A run context with a workspace — what production always passes.

    Round-1 review I-4: `view_image` now judges containment on the resolved
    path, the way `analyze_image` always has, so a call with no workspace is a
    shape production never uses.
    """
    return type("Ctx", (), {"workspace": str(tmp_path), "run_id": "r1", "agent_id": "probe"})()


def _png(path):
    from PIL import Image

    Image.new("RGB", (8, 8), (10, 120, 200)).save(path, format="PNG")
    return path


class TestExactPathStillWins:
    @pytest.mark.asyncio
    async def test_an_existing_file_is_read_as_asked(self, tmp_path):
        p = _png(tmp_path / "figure.png")
        out = await view_image({"path": str(p)}, _ctx(tmp_path))
        assert "error" not in out
        assert out.get("resolved_from") is None

    @pytest.mark.asyncio
    async def test_a_missing_path_with_no_sibling_still_errors(self, tmp_path):
        out = await view_image({"path": str(tmp_path / "absent.png")}, _ctx(tmp_path))
        assert "no such file" in out.get("error", "")


class TestExtensionMiss:
    @pytest.mark.asyncio
    async def test_the_incident_case(self, tmp_path):
        """Asked for .png, the file is .jpg — the 2026-08-26 failure verbatim."""
        _png(tmp_path / "thesis_abstract_page.jpg")
        out = await view_image({"path": str(tmp_path / "thesis_abstract_page.png")}, _ctx(tmp_path))
        assert "error" not in out, out
        assert out["path"].endswith("thesis_abstract_page.jpg"), "the file that was read"
        assert out["resolved_from"].endswith("thesis_abstract_page.png"), "the file you asked for"

    @pytest.mark.asyncio
    async def test_the_substitution_is_always_named(self, tmp_path):
        """A silent substitution would be worse than failing."""
        _png(tmp_path / "chart.jpeg")
        out = await view_image({"path": str(tmp_path / "chart.webp")}, _ctx(tmp_path))
        assert out.get("resolved_from"), "the tool read a different file without saying so"

    @pytest.mark.asyncio
    async def test_resolved_from_is_the_path_you_asked_for_not_the_one_you_got(self, tmp_path):
        """Review I2. `resolved_from` used to be set to the SUBSTITUTE — which
        is `path`, so the field carried no information at all — while the
        sibling tool `analyze_image` sets it to the path the agent asked for.
        Same tool family, same key, opposite readings: an agent that learned
        the key here read an `analyze_image` row as "this is the file I got",
        which is the one misreading the field exists to prevent."""
        _png(tmp_path / "scan.jpg")
        out = await view_image({"path": str(tmp_path / "scan.png")}, _ctx(tmp_path))
        assert out["resolved_from"] != out["path"], (
            "a field equal to `path` on every row it appears on says nothing"
        )
        assert out["resolved_from"] == str(tmp_path / "scan.png")
        assert out["path"] == str(tmp_path / "scan.jpg")

    @pytest.mark.asyncio
    async def test_ambiguity_is_refused_not_guessed(self, tmp_path):
        """Two candidates means the agent must choose, not the tool."""
        _png(tmp_path / "plot.jpg")
        _png(tmp_path / "plot.gif")
        out = await view_image({"path": str(tmp_path / "plot.png")}, _ctx(tmp_path))
        assert "error" in out
        assert "plot.gif" in out["error"] and "plot.jpg" in out["error"]

    @pytest.mark.asyncio
    async def test_a_non_image_sibling_is_not_offered(self, tmp_path):
        (tmp_path / "notes.txt").write_text("not an image")
        out = await view_image({"path": str(tmp_path / "notes.png")}, _ctx(tmp_path))
        assert "no such file" in out.get("error", "")

    @pytest.mark.asyncio
    async def test_a_directory_sibling_is_not_offered(self, tmp_path):
        (tmp_path / "assets.jpg").mkdir()
        out = await view_image({"path": str(tmp_path / "assets.png")}, _ctx(tmp_path))
        assert "no such file" in out.get("error", "")
