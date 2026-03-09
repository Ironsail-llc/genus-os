"""Tests for the template catalog system."""

import pytest
import yaml

from robothor.templates.catalog import Catalog


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
        "model_primary": "openrouter/z-ai/glm-5",
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
        assert catalog.defaults["model_primary"] == "openrouter/z-ai/glm-5"
        assert catalog.defaults["timezone"] == "UTC"

    def test_list_available_templates(self, tmp_catalog_dir):
        catalog = Catalog(tmp_catalog_dir)
        templates = catalog.list_available_templates()
        assert len(templates) == 1
        assert templates[0]["id"] == "canary"
        assert templates[0]["department"] == "ops"
