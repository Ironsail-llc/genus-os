"""The init scaffold must resolve from every install layout.

``_find_template_dir()`` used to check the Python code package
(``robothor/templates/`` — modules plus one stray markdown file) FIRST, so
the real repo-root ``templates/`` scaffold was dead code even in a dev
checkout, and a wheel install scaffolded a single file. ``create_workspace``
then silently skipped every missing template.

These tests pin the resolution order:

1. ``ROBOTHOR_TEMPLATE_DIR`` env override
2. repo-root ``templates/`` in a checkout (marker-detected, not
   parent-count arithmetic)
3. the wheel-bundled ``robothor/templates/bundled_scaffold``
   (never the bare code-package dir)

and make ``create_workspace`` warn — not silently skip — when an expected
scaffold file is missing.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

from robothor.setup import EXPECTED_SCAFFOLD_FILES, _find_template_dir, create_workspace

REPO = Path(__file__).resolve().parents[2]
SCAFFOLD = REPO / "templates"


@pytest.fixture(autouse=True)
def _no_ambient_override(monkeypatch):
    """Tests control the env override explicitly."""
    monkeypatch.delenv("ROBOTHOR_TEMPLATE_DIR", raising=False)


def _make_wheel_layout(root: Path) -> Path:
    """A site-packages-shaped tree: no pyproject.toml in any ancestor."""
    pkg = root / "site-packages" / "robothor"
    (pkg / "templates" / "bundled_scaffold").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "templates" / "__init__.py").write_text("")
    (pkg / "templates" / "bundled_scaffold" / "SOUL.md").write_text("# SOUL\n")
    return pkg


def _make_checkout_layout(root: Path) -> Path:
    """A repo-shaped tree: pyproject.toml + templates/ + robothor/ package."""
    repo = root / "repo"
    (repo / "templates").mkdir(parents=True)
    (repo / "templates" / "SOUL.md").write_text("# checkout SOUL\n")
    (repo / "pyproject.toml").write_text("[project]\nname = 'genus-os'\n")
    pkg = repo / "robothor"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    # A bundled copy may also exist (e.g. editable install) — the checkout
    # scaffold must still win.
    (pkg / "templates" / "bundled_scaffold").mkdir(parents=True)
    (pkg / "templates" / "bundled_scaffold" / "SOUL.md").write_text("# bundled SOUL\n")
    return pkg


class TestResolutionOrder:
    def test_wheel_layout_resolves_to_bundled_scaffold(self, tmp_path):
        pkg = _make_wheel_layout(tmp_path)
        resolved = _find_template_dir(package_dir=pkg)
        assert resolved == pkg / "templates" / "bundled_scaffold"

    def test_checkout_layout_prefers_repo_root_templates(self, tmp_path):
        pkg = _make_checkout_layout(tmp_path)
        resolved = _find_template_dir(package_dir=pkg)
        assert resolved == tmp_path / "repo" / "templates"

    def test_env_override_wins_over_everything(self, tmp_path, monkeypatch):
        pkg = _make_checkout_layout(tmp_path)
        custom = tmp_path / "custom-templates"
        custom.mkdir()
        (custom / "SOUL.md").write_text("# custom SOUL\n")
        monkeypatch.setenv("ROBOTHOR_TEMPLATE_DIR", str(custom))
        assert _find_template_dir(package_dir=pkg) == custom

    def test_bare_code_package_dir_is_never_returned(self, tmp_path):
        """A robothor/templates/ dir without bundled_scaffold/ resolves to None."""
        pkg = tmp_path / "site-packages" / "robothor"
        (pkg / "templates").mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "templates" / "__init__.py").write_text("")
        (pkg / "templates" / "wizard.py").write_text("")
        assert _find_template_dir(package_dir=pkg) is None

    def test_default_resolution_finds_this_checkout(self):
        resolved = _find_template_dir()
        assert resolved == SCAFFOLD


class TestScaffoldManifest:
    def test_manifest_files_all_exist_in_the_scaffold(self):
        missing = [name for name in EXPECTED_SCAFFOLD_FILES if not (SCAFFOLD / name).is_file()]
        assert missing == [], f"expected scaffold files missing from templates/: {missing}"

    def test_manifest_is_not_trivially_small(self):
        assert len(EXPECTED_SCAFFOLD_FILES) >= 13

    def test_bundled_scaffold_is_force_included_in_the_wheel(self):
        cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
        include = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        assert include.get("templates") == "robothor/templates/bundled_scaffold"


class TestCreateWorkspace:
    def test_scaffolds_expected_files(self, tmp_path, monkeypatch):
        workspace = tmp_path / "workspace"
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
        monkeypatch.setenv("ROBOTHOR_TEMPLATE_DIR", str(SCAFFOLD))
        create_workspace(workspace)
        scaffolded = [p for p in workspace.rglob("*") if p.is_file()]
        assert len(scaffolded) >= len(EXPECTED_SCAFFOLD_FILES)
        assert (workspace / "brain" / "SOUL.md").is_file()
        assert (workspace / "brain" / "CLAUDE.md").is_file()  # from brain-CLAUDE.md
        assert (workspace / "CLAUDE.md").is_file()
        assert (workspace / "AGENT_BUILDER.md").is_file()
        assert (workspace / "docs" / "agents" / "agent-manifest.yaml").is_file()

    def test_warns_on_missing_scaffold_file(self, tmp_path, monkeypatch, capsys):
        partial = tmp_path / "partial-templates"
        shutil.copytree(SCAFFOLD, partial, ignore=shutil.ignore_patterns(".git"))
        (partial / "SOUL.md").unlink()
        workspace = tmp_path / "workspace"
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
        monkeypatch.setenv("ROBOTHOR_TEMPLATE_DIR", str(partial))
        create_workspace(workspace)
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "SOUL.md" in combined
        assert "arning" in combined  # Warning/warning

    def test_warns_when_no_scaffold_found_at_all(self, tmp_path, monkeypatch, capsys):
        pkg = tmp_path / "site-packages" / "robothor"
        (pkg / "templates").mkdir(parents=True)
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "ws"))
        monkeypatch.setenv("ROBOTHOR_TEMPLATE_DIR", str(tmp_path / "does-not-exist"))
        create_workspace(tmp_path / "ws")
        out = capsys.readouterr()
        assert "arning" in (out.out + out.err)
