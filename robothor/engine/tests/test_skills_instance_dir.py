"""Agent-created skills live in the instance, not the platform tree.

``agents/skills/`` is platform code: tracked in git, the bundled skills
every instance gets. Until this change every runtime write -- create,
update, archive -- landed there too, so an instance's own skills sat as
untracked directories inside the platform tree: one ``git add -A`` away
from being committed, and deleted by any clean checkout.

Writes now go to ``<workspace>/brain/skills`` (gitignored, instance-land
like the rest of ``brain/``); reads see the bundled directory first and
the instance directory second, so an instance skill wins a name
collision and a bundled skill is never overwritten in place.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


class _FakeCtx:
    agent_id = "test-agent"
    tenant_id = "test-tenant"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A tmp workspace with both skill trees, wired end to end."""
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    import robothor.engine.skills as _mod
    from robothor.settings import reset_settings

    reset_settings()
    _mod._skills_cache = None
    _mod._meta_cache.clear()
    _mod._state_cache.clear()
    (tmp_path / "agents" / "skills").mkdir(parents=True)
    yield tmp_path
    _mod._skills_cache = None
    _mod._meta_cache.clear()
    _mod._state_cache.clear()
    reset_settings()


def _write_skill(base: Path, name: str, *, body: str = "body", meta: dict | None = None) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} desc\n---\n\n{body}\n")
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta))
    return d


# ── The resolver ─────────────────────────────────────────────────────


class TestResolver:
    def test_instance_dir_defaults_under_brain(self, workspace):
        from robothor.engine.skills import instance_skills_dir

        assert instance_skills_dir() == workspace / "brain" / "skills"

    def test_bundled_dir_is_the_platform_tree(self, workspace):
        from robothor.engine.skills import bundled_skills_dir

        assert bundled_skills_dir() == workspace / "agents" / "skills"

    def test_env_override_wins(self, workspace, monkeypatch):
        from robothor.engine.skills import instance_skills_dir
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_INSTANCE_SKILLS_DIR", str(workspace / "elsewhere"))
        reset_settings()
        assert instance_skills_dir() == workspace / "elsewhere"

    def test_the_setting_is_declared(self):
        from robothor.settings.registry import field_index

        assert "ROBOTHOR_INSTANCE_SKILLS_DIR" in field_index()

    def test_search_order_puts_the_instance_last(self, workspace):
        from robothor.engine.skills import (
            bundled_skills_dir,
            instance_skills_dir,
            skill_search_paths,
        )

        assert skill_search_paths() == (bundled_skills_dir(), instance_skills_dir())


# ── The origin marker ────────────────────────────────────────────────


class TestOrigin:
    def test_create_meta_stamps_instance_origin(self):
        from robothor.engine.skills import INSTANCE_ORIGIN, create_skill_meta

        assert create_skill_meta(created_by="main")["origin"] == INSTANCE_ORIGIN

    def test_explicit_platform_origin_beats_legacy_markers(self):
        from robothor.engine.skills import PLATFORM_ORIGIN, is_instance_skill_meta, skill_origin

        meta = {"origin": PLATFORM_ORIGIN, "auto_generated": True, "created_by": "main"}
        assert skill_origin(meta) == PLATFORM_ORIGIN
        assert not is_instance_skill_meta(meta)

    @pytest.mark.parametrize(
        "meta",
        [
            {"auto_generated": True},
            {"write_origin": "foreground"},
            {"is_agent_created": True},
        ],
    )
    def test_legacy_runtime_markers_read_as_instance(self, meta):
        from robothor.engine.skills import is_instance_skill_meta

        assert is_instance_skill_meta(meta)

    @pytest.mark.parametrize("meta", [None, {}, {"revision": 3}])
    def test_meta_without_a_marker_is_platform(self, meta):
        from robothor.engine.skills import PLATFORM_ORIGIN, skill_origin

        assert skill_origin(meta) == PLATFORM_ORIGIN


# ── Writes ───────────────────────────────────────────────────────────


