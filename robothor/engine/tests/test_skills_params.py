"""Tests for skill parameter system — parsing, validation, substitution."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from robothor.engine.skills import (
    SkillDefinition,
    SkillParameter,
    _parse_skill_file,
    build_skill_catalog,
)

# ─── Dataclass Tests ─────────────────────────────────────────────────


class TestSkillParameter:
    def test_skill_parameter_dataclass(self):
        """SkillParameter has all expected fields with defaults."""
        p = SkillParameter(
            name="glob", type="file_glob", description="Pattern", required=True, default=None
        )
        assert p.name == "glob"
        assert p.type == "file_glob"
        assert p.description == "Pattern"
        assert p.required is True
        assert p.default is None

    def test_skill_parameter_defaults(self):
        """SkillParameter defaults: type=string, required=False, default=None."""
        p = SkillParameter(name="x")
        assert p.type == "string"
        assert p.description == ""
        assert p.required is False
        assert p.default is None


class TestSkillDefinition:
    def test_skill_definition_new_fields(self):
        """SkillDefinition supports parameters, output_format, composable, depends_on."""
        params = (SkillParameter(name="glob", required=True),)
        defn = SkillDefinition(
            name="batch",
            description="Run in batch",
            content="steps here",
            path="/skills/batch/SKILL.md",
            parameters=params,
            output_format="json",
            composable=True,
            depends_on=("setup",),
        )
        assert defn.parameters == params
        assert defn.output_format == "json"
        assert defn.composable is True
        assert defn.depends_on == ("setup",)

    def test_output_format_default(self):
        """Default output_format is 'text'."""
        defn = SkillDefinition(name="x", description="", content="", path="")
        assert defn.output_format == "text"

    def test_composable_default(self):
        """Default composable is False."""
        defn = SkillDefinition(name="x", description="", content="", path="")
        assert defn.composable is False


# ─── Parsing Tests ───────────────────────────────────────────────────


class TestParseSkill:
    def test_parse_skill_with_parameters(self, tmp_path: Path):
        """A SKILL.md with YAML parameters block parses into SkillParameter list."""
        skill_dir = tmp_path / "batch"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            """\
---
name: batch
description: Run a task across files
parameters:
  - name: glob
    type: file_glob
    description: File pattern
    required: true
  - name: instruction
    type: string
    description: What to do
    required: true
  - name: concurrency
    type: integer
    description: Parallel workers
    required: false
    default: 3
output_format: json
composable: true
depends_on:
  - setup
---

Run the instruction across all files matching glob.
"""
        )
        defn = _parse_skill_file(skill_file)
        assert defn is not None
        assert defn.name == "batch"
        assert len(defn.parameters) == 3
        assert defn.parameters[0].name == "glob"
        assert defn.parameters[0].type == "file_glob"
        assert defn.parameters[0].required is True
        assert defn.parameters[2].name == "concurrency"
        assert defn.parameters[2].default == 3
        assert defn.output_format == "json"
        assert defn.composable is True
        assert defn.depends_on == ("setup",)

    def test_parse_skill_no_parameters(self, tmp_path: Path):
        """Existing skills without parameters still parse fine (backward compat)."""
        skill_dir = tmp_path / "simple"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            """\
---
name: simple-skill
description: A basic skill
tags: [util]
---

Just do the thing.
"""
        )
        defn = _parse_skill_file(skill_file)
        assert defn is not None
        assert defn.name == "simple-skill"
        assert defn.parameters == ()
        assert defn.output_format == "text"
        assert defn.composable is False
        assert defn.depends_on == ()


# ─── Catalog Tests ───────────────────────────────────────────────────


class TestBuildCatalog:
    def test_build_catalog_with_params(self):
        """Skills with parameters show a signature like batch(glob, instruction, concurrency=3)."""
        skills = {
            "batch": SkillDefinition(
                name="batch",
                description="Run in batch",
                content="",
                path="",
                parameters=(
                    SkillParameter(name="glob", required=True),
                    SkillParameter(name="instruction", required=True),
                    SkillParameter(name="concurrency", default=3),
                ),
            ),
        }
        catalog = build_skill_catalog(skills)
        assert "**batch**" in catalog
        assert "(glob, instruction, concurrency=3)" in catalog
        assert "Run in batch" in catalog

    def test_build_catalog_without_params(self):
        """Skills without params show no signature parentheses."""
        skills = {
            "simple": SkillDefinition(
                name="simple",
                description="A simple skill",
                content="",
                path="",
            ),
        }
        catalog = build_skill_catalog(skills)
        assert "**simple**" in catalog
        assert "(" not in catalog.split("**simple**")[1].split(":")[0]
        assert "A simple skill" in catalog
