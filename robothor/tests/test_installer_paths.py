"""The agent installer must never resolve paths into a wheel's site-packages.

``_find_repo_root()`` in ``robothor.templates.installer`` used to be
``Path(__file__).resolve().parent.parent.parent`` — in a checkout that
happens to land on the repo root, but in a wheel install ``__file__``
resolves inside ``site-packages``, so every agent install/remove/update would
read and write ``docs/agents/`` and ``brain/`` under the installed package
instead of the operator's workspace. The same bare-``__file__`` fallback
existed twice in ``robothor.templates.validators`` for the same reason.

These tests pin the fix:

1. The default resolution never returns (or derives writes from) a path
   under a simulated site-packages tree.
2. ``ROBOTHOR_WORKSPACE`` is respected when set.
3. The dev-checkout layout — where ``templates/agents/_defaults.yaml`` lives
   directly under the resolved root — still works without the fallback.
4. The template *source* lookup (``_find_defaults_path``) falls back to the
   shared resolver introduced in #245 (``robothor.setup._find_template_dir``)
   rather than inventing a third resolution scheme.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import robothor.templates.installer as installer_mod
import robothor.templates.validators as validators_mod
from robothor.templates.safety import default_workspace_root

if TYPE_CHECKING:
    from pathlib import Path


def _fake_site_packages_module_file(tmp_path: Path, module_name: str) -> Path:
    """A site-packages-shaped path for one of our own modules.

    Mirrors a real wheel install: ``.../site-packages/robothor/templates/<module_name>``.
    """
    fake_file = (
        tmp_path
        / "venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "robothor"
        / "templates"
        / module_name
    )
    fake_file.parent.mkdir(parents=True)
    fake_file.write_text("")
    return fake_file


class TestDefaultWorkspaceRoot:
    """The shared helper both installer.py and validators.py now delegate to."""

    def test_respects_robothor_workspace_env(self, monkeypatch, tmp_path):
        workspace = tmp_path / "custom-workspace"
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
        assert default_workspace_root() == workspace

    def test_falls_back_to_home_robothor(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ROBOTHOR_WORKSPACE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        assert default_workspace_root() == tmp_path / "home" / "robothor"


class TestInstallerFindRepoRoot:
    def test_never_resolves_under_simulated_site_packages(self, monkeypatch, tmp_path):
        fake_file = _fake_site_packages_module_file(tmp_path, "installer.py")
        monkeypatch.setattr(installer_mod, "__file__", str(fake_file))
        monkeypatch.delenv("ROBOTHOR_WORKSPACE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        result = installer_mod._find_repo_root()

        site_packages_root = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
        assert result != site_packages_root
        assert site_packages_root not in result.parents

    def test_workspace_env_wins_even_under_simulated_site_packages(self, monkeypatch, tmp_path):
        fake_file = _fake_site_packages_module_file(tmp_path, "installer.py")
        monkeypatch.setattr(installer_mod, "__file__", str(fake_file))
        workspace = tmp_path / "custom-workspace"
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))

        assert installer_mod._find_repo_root() == workspace


class TestInstallerFindDefaultsPath:
    def test_checkout_layout_still_works(self, tmp_path):
        """When repo_root itself carries templates/agents/, prefer it directly."""
        repo_root = tmp_path / "repo"
        defaults = repo_root / "templates" / "agents" / "_defaults.yaml"
        defaults.parent.mkdir(parents=True)
        defaults.write_text("model_primary: checkout\n")

        assert installer_mod._find_defaults_path(repo_root) == defaults

    def test_falls_back_to_shared_template_resolver(self, monkeypatch, tmp_path):
        """A wheel install's workspace has no templates/ — use the #245 resolver."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        bundled = tmp_path / "bundled-templates"
        (bundled / "agents").mkdir(parents=True)
        (bundled / "agents" / "_defaults.yaml").write_text("model_primary: bundled\n")
        monkeypatch.setenv("ROBOTHOR_TEMPLATE_DIR", str(bundled))

        result = installer_mod._find_defaults_path(workspace)

        assert result == bundled / "agents" / "_defaults.yaml"

    def test_returns_none_when_neither_source_has_defaults(self, monkeypatch, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.setattr("robothor.setup._find_template_dir", lambda *a, **k: None)

        assert installer_mod._find_defaults_path(workspace) is None


class TestValidatorsDefaultRepoRoot:
    """validate_post_install / validate_chain_post_install repo_root=None default."""

    def test_post_install_never_resolves_under_simulated_site_packages(self, monkeypatch, tmp_path):
        fake_file = _fake_site_packages_module_file(tmp_path, "validators.py")
        monkeypatch.setattr(validators_mod, "__file__", str(fake_file))
        monkeypatch.delenv("ROBOTHOR_WORKSPACE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        manifest_path = tmp_path / "manifest.yaml"
        manifest_path.write_text("id: test-agent\n")

        captured: dict[str, Path | None] = {}

        def fake_validate_agent(manifest, all_manifests, registered_tools, repo_root=None):
            captured["repo_root"] = repo_root
            return []

        monkeypatch.setattr(
            "robothor.templates.manifest_checks.validate_agent", fake_validate_agent
        )

        validators_mod.validate_post_install(manifest_path, repo_root=None)

        site_packages_root = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
        assert captured["repo_root"] != site_packages_root
        assert site_packages_root not in captured["repo_root"].parents

    def test_post_install_respects_workspace_env(self, monkeypatch, tmp_path):
        workspace = tmp_path / "custom-workspace"
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))

        manifest_path = tmp_path / "manifest.yaml"
        manifest_path.write_text("id: test-agent\n")

        captured: dict[str, Path | None] = {}

        def fake_validate_agent(manifest, all_manifests, registered_tools, repo_root=None):
            captured["repo_root"] = repo_root
            return []

        monkeypatch.setattr(
            "robothor.templates.manifest_checks.validate_agent", fake_validate_agent
        )

        validators_mod.validate_post_install(manifest_path, repo_root=None)

        assert captured["repo_root"] == workspace

    def test_chain_post_install_respects_workspace_env(self, monkeypatch, tmp_path):
        workspace = tmp_path / "custom-workspace"
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))

        manifest_path = tmp_path / "manifest.yaml"
        manifest_path.write_text("id: test-agent\n")

        captured: dict[str, Path | None] = {}

        def fake_validate_chain(manifest, all_manifests, repo_root=None):
            captured["repo_root"] = repo_root
            return []

        monkeypatch.setattr(
            "robothor.templates.chain_validator.validate_chain", fake_validate_chain
        )

        validators_mod.validate_chain_post_install(manifest_path, repo_root=None)

        assert captured["repo_root"] == workspace

    def test_chain_post_install_never_resolves_under_simulated_site_packages(
        self, monkeypatch, tmp_path
    ):
        fake_file = _fake_site_packages_module_file(tmp_path, "validators.py")
        monkeypatch.setattr(validators_mod, "__file__", str(fake_file))
        monkeypatch.delenv("ROBOTHOR_WORKSPACE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        manifest_path = tmp_path / "manifest.yaml"
        manifest_path.write_text("id: test-agent\n")

        captured: dict[str, Path | None] = {}

        def fake_validate_chain(manifest, all_manifests, repo_root=None):
            captured["repo_root"] = repo_root
            return []

        monkeypatch.setattr(
            "robothor.templates.chain_validator.validate_chain", fake_validate_chain
        )

        validators_mod.validate_chain_post_install(manifest_path, repo_root=None)

        site_packages_root = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
        assert captured["repo_root"] != site_packages_root
        assert site_packages_root not in captured["repo_root"].parents


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