class TestWritesLandInTheInstance:
    @pytest.mark.asyncio
    async def test_create_skill_never_touches_the_platform_tree(self, workspace):
        from robothor.engine.tools.handlers.skills import _create_skill

        result = await _create_skill(
            {"name": "deploy-staging", "description": "Deploy", "content": "## Steps\n1. go"},
            _FakeCtx(),
        )

        assert result["created"] is True
        instance = workspace / "brain" / "skills" / "deploy-staging"
        assert (instance / "SKILL.md").exists()
        assert (instance / "meta.json").exists()
        assert (instance / "state.json").exists()
        assert not (workspace / "agents" / "skills" / "deploy-staging").exists()
        meta = json.loads((instance / "meta.json").read_text())
        assert meta["origin"] == "instance"

    @pytest.mark.asyncio
    async def test_update_of_a_bundled_skill_copies_into_the_instance(self, workspace):
        from robothor.engine.tools.handlers.skills import _update_skill

        bundled = _write_skill(
            workspace / "agents" / "skills",
            "crm-lookup",
            body="platform body",
            meta={"origin": "platform", "revision": 4},
        )
        before = (bundled / "SKILL.md").read_bytes()

        result = await _update_skill(
            {"name": "crm-lookup", "content": "improved body", "reason": "learned something"},
            _FakeCtx(),
        )

        assert result["updated"] is True
        assert (bundled / "SKILL.md").read_bytes() == before, "the platform file was rewritten"
        overlay = workspace / "brain" / "skills" / "crm-lookup"
        assert "improved body" in (overlay / "SKILL.md").read_text()
        assert json.loads((overlay / "meta.json").read_text())["revision"] == 5

    def test_increment_usage_writes_state_into_the_instance(self, workspace):
        from robothor.engine.skills import increment_usage, read_skill_view

        _write_skill(workspace / "agents" / "skills", "health-check", meta={"revision": 1})
        increment_usage("health-check")

        assert (workspace / "brain" / "skills" / "health-check" / "state.json").exists()
        assert not (workspace / "agents" / "skills" / "health-check" / "state.json").exists()
        view = read_skill_view("health-check")
        assert view is not None
        assert view["usage_count"] == 1
        assert view["revision"] == 1, "bundled meta.json must still be merged in"


# ── Reads ────────────────────────────────────────────────────────────


class TestReadsSeeBothTrees:
    def test_both_trees_are_loaded(self, workspace):
        from robothor.engine.skills import load_skills

        _write_skill(workspace / "agents" / "skills", "bundled-one")
        _write_skill(workspace / "brain" / "skills", "instance-one")

        names = set(load_skills())
        assert {"bundled-one", "instance-one"} <= names

    def test_instance_wins_a_name_collision(self, workspace):
        from robothor.engine.skills import load_skills

        _write_skill(workspace / "agents" / "skills", "shared", body="platform body")
        _write_skill(workspace / "brain" / "skills", "shared", body="instance body")

        assert "instance body" in load_skills()["shared"].content

    def test_lifecycle_pass_sees_instance_skills(self, workspace):
        from datetime import UTC, datetime, timedelta

        from robothor.engine.skills import apply_skill_lifecycle

        old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
        _write_skill(
            workspace / "brain" / "skills",
            "cold-instance-skill",
            meta={"origin": "instance", "created_at": old},
        )
        report = apply_skill_lifecycle()
        assert "cold-instance-skill" in report["archived"]


# ── Archive / retirement ─────────────────────────────────────────────


class TestArchive:
    @staticmethod
    async def _archive(name):
        from robothor.engine.tools.handlers.skills import _skill_archive

        return await _skill_archive({"name": name}, _FakeCtx())

    @pytest.mark.asyncio
    async def test_archives_into_the_instance_tree(self, workspace):
        _write_skill(
            workspace / "brain" / "skills",
            "cold-one",
            meta={"origin": "instance", "is_agent_created": True},
        )
        result = await self._archive("cold-one")

        assert result.get("archived") == "cold-one"
        assert not (workspace / "brain" / "skills" / "cold-one").exists()
        assert (workspace / "brain" / "skills" / ".archive" / "cold-one" / "SKILL.md").exists()

    @pytest.mark.asyncio
    async def test_a_stray_in_the_platform_tree_is_retired_out_of_it(self, workspace):
        """The pre-migration case: an agent-created skill still in agents/skills."""
        _write_skill(
            workspace / "agents" / "skills",
            "stray-one",
            meta={"auto_generated": True, "created_by": "main"},
        )
        result = await self._archive("stray-one")

        assert result.get("archived") == "stray-one"
        assert not (workspace / "agents" / "skills" / "stray-one").exists()
        assert (workspace / "brain" / "skills" / ".archive" / "stray-one" / "SKILL.md").exists()

    @pytest.mark.asyncio
    async def test_refuses_a_platform_bundled_skill(self, workspace):
        _write_skill(
            workspace / "agents" / "skills",
            "code-review",
            meta={"origin": "platform", "auto_generated": True},
        )
        result = await self._archive("code-review")

        assert "error" in result
        assert (workspace / "agents" / "skills" / "code-review").exists()


