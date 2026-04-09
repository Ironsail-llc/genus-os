"""Tests for autonomous skill creation and update (create_skill, update_skill)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from robothor.engine.skills import (
    _content_hash,
    create_skill_meta,
    increment_usage,
    read_skill_meta,
    validate_skill_name,
    write_skill_file,
    write_skill_meta,
)

# ─── Validation Tests ───────────────────────────────────────────────


class TestValidateSkillName:
    def test_valid_names(self):
        assert validate_skill_name("deploy-staging") is None
        assert validate_skill_name("crm-lookup") is None
        assert validate_skill_name("abc") is None
        assert validate_skill_name("a1b") is None

    def test_empty(self):
        assert validate_skill_name("") is not None

    def test_uppercase_rejected(self):
        assert validate_skill_name("Deploy-Staging") is not None

    def test_underscores_rejected(self):
        assert validate_skill_name("deploy_staging") is not None

    def test_too_short(self):
        assert validate_skill_name("ab") is not None

    def test_leading_hyphen(self):
        assert validate_skill_name("-deploy") is not None

    def test_trailing_hyphen(self):
        assert validate_skill_name("deploy-") is not None

    def test_spaces_rejected(self):
        assert validate_skill_name("deploy staging") is not None


# ─── Meta.json Tests ────────────────────────────────────────────────


class TestSkillMeta:
    def test_create_meta(self):
        meta = create_skill_meta(created_by="main")
        assert meta["auto_generated"] is True
        assert meta["created_by"] == "main"
        assert meta["revision"] == 1
        assert meta["usage_count"] == 0
        assert meta["last_used"] is None
        assert meta["revision_history"] == []

    def test_read_write_meta(self, tmp_path: Path):
        meta = {"revision": 1, "usage_count": 5}
        write_skill_meta("test-skill", meta, base=tmp_path)

        loaded = read_skill_meta("test-skill", base=tmp_path)
        assert loaded is not None
        assert loaded["revision"] == 1
        assert loaded["usage_count"] == 5

    def test_read_missing_meta(self, tmp_path: Path):
        assert read_skill_meta("nonexistent", base=tmp_path) is None

    def test_increment_usage(self, tmp_path: Path):
        meta = create_skill_meta(created_by="main")
        write_skill_meta("test-skill", meta, base=tmp_path)

        increment_usage("test-skill", base=tmp_path)

        updated = read_skill_meta("test-skill", base=tmp_path)
        assert updated is not None
        assert updated["usage_count"] == 1
        assert updated["last_used"] is not None

    def test_increment_usage_no_meta(self, tmp_path: Path):
        """increment_usage is a no-op when meta.json doesn't exist."""
        increment_usage("no-such-skill", base=tmp_path)  # should not raise


# ─── write_skill_file Tests ─────────────────────────────────────────


class TestWriteSkillFile:
    def test_basic_write(self, tmp_path: Path):
        frontmatter = {"name": "test-skill", "description": "A test skill"}
        body = "## Steps\n\n1. Do the thing\n2. Done"

        path = write_skill_file("test-skill", frontmatter, body, base=tmp_path)

        assert path.exists()
        content = path.read_text()
        assert content.startswith("---\n")
        assert "name: test-skill" in content
        assert "description: A test skill" in content
        assert "## Steps" in content

    def test_creates_directory(self, tmp_path: Path):
        path = write_skill_file(
            "new-skill",
            {"name": "new-skill", "description": "desc"},
            "body",
            base=tmp_path,
        )
        assert (tmp_path / "new-skill").is_dir()
        assert path == tmp_path / "new-skill" / "SKILL.md"

    def test_roundtrip_parse(self, tmp_path: Path):
        """Written SKILL.md can be parsed back by _parse_skill_file."""
        from robothor.engine.skills import _parse_skill_file

        frontmatter = {
            "name": "roundtrip",
            "description": "Test roundtrip",
            "tags": ["test", "demo"],
        }
        body = "Step 1: Do stuff\nStep 2: More stuff"

        path = write_skill_file("roundtrip", frontmatter, body, base=tmp_path)
        defn = _parse_skill_file(path)

        assert defn is not None
        assert defn.name == "roundtrip"
        assert defn.description == "Test roundtrip"
        assert "test" in defn.tags
        assert "Step 1" in defn.content


