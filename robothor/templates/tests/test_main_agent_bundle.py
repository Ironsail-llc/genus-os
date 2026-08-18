"""The shipped `main` agent bundle must install cleanly from the real catalog.

`infra/docker-compose.apps.yml` hard-requires a `main` agent
(`ROBOTHOR_REQUIRED_AGENT_IDS: main`), and `robothor init` prints
`robothor agent install --preset standard` as its literal next step. Both of
those are dead ends unless the real, tracked `templates/agents/` catalog
resolves a `main` bundle and that bundle installs into a manifest that
passes the platform's own schema contract (`docs/agents/schema.yaml`, the
same file `scripts/validate_agents.py` enforces).

These tests exercise the real repo catalog end to end (not a synthetic
fixture) so a future edit to `_catalog.yaml` or the `main` bundle that
breaks this flow fails here instead of silently shipping.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from robothor.templates.catalog import Catalog
from robothor.templates.installer import install
from robothor.templates.manifest_checks import load_schema, validate_agent

REPO = Path(__file__).resolve().parents[3]
CATALOG_DIR = REPO / "templates" / "agents"
SCHEMA_PATH = REPO / "docs" / "agents" / "schema.yaml"


@pytest.fixture
def real_catalog() -> Catalog:
    return Catalog(CATALOG_DIR)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace shaped like a real `robothor init` output: brain/ files
    that `install()`'s file-existence check expects, plus the catalog's own
    _defaults.yaml and the platform's real schema.yaml (mirroring how a dev
    checkout doubles as its own workspace today)."""
    ws = tmp_path / "workspace"
    (ws / "docs" / "agents").mkdir(parents=True)
    (ws / "brain" / "agents").mkdir(parents=True)
    (ws / ".robothor").mkdir(parents=True)
    (ws / "brain" / "AGENTS.md").write_text("# Agents\n")
    (ws / "brain" / "TOOLS.md").write_text("# Tools\n")
    (ws / "templates" / "agents").mkdir(parents=True)
    shutil.copy(CATALOG_DIR / "_defaults.yaml", ws / "templates" / "agents" / "_defaults.yaml")
    (ws / "docs" / "agents" / "schema.yaml").write_text(SCHEMA_PATH.read_text())
    return ws


class TestStandardPresetResolvesMain:
    def test_main_is_in_the_standard_preset(self, real_catalog):
        assert "main" in real_catalog.get_preset_agents("standard")

    def test_every_standard_preset_agent_has_a_real_template(self, real_catalog):
        agents = real_catalog.get_preset_agents("standard")
        assert agents, "standard preset resolved to zero agents"
        missing = [a for a in agents if real_catalog.find_template(a) is None]
        assert missing == [], f"standard preset references agents with no template bundle: {missing}"

    def test_every_catalog_department_agent_has_a_real_template(self, real_catalog):
        """Guards against the exact drift this bundle fixes: a department
        listing agent IDs that don't correspond to any templates/agents/
        bundle on disk, so installs silently skip them."""
        missing: dict[str, list[str]] = {}
        for dept in real_catalog.list_departments():
            gaps = [a for a in dept["agents"] if real_catalog.find_template(a) is None]
            if gaps:
                missing[dept["id"]] = gaps
        assert missing == {}, f"catalog departments reference missing bundles: {missing}"


class TestMainBundleInstalls:
    def test_install_writes_docs_agents_main_yaml(self, real_catalog, workspace):
        template_path = real_catalog.find_template("main")
        assert template_path is not None

        result = install(
            str(template_path),
            overrides={},
            auto_yes=True,
            instance_dir=workspace / ".robothor",
            repo_root=workspace,
        )

        assert result["agent_id"] == "main"
        manifest_path = workspace / "docs" / "agents" / "main.yaml"
        assert manifest_path.is_file()

        raw = manifest_path.read_text()
        assert "{{" not in raw, f"unresolved template variables leaked into main.yaml: {raw}"

    def test_installed_main_yaml_passes_schema_validation(self, real_catalog, workspace):
        template_path = real_catalog.find_template("main")
        install(
            str(template_path),
            overrides={},
            auto_yes=True,
            instance_dir=workspace / ".robothor",
            repo_root=workspace,
        )

        manifest_path = workspace / "docs" / "agents" / "main.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())

        schema, required_fields, departments = load_schema(SCHEMA_PATH)
        assert required_fields, "schema.yaml did not load any required fields"
        assert manifest.get("department") in departments

        results = validate_agent(
            manifest,
            all_manifests={"main": manifest},
            registered_tools=set(),
            repo_root=workspace,
        )
        failures = [(r.check_id, r.message, r.details) for r in results if r.status == "FAIL"]
        assert failures == [], f"main.yaml fails manifest checks: {failures}"