# ── The one-shot migration ───────────────────────────────────────────


class TestMigration:
    def test_moves_an_agent_created_skill_and_is_idempotent(self, workspace):
        from robothor.engine.skills import migrate_instance_skills

        _write_skill(
            workspace / "agents" / "skills",
            "inert-guard-detection",
            meta={"auto_generated": True, "created_by": "auto-agent"},
        )
        (workspace / "agents" / "skills" / "inert-guard-detection" / "state.json").write_text(
            json.dumps({"usage_count": 3, "last_used": None})
        )

        first = migrate_instance_skills()
        assert first["moved"] == ["inert-guard-detection"]
        moved = workspace / "brain" / "skills" / "inert-guard-detection"
        assert (moved / "SKILL.md").exists()
        assert (moved / "meta.json").exists()
        assert json.loads((moved / "state.json").read_text())["usage_count"] == 3
        assert not (workspace / "agents" / "skills" / "inert-guard-detection").exists()

        second = migrate_instance_skills()
        assert second["moved"] == []

    def test_leaves_a_platform_skill_alone(self, workspace):
        from robothor.engine.skills import migrate_instance_skills

        _write_skill(
            workspace / "agents" / "skills",
            "crm-lookup",
            meta={"origin": "platform", "auto_generated": True},
        )
        result = migrate_instance_skills()
        assert result["moved"] == []
        assert result["skipped"] == ["crm-lookup"]
        assert (workspace / "agents" / "skills" / "crm-lookup" / "SKILL.md").exists()

    def test_a_skill_without_meta_is_left_alone(self, workspace):
        from robothor.engine.skills import migrate_instance_skills

        _write_skill(workspace / "agents" / "skills", "batch")
        assert migrate_instance_skills()["moved"] == []
        assert (workspace / "agents" / "skills" / "batch" / "SKILL.md").exists()

    def test_a_collision_is_reported_not_overwritten(self, workspace):
        from robothor.engine.skills import migrate_instance_skills

        _write_skill(
            workspace / "agents" / "skills",
            "dup",
            body="stray",
            meta={"auto_generated": True},
        )
        _write_skill(workspace / "brain" / "skills", "dup", body="already here")

        result = migrate_instance_skills()
        assert result["conflicts"] == ["dup"]
        assert "already here" in (workspace / "brain" / "skills" / "dup" / "SKILL.md").read_text()
        assert (workspace / "agents" / "skills" / "dup" / "SKILL.md").exists()

    def test_dry_run_moves_nothing(self, workspace):
        from robothor.engine.skills import migrate_instance_skills

        _write_skill(workspace / "agents" / "skills", "stray", meta={"auto_generated": True})
        result = migrate_instance_skills(dry_run=True)
        assert result["moved"] == ["stray"]
        assert (workspace / "agents" / "skills" / "stray" / "SKILL.md").exists()
        assert not (workspace / "brain" / "skills" / "stray").exists()

    def test_the_retirement_archive_moves_too(self, workspace):
        from robothor.engine.skills import migrate_instance_skills

        _write_skill(
            workspace / "agents" / "skills" / ".archive",
            "retired-one",
            meta={"auto_generated": True},
        )
        result = migrate_instance_skills()
        assert result["moved"] == [".archive/retired-one"]
        assert (workspace / "brain" / "skills" / ".archive" / "retired-one" / "SKILL.md").exists()

    def test_the_cli_runs_it(self, workspace, capsys):
        import argparse

        from robothor.cli.skills import cmd_skills

        _write_skill(workspace / "agents" / "skills", "stray", meta={"auto_generated": True})
        rc = cmd_skills(argparse.Namespace(skills_command="migrate-instance", dry_run=False))
        assert rc == 0
        assert "stray" in capsys.readouterr().out
        assert (workspace / "brain" / "skills" / "stray" / "SKILL.md").exists()


# ── Nothing writes to the real workspace ─────────────────────────────


def test_no_write_helper_defaults_to_the_platform_tree(workspace):
    """Every write helper resolves to the instance dir when base is None."""
    from robothor.engine.skills import (
        _skill_write_path,
        _state_write_path,
        instance_skills_dir,
    )

    with patch.object(Path, "mkdir"):
        assert _skill_write_path("x").parent.parent == instance_skills_dir()
        assert _state_write_path("x").parent.parent == instance_skills_dir()
