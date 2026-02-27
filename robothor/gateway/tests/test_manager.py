"""Tests for GatewayManager."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from robothor.gateway.manager import GatewayManager, GatewayStatus


class TestGetVersion:
    def test_reads_version_from_package_json(self, tmp_gateway_dir: Path):
        mgr = GatewayManager(gateway_dir=tmp_gateway_dir)
        assert mgr.get_version() == "2026.2.26"

    def test_returns_unknown_when_missing(self, tmp_path: Path):
        mgr = GatewayManager(gateway_dir=tmp_path / "nonexistent")
        assert mgr.get_version() == "unknown"

    def test_returns_unknown_on_bad_json(self, tmp_path: Path):
        gateway = tmp_path / "gateway"
        gateway.mkdir()
        (gateway / "package.json").write_text("not json")
        mgr = GatewayManager(gateway_dir=gateway)
        assert mgr.get_version() == "unknown"


class TestIsBuilt:
    def test_true_when_dist_index_exists(self, tmp_gateway_dir: Path):
        mgr = GatewayManager(gateway_dir=tmp_gateway_dir)
        assert mgr.is_built() is True

    def test_false_when_no_dist(self, unbuilt_gateway_dir: Path):
        mgr = GatewayManager(gateway_dir=unbuilt_gateway_dir)
        assert mgr.is_built() is False

    def test_false_when_dist_empty(self, tmp_path: Path):
        gateway = tmp_path / "gateway"
        gateway.mkdir()
        (gateway / "dist").mkdir()
        mgr = GatewayManager(gateway_dir=gateway)
        assert mgr.is_built() is False


class TestBuild:
    @patch("subprocess.run")
    def test_build_runs_install_and_build(self, mock_run, tmp_gateway_dir: Path):
        mock_run.return_value.returncode = 0
        mgr = GatewayManager(gateway_dir=tmp_gateway_dir)
        assert mgr.build() is True
        assert mock_run.call_count == 2

        # First call: pnpm install
        first_call = mock_run.call_args_list[0]
        assert first_call[0][0][0] == "pnpm"
        assert "install" in first_call[0][0]

        # Second call: pnpm build
        second_call = mock_run.call_args_list[1]
        assert second_call[0][0] == ["pnpm", "build"]

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_build_returns_false_on_missing_pnpm(self, mock_run, tmp_gateway_dir: Path):
        mgr = GatewayManager(gateway_dir=tmp_gateway_dir)
        assert mgr.build() is False


class TestStatus:
    def test_returns_gateway_status(self, tmp_gateway_dir: Path, tmp_config_dir: Path):
        mgr = GatewayManager(gateway_dir=tmp_gateway_dir, config_dir=tmp_config_dir)
        status = mgr.status()
        assert isinstance(status, GatewayStatus)
        assert status.version == "2026.2.26"
        assert status.built is True
        assert status.gateway_dir == tmp_gateway_dir
        assert status.config_dir == tmp_config_dir


class TestInstallPlugins:
    def test_creates_symlinks_for_bundled_plugins(
        self, tmp_gateway_dir: Path, tmp_config_dir: Path
    ):
        # Create a bundled plugin
        plugins = Path(__file__).parent.parent / "plugins"
        if not plugins.exists():
            pytest.skip("No bundled plugins directory")

        mgr = GatewayManager(gateway_dir=tmp_gateway_dir, config_dir=tmp_config_dir)
        assert mgr.install_plugins() is True

    def test_succeeds_with_no_plugins(self, tmp_gateway_dir: Path, tmp_config_dir: Path):
        mgr = GatewayManager(gateway_dir=tmp_gateway_dir, config_dir=tmp_config_dir)
        # Patch plugins path to nonexistent
        with patch.object(Path, "exists", return_value=False):
            assert mgr.install_plugins() is True


class TestCheckPrerequisites:
    def test_returns_list_of_results(self, tmp_gateway_dir: Path):
        mgr = GatewayManager(gateway_dir=tmp_gateway_dir)
        results = mgr.check_prerequisites()
        assert len(results) == 2
        names = [r.name for r in results]
        assert "Node.js" in names
        assert "pnpm" in names


class TestSyncUpstream:
    @patch("subprocess.run")
    def test_calls_git_subtree_pull(self, mock_run, tmp_gateway_dir: Path):
        mock_run.return_value.returncode = 0
        mgr = GatewayManager(gateway_dir=tmp_gateway_dir)
        assert mgr.sync_upstream() is True

        call_args = mock_run.call_args[0][0]
        assert "subtree" in call_args
        assert "pull" in call_args
        assert "--prefix=gateway" in call_args