# ─── Content Hash Tests ─────────────────────────────────────────────


class TestContentHash:
    def test_deterministic(self):
        assert _content_hash("hello") == _content_hash("hello")

    def test_different_content(self):
        assert _content_hash("hello") != _content_hash("world")

    def test_length(self):
        assert len(_content_hash("test")) == 16


# ─── Handler Tests ──────────────────────────────────────────────────


@dataclass(frozen=True)
class _FakeCtx:
    agent_id: str = "test-agent"
    tenant_id: str = "test-tenant"


@pytest.fixture()
def skills_dir(tmp_path: Path):
    """Temporary skills directory with cache invalidation."""
    import robothor.engine.skills as _mod

    _mod._skills_cache = None
    yield tmp_path
    _mod._skills_cache = None


def _patch_skills_dir(skills_dir: Path):
    """Context manager to patch _skills_dir and invalidate cache."""
    import robothor.engine.skills as _mod

    return patch.object(_mod, "_skills_dir", return_value=skills_dir)


class TestCreateSkillHandler:
    @pytest.mark.asyncio
    async def test_create_basic(self, skills_dir: Path):
        from robothor.engine.tools.handlers.skills import _create_skill

        ctx = _FakeCtx()
        with _patch_skills_dir(skills_dir):
            result = await _create_skill(
                {
                    "name": "test-deploy",
                    "description": "Deploy to staging",
                    "content": "## Steps\n\n1. Build\n2. Deploy",
                    "tags": ["devops"],
                },
                ctx,
            )

        assert result["created"] is True
        assert result["name"] == "test-deploy"
        assert (skills_dir / "test-deploy" / "SKILL.md").exists()
        assert (skills_dir / "test-deploy" / "meta.json").exists()

        meta = json.loads((skills_dir / "test-deploy" / "meta.json").read_text())
        assert meta["auto_generated"] is True
        assert meta["created_by"] == "test-agent"
        assert meta["revision"] == 1

    @pytest.mark.asyncio
    async def test_create_invalid_name(self, skills_dir: Path):
        from robothor.engine.tools.handlers.skills import _create_skill

        with _patch_skills_dir(skills_dir):
            result = await _create_skill(
                {"name": "Bad Name!", "description": "x", "content": "y"},
                _FakeCtx(),
            )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_create_missing_description(self, skills_dir: Path):
        from robothor.engine.tools.handlers.skills import _create_skill

        with _patch_skills_dir(skills_dir):
            result = await _create_skill(
                {"name": "test-skill", "description": "", "content": "body"},
                _FakeCtx(),
            )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_create_content_too_long(self, skills_dir: Path):
        from robothor.engine.tools.handlers.skills import _create_skill

        with _patch_skills_dir(skills_dir):
            result = await _create_skill(
                {
                    "name": "test-big",
                    "description": "big",
                    "content": "x" * 11_000,
                },
                _FakeCtx(),
            )
        assert "error" in result
        assert "limit" in result["error"]

    @pytest.mark.asyncio
    async def test_create_refuses_collision_with_hand_authored(self, skills_dir: Path):
        """Cannot overwrite a hand-authored skill without overwrite=true."""
        from robothor.engine.tools.handlers.skills import _create_skill

        # Create a hand-authored skill (no meta.json)
        (skills_dir / "existing").mkdir(parents=True)
        (skills_dir / "existing" / "SKILL.md").write_text(
            "---\nname: existing\ndescription: hand-authored\n---\n\nBody"
        )

        with _patch_skills_dir(skills_dir):
            result = await _create_skill(
                {"name": "existing", "description": "new", "content": "new body"},
                _FakeCtx(),
            )
        assert "error" in result
        assert "hand-authored" in result["error"]

    @pytest.mark.asyncio
    async def test_create_allows_overwrite(self, skills_dir: Path):
        """overwrite=true allows replacing even hand-authored skills."""
        from robothor.engine.tools.handlers.skills import _create_skill

        (skills_dir / "existing").mkdir(parents=True)
        (skills_dir / "existing" / "SKILL.md").write_text(
            "---\nname: existing\ndescription: old\n---\n\nOld body"
        )

        with _patch_skills_dir(skills_dir):
            result = await _create_skill(
                {
                    "name": "existing",
                    "description": "replaced",
                    "content": "new body",
                    "overwrite": True,
                },
                _FakeCtx(),
            )
        assert result["created"] is True


