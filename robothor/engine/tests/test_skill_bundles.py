"""Tests for Rip 11 skill bundles (loader + slash resolver + message builder)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from robothor.engine.skill_bundles import (
    BundleDefinition,
    build_bundle_invocation_message,
    get_bundle,
    load_bundles,
    resolve_slash_command,
)


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    return tmp_path


def _write_bundle(path: Path, **fields: object) -> Path:
    import yaml

    p = path / f"{fields['name']}.yaml"
    p.write_text(yaml.safe_dump(fields))
    return p


class TestLoadBundles:
    def test_empty_dir_returns_empty_mapping(self, tmp_path: Path) -> None:
        assert load_bundles(tmp_path) == {}

    def test_loads_valid_bundle(self, bundle_dir: Path) -> None:
        _write_bundle(
            bundle_dir,
            name="release",
            description="ship a release",
            instruction="follow each step",
            skills=["code-review", "run-tests"],
        )
        bundles = load_bundles(bundle_dir)
        assert "release" in bundles
        b = bundles["release"]
        assert b.skills == ("code-review", "run-tests")
        assert b.description == "ship a release"
        assert b.instruction == "follow each step"

    def test_skips_malformed_yaml(self, bundle_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
        bad = bundle_dir / "bad.yaml"
        bad.write_text(":\n  - not a valid mapping\n  - {")
        with caplog.at_level(logging.WARNING):
            result = load_bundles(bundle_dir)
        assert result == {}

    def test_skips_bundle_without_skills(self, bundle_dir: Path) -> None:
        _write_bundle(bundle_dir, name="empty", description="x", skills=[])
        assert load_bundles(bundle_dir) == {}

    def test_skips_invalid_name(self, bundle_dir: Path) -> None:
        _write_bundle(bundle_dir, name="X", description="x", skills=["a"])
        assert load_bundles(bundle_dir) == {}

    def test_skips_dunder_files(self, bundle_dir: Path) -> None:
        _write_bundle(bundle_dir, name="real", description="x", skills=["a"])
        _write_bundle(bundle_dir, name="_hidden", description="x", skills=["a"])
        # _hidden.yaml passes name regex? '_hidden' fails kebab regex so
        # parse rejects. The dunder-skip is a belt-and-braces guard.
        result = load_bundles(bundle_dir)
        assert "real" in result
        assert "_hidden" not in result

    def test_duplicate_name_keeps_first(
        self, bundle_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Two files both declaring name='dup'; second is skipped.
        import yaml

        (bundle_dir / "a.yaml").write_text(
            yaml.safe_dump({"name": "dup", "description": "first", "skills": ["x"]})
        )
        (bundle_dir / "b.yaml").write_text(
            yaml.safe_dump({"name": "dup", "description": "second", "skills": ["y"]})
        )
        with caplog.at_level(logging.WARNING):
            bundles = load_bundles(bundle_dir)
        assert bundles["dup"].skills == ("x",)


class TestGetBundle:
    def test_case_insensitive(self, bundle_dir: Path) -> None:
        _write_bundle(bundle_dir, name="release", description="r", skills=["a"])
        assert get_bundle("RELEASE", bundle_dir) is not None
        assert get_bundle("release", bundle_dir) is not None
        assert get_bundle("Release ", bundle_dir) is not None

    def test_missing_returns_none(self, bundle_dir: Path) -> None:
        assert get_bundle("nope", bundle_dir) is None


class TestResolveSlashCommand:
    def test_bundle_wins_over_skill(self, bundle_dir: Path) -> None:
        _write_bundle(bundle_dir, name="release", description="r", skills=["a"])
        # A skill ALSO named 'release' exists in the live skills map.
        kind, bundle = resolve_slash_command(
            "/release",
            bundles_dir=bundle_dir,
            skills={"release": object()},  # any truthy dict membership
        )
        assert kind == "bundle"
        assert bundle is not None
        assert bundle.name == "release"

    def test_falls_back_to_skill(self, bundle_dir: Path) -> None:
        kind, bundle = resolve_slash_command(
            "/code-review", bundles_dir=bundle_dir, skills={"code-review": object()}
        )
        assert kind == "skill"
        assert bundle is None

    def test_unknown(self, bundle_dir: Path) -> None:
        kind, bundle = resolve_slash_command("/nope", bundles_dir=bundle_dir, skills={})
        assert kind == "unknown"

    def test_strips_leading_slash_and_whitespace(self, bundle_dir: Path) -> None:
        _write_bundle(bundle_dir, name="release", description="r", skills=["a"])
        kind, _ = resolve_slash_command("/Release ", bundles_dir=bundle_dir, skills={})
        assert kind == "bundle"

    def test_empty_command(self, bundle_dir: Path) -> None:
        kind, bundle = resolve_slash_command("/", bundles_dir=bundle_dir, skills={})
        assert kind == "unknown"
        assert bundle is None


class TestBuildBundleInvocationMessage:
    def test_concatenates_known_skill_bodies(self) -> None:
        bundle = BundleDefinition(
            name="release",
            description="release flow",
            instruction="follow each step",
            skills=("a", "b"),
        )
        msg = build_bundle_invocation_message(
            bundle, skill_bodies={"a": "# A\nbody-a", "b": "# B\nbody-b"}
        )
        assert "# Bundle: release" in msg
        assert "release flow" in msg
        assert "follow each step" in msg
        assert "## Skill: a" in msg
        assert "body-a" in msg
        assert "## Skill: b" in msg
        assert "body-b" in msg

    def test_missing_body_renders_placeholder(self) -> None:
        bundle = BundleDefinition(name="x", description="", skills=("a", "missing"))
        msg = build_bundle_invocation_message(bundle, skill_bodies={"a": "body-a"})
        assert "body-a" in msg
        assert "skill 'missing' not found" in msg

    def test_no_instruction_renders_cleanly(self) -> None:
        bundle = BundleDefinition(name="x", description="x", skills=("a",))
        msg = build_bundle_invocation_message(bundle, skill_bodies={"a": "body"})
        assert "## Skill: a" in msg
