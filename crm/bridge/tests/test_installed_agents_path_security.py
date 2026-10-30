"""Filesystem trust-boundary tests for installed-agent readiness."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from routers.installed_agents import (
    _catalog_bundle_for_agent,
    check_readiness,
    list_installed_agents,
)

from robothor.templates.description_optimizer import HubReadinessReport
from robothor.templates.safety import TemplateSecurityError


def test_catalog_lookup_rejects_path_syntax_before_catalog_access() -> None:
    with patch("robothor.templates.catalog.Catalog") as catalog:
        with pytest.raises(TemplateSecurityError, match="agent ID"):
            _catalog_bundle_for_agent("../../etc")
    catalog.assert_not_called()


def test_readiness_rejects_path_injection_without_scoring() -> None:
    with patch("robothor.templates.description_optimizer.score_hub_readiness") as score:
        with pytest.raises(HTTPException) as raised:
            check_readiness("../../etc")
    assert raised.value.status_code == 400
    score.assert_not_called()


def test_readiness_scores_only_the_catalog_discovered_bundle(tmp_path) -> None:
    bundle = tmp_path / "catalog" / "operations" / "safe-agent"
    bundle.mkdir(parents=True)
    report = HubReadinessReport(score=80)

    with (
        patch("routers.installed_agents._catalog_bundle_for_agent", return_value=bundle),
        patch(
            "robothor.templates.description_optimizer.score_hub_readiness",
            return_value=report,
        ) as score,
    ):
        result = check_readiness("safe-agent")

    score.assert_called_once_with(bundle)
    assert result["readiness"] == {
        "score": 80,
        "breakdown": {},
        "issues": [],
        "suggestions": [],
    }


def test_catalog_lookup_returns_only_discovered_non_symlink_bundle(tmp_path) -> None:
    catalog_root = tmp_path / "catalog"
    bundle = catalog_root / "operations" / "safe-agent"
    bundle.mkdir(parents=True)
    (bundle / "setup.yaml").write_text("agent_id: safe-agent\n")
    catalog = MagicMock(catalog_dir=catalog_root)

    with patch("robothor.templates.catalog.Catalog", return_value=catalog):
        assert _catalog_bundle_for_agent("safe-agent") == bundle


def test_catalog_lookup_does_not_follow_matching_symlink(tmp_path) -> None:
    catalog_root = tmp_path / "catalog"
    department = catalog_root / "operations"
    department.mkdir(parents=True)
    outside = tmp_path / "outside" / "safe-agent"
    outside.mkdir(parents=True)
    (department / "safe-agent").symlink_to(outside, target_is_directory=True)
    catalog = MagicMock(catalog_dir=catalog_root)

    with patch("robothor.templates.catalog.Catalog", return_value=catalog):
        assert _catalog_bundle_for_agent("safe-agent") is None


def test_list_skips_mutable_registry_ids_that_are_not_safe_paths(monkeypatch, tmp_path) -> None:
    manifest_root = tmp_path / "manifests"
    manifest_root.mkdir()
    (manifest_root / "safe-agent.yaml").write_text("id: safe-agent\n")
    monkeypatch.setattr("routers.installed_agents.MANIFEST_DIR", manifest_root)
    config = MagicMock(
        installed_agents={
            "../../etc": {},
            "safe-agent": {"version": "1.0.0"},
        }
    )

    with patch("robothor.templates.instance.InstanceConfig.load", return_value=config):
        result = list_installed_agents()

    assert result == {
        "agents": [
            {
                "agent_id": "safe-agent",
                "version": "1.0.0",
                "installed_at": "",
                "source": "",
                "department": "",
                "has_manifest": True,
            }
        ],
        "count": 1,
    }
