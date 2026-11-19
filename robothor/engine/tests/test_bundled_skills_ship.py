"""The platform's bundled skills ship with the code, not only with a checkout.

Bundled skills were read from ``<workspace>/agents/skills`` and nowhere
else. A hosted engine whose image has no ``agents/skills`` directory, running
against a fresh workspace, therefore had no bundled skills at all: every
queued pull-request review failed with "the pr-review skill is not
installed", and the only way out was copying the skill into the workspace by
hand.

The skills now ship inside the wheel (``robothor/bundled_skills``, built from
``agents/skills`` by the build hook) and the image, and the lookup falls back
to that copy for any name the workspace does not have. The layering is per
skill, lowest precedence first: the copy shipped with the code, then the
workspace's ``agents/skills``, then the instance's own ``brain/skills``. A
workspace or instance copy still wins its name.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A fresh workspace: no agents/skills, no brain/skills."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(ws))
    monkeypatch.delenv("ROBOTHOR_INSTANCE_SKILLS_DIR", raising=False)
    import robothor.engine.skills as _mod
    from robothor.settings import reset_settings

    reset_settings()
    _mod._skills_cache = None
    _mod._meta_cache.clear()
    _mod._state_cache.clear()
    yield ws
    _mod._skills_cache = None
    _mod._meta_cache.clear()
    _mod._state_cache.clear()
    reset_settings()


def _write_skill(base: Path, name: str, body: str) -> None:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} desc\n---\n\n{body}\n")


# ── The lookup ───────────────────────────────────────────────────────


class TestAWorkspaceWithoutAgentsSkills:
    def test_a_workspace_without_agents_skills_still_loads_a_bundled_skill(self, workspace):
        from robothor.engine.skills import get_skill_content, load_skills

        assert not (workspace / "agents").exists()
        assert "pr-review" in load_skills()
        content = get_skill_content("pr-review")
        assert content and "review" in content.lower()

    def test_the_shipped_skill_counts_as_bundled(self, workspace):
        from robothor.engine.skills import bundled_skill_exists, resolve_skill_dir

        assert bundled_skill_exists("pr-review")
        found = resolve_skill_dir("pr-review")
        assert found is not None and (found / "SKILL.md").is_file()

    def test_its_sidecar_files_are_readable_too(self, workspace):
        from robothor.engine.skills import read_skill_meta

        assert read_skill_meta("pr-review") is not None

    def test_the_instance_copy_still_wins(self, workspace):
        from robothor.engine.skills import get_skill_content, shadows_bundled

        _write_skill(workspace / "brain" / "skills", "pr-review", "the instance's own procedure")
        assert get_skill_content("pr-review") == "the instance's own procedure"
        assert shadows_bundled("pr-review")

    def test_a_workspace_copy_still_wins(self, workspace):
        from robothor.engine.skills import get_skill_content

        _write_skill(workspace / "agents" / "skills", "pr-review", "the workspace's copy")
        assert get_skill_content("pr-review") == "the workspace's copy"

    def test_a_partial_workspace_tree_does_not_hide_the_rest(self, workspace):
        # A bundle install writes agents/skills/<name> into the workspace; the
        # shipped skills it does not carry must stay visible.
        from robothor.engine.skills import get_skill_content

        _write_skill(workspace / "agents" / "skills", "only-this-one", "x")
        assert get_skill_content("only-this-one") == "x"
        assert get_skill_content("pr-review")

    def test_writes_never_land_in_the_shipped_copy(self, workspace):
        from robothor.engine.skills import instance_skills_dir

        assert instance_skills_dir() == workspace / "brain" / "skills"


class TestWhereTheShippedCopyIsFound:
    def test_a_wheel_install_reads_the_package_data(self, tmp_path):
        from robothor.engine.skills import platform_skills_dir

        package = tmp_path / "site-packages" / "robothor"
        _write_skill(package / "bundled_skills", "pr-review", "packaged")
        assert platform_skills_dir(package) == package / "bundled_skills"

    def test_a_checkout_reads_its_own_agents_skills(self, tmp_path):
        from robothor.engine.skills import platform_skills_dir

        root = tmp_path / "checkout"
        (root / "robothor").mkdir(parents=True)
        (root / "pyproject.toml").write_text("")
        _write_skill(root / "agents" / "skills", "pr-review", "checkout")
        assert platform_skills_dir(root / "robothor") == root / "agents" / "skills"

    def test_neither_is_none(self, tmp_path):
        from robothor.engine.skills import platform_skills_dir

        package = tmp_path / "site-packages" / "robothor"
        package.mkdir(parents=True)
        assert platform_skills_dir(package) is None


# ── The wheel ────────────────────────────────────────────────────────


def _hook_module():
    spec = importlib.util.spec_from_file_location("hatch_build", REPO / "hatch_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheWheelCarriesTheSkills:
    def test_every_tracked_bundled_skill_file_is_mapped_into_the_package(self):
        include = _hook_module().compute_skill_force_include(REPO)
        targets = set(include.values())
        for name in ("SKILL.md", "meta.json", "schema.json"):
            assert f"robothor/bundled_skills/pr-review/{name}" in targets
        shipped = {p.parent.name for p in (REPO / "agents" / "skills").glob("*/SKILL.md")}
        assert shipped
        for skill in shipped:
            assert f"robothor/bundled_skills/{skill}/SKILL.md" in targets

    def test_runtime_sidecars_and_instance_skills_never_ship(self, tmp_path):
        root = tmp_path
        skills = root / "agents" / "skills"
        _write_skill(skills, "shipped", "x")
        (skills / "shipped" / "meta.json").write_text(json.dumps({"origin": "platform"}))
        (skills / "shipped" / "state.json").write_text("{}")
        (skills / "shipped" / "meta.json.tmp").write_text("{}")
        _write_skill(skills, "learned", "y")
        (skills / "learned" / "meta.json").write_text(json.dumps({"origin": "instance"}))
        _write_skill(skills, "legacy", "z")
        (skills / "legacy" / "meta.json").write_text(json.dumps({"auto_generated": True}))

        targets = set(_hook_module().compute_skill_force_include(root).values())
        assert targets == {
            "robothor/bundled_skills/shipped/SKILL.md",
            "robothor/bundled_skills/shipped/meta.json",
        }

    def test_the_build_hook_adds_them(self, tmp_path):
        hook = _hook_module()
        skills = tmp_path / "agents" / "skills"
        _write_skill(skills, "shipped", "x")

        class _Hook(hook.CustomBuildHook):
            def __init__(self, root):
                self.root = str(root)

        build_data: dict = {}
        _Hook(tmp_path).initialize("standard", build_data)
        assert "robothor/bundled_skills/shipped/SKILL.md" in build_data["force_include"].values()


# ── Export ───────────────────────────────────────────────────────────


class TestExportCarriesWhatTheInstanceOwns:
    def test_an_export_leaves_out_the_copy_shipped_with_the_code(self, workspace, tmp_path):
        # Exported skills are imported as instance overlays; a shipped skill in
        # an export would pin that version on the receiving instance.
        from robothor.cli.exporter import _export_skills

        _write_skill(workspace / "brain" / "skills", "mine", "the instance's own")
        out = tmp_path / "export"
        out.mkdir()
        assert _export_skills(out) == 1
        assert sorted(p.name for p in (out / "skills").iterdir()) == ["mine"]