class TestUpdateSkillHandler:
    @pytest.mark.asyncio
    async def test_update_basic(self, skills_dir: Path):
        from robothor.engine.tools.handlers.skills import _create_skill, _update_skill

        ctx = _FakeCtx()
        with _patch_skills_dir(skills_dir):
            await _create_skill(
                {"name": "updatable", "description": "v1", "content": "v1 body"},
                ctx,
            )
            result = await _update_skill(
                {
                    "name": "updatable",
                    "content": "v2 improved body",
                    "reason": "Added error handling",
                },
                ctx,
            )

        assert result.get("error") is None, f"Unexpected error: {result}"
        assert result["updated"] is True
        assert result["revision"] == 2

        # Verify content was updated
        skill_text = (skills_dir / "updatable" / "SKILL.md").read_text()
        assert "v2 improved body" in skill_text

        # Verify meta has revision history
        meta = json.loads((skills_dir / "updatable" / "meta.json").read_text())
        assert meta["revision"] == 2
        assert len(meta["revision_history"]) == 1
        assert meta["revision_history"][0]["reason"] == "Added error handling"

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, skills_dir: Path):
        from robothor.engine.tools.handlers.skills import _update_skill

        with _patch_skills_dir(skills_dir):
            result = await _update_skill(
                {"name": "ghost", "content": "new body"},
                _FakeCtx(),
            )
        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_update_preserves_frontmatter(self, skills_dir: Path):
        """update_skill preserves tags, parameters, etc. from original."""
        from robothor.engine.skills import _parse_skill_file
        from robothor.engine.tools.handlers.skills import _create_skill, _update_skill

        ctx = _FakeCtx()
        with _patch_skills_dir(skills_dir):
            await _create_skill(
                {
                    "name": "tagged",
                    "description": "has tags",
                    "content": "v1",
                    "tags": ["devops", "ci"],
                },
                ctx,
            )
            await _update_skill(
                {"name": "tagged", "content": "v2 body"},
                ctx,
            )

        defn = _parse_skill_file(skills_dir / "tagged" / "SKILL.md")
        assert defn is not None
        assert "devops" in defn.tags
        assert "ci" in defn.tags

    @pytest.mark.asyncio
    async def test_update_can_change_description(self, skills_dir: Path):
        from robothor.engine.skills import _parse_skill_file
        from robothor.engine.tools.handlers.skills import _create_skill, _update_skill

        ctx = _FakeCtx()
        with _patch_skills_dir(skills_dir):
            await _create_skill(
                {"name": "desc-test", "description": "old desc", "content": "body"},
                ctx,
            )
            result = await _update_skill(
                {
                    "name": "desc-test",
                    "content": "new body",
                    "description": "new desc",
                },
                ctx,
            )

        assert result["updated"] is True
        defn = _parse_skill_file(skills_dir / "desc-test" / "SKILL.md")
        assert defn is not None
        assert defn.description == "new desc"


class TestInvokeSkillUsageTracking:
    @pytest.mark.asyncio
    async def test_invoke_increments_usage(self, skills_dir: Path):
        """invoke_skill increments usage_count in meta.json."""
        from robothor.engine.tools.handlers.skills import _invoke_skill

        # Create a skill with meta.json
        fm = {"name": "trackable", "description": "test"}
        from robothor.engine.skills import create_skill_meta, write_skill_file, write_skill_meta

        write_skill_file("trackable", fm, "Do the thing", base=skills_dir)
        write_skill_meta("trackable", create_skill_meta(created_by="test"), base=skills_dir)

        ctx = _FakeCtx()
        with _patch_skills_dir(skills_dir):
            result = await _invoke_skill({"name": "trackable"}, ctx)

        assert "content" in result

        meta = read_skill_meta("trackable", base=skills_dir)
        assert meta is not None
        assert meta["usage_count"] == 1
