"""Tests for the Rip 3 skills router (lean catalog + skill_view)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from robothor.engine.skills import (
    SkillDefinition,
    SkillParameter,
    build_skill_catalog,
)
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.skills import _skill_view


def _make_skill(
    name: str = "code-review",
    description: str = "Review a pull request systematically",
    parameters: list[SkillParameter] | None = None,
    trigger_phrases: list[str] | None = None,
    content: str = "# Code Review\n\nProcedure goes here…",
) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description=description,
        parameters=parameters or [],
        trigger_phrases=trigger_phrases or [],
        tools_required=[],
        composable=False,
        tags=[],
        output_format="text",
        content=content,
        path=Path("/tmp/fake/SKILL.md"),
    )


class TestLeanCatalogRip3Enabled:
    def test_lean_catalog_one_line_per_skill(self) -> None:
        skills = {
            "code-review": _make_skill("code-review", "Review a pull request"),
            "send-email": _make_skill("send-email", "Send an email via gws"),
        }
        with patch.dict(os.environ, {"ROBOTHOR_RIP_3_ENABLED": "1"}, clear=True):
            out = build_skill_catalog(skills)
        assert "- /code-review — Review a pull request" in out
        assert "- /send-email — Send an email via gws" in out

    def test_lean_catalog_omits_signature_and_triggers(self) -> None:
        skills = {
            "code-review": _make_skill(
                "code-review",
                "Review",
                parameters=[
                    SkillParameter(
                        name="pr_number", type="int", description="", required=True, default=None
                    )
                ],
                trigger_phrases=["review pr", "check the diff"],
            )
        }
        with patch.dict(os.environ, {"ROBOTHOR_RIP_3_ENABLED": "1"}, clear=True):
            out = build_skill_catalog(skills)
        # Catalog must NOT include parameter signature or trigger phrases.
        assert "pr_number" not in out
        assert "review pr" not in out
        assert "check the diff" not in out

    def test_lean_catalog_truncates_long_descriptions(self) -> None:
        long_desc = "a" * 500
        skills = {"x": _make_skill("x", long_desc)}
        with patch.dict(os.environ, {"ROBOTHOR_RIP_3_ENABLED": "1"}, clear=True):
            out = build_skill_catalog(skills)
        assert "..." in out
        # Bounded line length per skill.
        for line in out.splitlines():
            if line.startswith("- /"):
                assert len(line) < 130, f"line too long: {line[:80]}..."

    def test_lean_catalog_mentions_skill_view(self) -> None:
        skills = {"x": _make_skill("x", "desc")}
        with patch.dict(os.environ, {"ROBOTHOR_RIP_3_ENABLED": "1"}, clear=True):
            out = build_skill_catalog(skills)
        assert "skill_view" in out
        assert "invoke_skill" in out


class TestLegacyCatalogRip3Disabled:
    def test_legacy_catalog_includes_signature(self) -> None:
        skills = {
            "code-review": _make_skill(
                "code-review",
                "Review",
                parameters=[
                    SkillParameter(
                        name="pr_number",
                        type="int",
                        description="",
                        required=True,
                        default=None,
                    )
                ],
            )
        }
        with patch.dict(os.environ, {}, clear=True):  # rip 3 off
            out = build_skill_catalog(skills)
        # Legacy format: bold name + signature in parens.
        assert "**code-review**" in out
        assert "(pr_number)" in out


class TestSkillView:
    @pytest.mark.asyncio
    async def test_returns_full_body(self) -> None:
        body = "# Body of the skill\n\nWith multiple lines.\n"
        sk = _make_skill("test-skill", "test", content=body)

        with (
            patch(
                "robothor.engine.skills.load_skills",
                return_value={"test-skill": sk},
            ),
            patch(
                "robothor.engine.skills.get_skill_content",
                return_value=body,
            ),
            patch(
                "robothor.engine.skills.read_skill_meta",
                return_value={"usage_count": 3, "write_origin": "background_review"},
            ),
            patch("robothor.engine.skills.increment_usage"),
        ):
            result = await _skill_view({"name": "test-skill"}, ToolContext(agent_id="a"))

        assert result["name"] == "test-skill"
        assert result["content"] == body
        assert result["usage_count"] == 3
        assert result["write_origin"] == "background_review"

    @pytest.mark.asyncio
    async def test_missing_name_error(self) -> None:
        result = await _skill_view({}, ToolContext(agent_id="a"))
        assert "name is required" in result["error"]

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        with patch(
            "robothor.engine.skills.get_skill_content",
            return_value=None,
        ):
            result = await _skill_view({"name": "no-such"}, ToolContext(agent_id="a"))
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_increment_usage_failure_does_not_break_view(self) -> None:
        body = "body"
        sk = _make_skill("x", "x", content=body)

        with (
            patch(
                "robothor.engine.skills.load_skills",
                return_value={"x": sk},
            ),
            patch(
                "robothor.engine.skills.get_skill_content",
                return_value=body,
            ),
            patch(
                "robothor.engine.skills.read_skill_meta",
                return_value=None,  # no meta yet
            ),
            patch(
                "robothor.engine.skills.increment_usage",
                side_effect=RuntimeError("disk full"),
            ),
        ):
            result = await _skill_view({"name": "x"}, ToolContext(agent_id="a"))

        # The view succeeds even though incrementing the counter raised.
        assert result["content"] == body
        assert result["usage_count"] == 0
        assert result["is_agent_created"] is False
