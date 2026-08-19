"""Tests for the template catalog system."""

import logging
from pathlib import Path

import pytest
import yaml

from robothor.templates.catalog import Catalog, _find_catalog_dir


@pytest.fixture
def tmp_catalog_dir(tmp_path):
    """Create a minimal catalog structure."""
    catalog_dir = tmp_path / "agents"
    catalog_dir.mkdir()

    # _catalog.yaml
    catalog_data = {
        "departments": {
            "email": {
                "name": "Email Pipeline",
                "description": "Classify and respond to emails",
                "agents": ["email-classifier", "email-responder"],
            },
            "ops": {
                "name": "Operations",
                "description": "Monitoring and testing",
                "agents": ["canary"],
            },
        },
        "presets": {
            "minimal": {
                "description": "Just the canary",
                "agents": ["canary"],
            },
            "full": {
                "description": "All agents",
                "agents": "all",
            },
        },
    }
    (catalog_dir / "_catalog.yaml").write_text(yaml.dump(catalog_data, default_flow_style=False))

    # _defaults.yaml
    defaults = {
        "model_primary": "openrouter/xiaomi/mimo-v2-pro",
        "timezone": "UTC",
    }
    (catalog_dir / "_defaults.yaml").write_text(yaml.dump(defaults, default_flow_style=False))

    # Template bundle for canary
    ops_dir = catalog_dir / "ops" / "canary"
    ops_dir.mkdir(parents=True)
    (ops_dir / "setup.yaml").write_text(
        yaml.dump({"agent_id": "canary", "version": "1.0.0", "variables": {}})
    )

    return catalog_dir


class TestCatalog:
    def test_list_departments(self, tmp_catalog_dir):
        catalog = Catalog(tmp_catalog_dir)
        depts = catalog.list_departments()
        assert len(depts) == 2
        names = {d["id"] for d in depts}
        assert names == {"email", "ops"}

    def test_list_presets(self, tmp_catalog_dir):
        catalog = Catalog(tmp_catalog_dir)
        presets = catalog.list_presets()
        assert len(presets) == 2
        ids = {p["id"] for p in presets}
        assert ids == {"minimal", "full"}

    def test_get_preset_agents(self, tmp_catalog_dir):
        catalog = Catalog(tmp_catalog_dir)
        agents = catalog.get_preset_agents("minimal")
        assert agents == ["canary"]

    def test_get_preset_all(self, tmp_catalog_dir):
        catalog = Catalog(tmp_catalog_dir)
        agents = catalog.get_preset_agents("full")
        assert set(agents) == {"email-classifier", "email-responder", "canary"}

    def test_get_department_agents(self, tmp_catalog_dir):
        catalog = Catalog(tmp_catalog_dir)
        agents = catalog.get_department_agents("email")
        assert agents == ["email-classifier", "email-responder"]

    def test_get_department_nonexistent(self, tmp_catalog_dir):
        catalog = Catalog(tmp_catalog_dir)
        agents = catalog.get_department_agents("nonexistent")
        assert agents == []

    def test_find_template(self, tmp_catalog_dir):
        catalog = Catalog(tmp_catalog_dir)
        path = catalog.find_template("canary")
        assert path is not None
        assert path.name == "canary"

    def test_find_template_not_found(self, tmp_catalog_dir):
        catalog = Catalog(tmp_catalog_dir)
        path = catalog.find_template("nonexistent")
        assert path is None

    def test_defaults(self, tmp_catalog_dir):
        catalog = Catalog(tmp_catalog_dir)
        assert catalog.defaults["model_primary"] == "openrouter/xiaomi/mimo-v2-pro"
        assert catalog.defaults["timezone"] == "UTC"

    def test_list_available_templates(self, tmp_catalog_dir):
        catalog = Catalog(tmp_catalog_dir)
        templates = catalog.list_available_templates()
        assert len(templates) == 1
        assert templates[0]["id"] == "canary"
        assert templates[0]["department"] == "ops"


def _make_wheel_layout(root: Path) -> Path:
    """A site-packages-shaped tree: no pyproject.toml in any ancestor."""
    pkg = root / "site-packages" / "robothor"
    (pkg / "templates").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "templates" / "__init__.py").write_text("")
    return pkg


class TestCatalogDirResolution:
    """`_find_catalog_dir` must resolve from a wheel install and warn, never
    silently point at a directory that isn't there."""

    @pytest.fixture(autouse=True)
    def _no_ambient_override(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_TEMPLATE_DIR", raising=False)

    def test_catalog_non_empty_from_simulated_wheel_layout(self, tmp_path):
        pkg = _make_wheel_layout(tmp_path)
        agents_dir = pkg / "templates" / "bundled_scaffold" / "agents"
        bundle = agents_dir / "ops" / "canary"
        bundle.mkdir(parents=True)
        (bundle / "setup.yaml").write_text(
            yaml.dump({"agent_id": "canary", "version": "1.0.0", "variables": {}})
        )
        (agents_dir / "_catalog.yaml").write_text(
            yaml.dump(
                {
                    "departments": {"ops": {"name": "Ops", "agents": ["canary"]}},
                    "presets": {"minimal": {"description": "just canary", "agents": ["canary"]}},
                }
            )
        )

        resolved = _find_catalog_dir(package_dir=pkg)
        assert resolved == agents_dir

        catalog = Catalog(resolved)
        presets = catalog.list_presets()
        assert len(presets) >= 1
        assert catalog.find_template("canary") is not None

    def test_warns_when_no_scaffold_resolves_at_all(self, tmp_path, caplog):
        pkg = tmp_path / "site-packages" / "robothor"
        (pkg / "templates").mkdir(parents=True)  # no bundled_scaffold/ inside
        (pkg / "__init__.py").write_text("")
        (pkg / "templates" / "__init__.py").write_text("")

        with caplog.at_level(logging.WARNING):
            resolved = _find_catalog_dir(package_dir=pkg)

        assert resolved is None
        assert any(
            "catalog" in record.message.lower() or "scaffold" in record.message.lower()
            for record in caplog.records
        )

    def test_warns_when_scaffold_has_no_agents_dir(self, tmp_path, caplog):
        pkg = _make_wheel_layout(tmp_path)
        (pkg / "templates" / "bundled_scaffold").mkdir(parents=True)
        # No agents/ subdirectory under the resolved scaffold.

        with caplog.at_level(logging.WARNING):
            resolved = _find_catalog_dir(package_dir=pkg)

        assert resolved is None
        assert any("agents" in record.message.lower() for record in caplog.records)

    def test_empty_resolution_warns_instead_of_silently_returning_empty(self, tmp_path, caplog):
        """A Catalog pointed at a real directory with no _catalog.yaml must
        warn loudly, not just quietly hand back an empty catalog."""
        empty_dir = tmp_path / "agents"
        empty_dir.mkdir()
        catalog = Catalog(empty_dir)

        with caplog.at_level(logging.WARNING):
            data = catalog.catalog

        assert data == {"departments": {}, "presets": {}}
        assert any("_catalog.yaml" in record.message for record in caplog.records)
