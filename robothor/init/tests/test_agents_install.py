"""The step that gives the instance a fleet, held to actually giving it one.

The defect: `create_workspace` copied the scaffold `agent-manifest.yaml` into
`docs/agents/`, where its placeholder line `id: {AGENT_ID}` parses as a YAML
MAPPING. Four steps later the preset installer cross-references every manifest
already in that directory by `data["id"]` — a dict — and raised
`TypeError: unhashable type: 'dict'` for every agent. The step caught each one
(one bad template is not ten), reported "0 of 3 agents installed", and the
runner recorded it as `applied`, exit 0, "Genus OS is initialized."

So an instance came up with no fleet and one placeholder manifest that
`load_manifest_dir` was happy to load as an agent.

These tests use the REAL installer against a REAL `create_workspace` tree,
because every faked layer is a layer that hid this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from robothor.init.context import InitContext
from robothor.init.steps import AgentsStep, StepError

REPO_ROOT = Path(__file__).resolve().parents[3]


def _ctx(tmp_path, **kwargs: Any) -> InitContext:
    kwargs.setdefault("yes", True)
    return InitContext(workspace=tmp_path / "workspace", **kwargs)


@pytest.fixture
def scaffolded(tmp_path, monkeypatch):
    """A workspace built exactly the way step 7 builds it."""
    monkeypatch.setenv("ROBOTHOR_TEMPLATE_DIR", str(REPO_ROOT / "templates"))
    from robothor.setup import create_workspace

    workspace = tmp_path / "workspace"
    create_workspace(workspace)
    return workspace


class TestTheScaffoldIsNotAnAgent:
    def test_the_manifest_directory_holds_no_yaml_the_engine_would_load(self, scaffolded):
        """`load_manifest_dir` globs `*.yaml`, and so does the installer's
        cross-reference. A placeholder in there is an agent to both of them."""
        loadable = sorted(p.name for p in (scaffolded / "docs" / "agents").glob("*.yaml"))

        assert loadable == []

    def test_the_example_is_still_shipped_under_a_name_nothing_globs(self, scaffolded):
        example = scaffolded / "docs" / "agents" / "agent-manifest.yaml.example"

        assert example.is_file()
        assert "{AGENT_ID}" in example.read_text()

    def test_the_engine_sees_an_empty_fleet_not_a_broken_one(self, scaffolded):
        from robothor.engine.config import load_manifest_dir

        scan = load_manifest_dir(scaffolded / "docs" / "agents")

        assert list(scan.manifests) == []
        assert list(scan.failures) == []


class TestAPresetActuallyInstalls:
    def test_every_agent_lands_in_a_freshly_created_workspace(self, scaffolded):
        """Into a bare directory the preset installed three agents; into a
        create_workspace-built one, zero. That difference was the bug."""
        from robothor.cli.agent import install_preset

        outcome = install_preset("minimal", auto_yes=True, workspace=scaffolded)

        assert outcome["unknown_preset"] is False
        assert outcome["failed"] == {}
        assert len(outcome["installed"]) == outcome["requested"]
        assert outcome["requested"] > 0

    def test_the_installed_manifests_are_loadable_agents(self, scaffolded):
        from robothor.cli.agent import install_preset
        from robothor.engine.config import load_manifest_dir

        install_preset("minimal", auto_yes=True, workspace=scaffolded)
        scan = load_manifest_dir(scaffolded / "docs" / "agents")

        assert len(scan.manifests) >= 1
        assert list(scan.failures) == []
        assert all(isinstance(row.get("id"), str) for row in scan.manifests)


class TestZeroInstalledIsAFailure:
    def _outcome(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "unknown_preset": False,
            "available": ["minimal", "standard", "full"],
            "requested": 3,
            "installed": [],
            "failed": {"main": "TypeError: unhashable type: 'dict'"},
            "missing": [],
        }
        base.update(overrides)
        return base

    def test_installing_none_of_them_raises_rather_than_reporting_applied(self, tmp_path):
        step = AgentsStep(installer=lambda preset, **k: self._outcome())

        with pytest.raises(StepError) as exc:
            step.apply(_ctx(tmp_path, answers={"preset": "standard"}))

        assert "0 of 3" in str(exc.value)
        assert "main" in str(exc.value)

    def test_the_reason_the_installer_gave_is_carried_through(self, tmp_path):
        step = AgentsStep(installer=lambda preset, **k: self._outcome())

        with pytest.raises(StepError) as exc:
            step.apply(_ctx(tmp_path, answers={"preset": "standard"}))

        assert "unhashable" in str(exc.value)

    def test_a_partial_install_reports_without_failing(self, tmp_path):
        """Seven of ten installed is not a failed step: it is seven agents and
        a named problem, and the operator needs the instance either way."""
        step = AgentsStep(
            installer=lambda preset, **k: self._outcome(installed=["main", "scout"], requested=3)
        )
        ctx = _ctx(tmp_path, answers={"preset": "standard"})

        step.apply(ctx)

        assert "2 of 3" in ctx.details["agents"]
        assert "main" in ctx.details["agents"]

    def test_a_preset_that_asked_for_nothing_is_not_a_failure(self, tmp_path):
        step = AgentsStep(
            installer=lambda preset, **k: self._outcome(requested=0, failed={}, installed=[])
        )
        ctx = _ctx(tmp_path, answers={"preset": "standard"})

        step.apply(ctx)

        assert "0 of 0" in ctx.details["agents"]


class TestTheValidatorSurvivesAPlaceholder:
    def test_a_manifest_whose_id_is_not_a_string_is_skipped_not_fatal(self, tmp_path):
        """`id: {AGENT_ID}` is a dict, and indexing a dict by it raises."""
        from robothor.templates.validators import validate_post_install

        manifest_dir = tmp_path / "docs" / "agents"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "placeholder.yaml").write_text(
            "id: {AGENT_ID}\nname: Placeholder\n", encoding="utf-8"
        )
        real = manifest_dir / "main.yaml"
        real.write_text(
            yaml.safe_dump({"id": "main", "name": "Main", "model": {"primary": "x"}}),
            encoding="utf-8",
        )

        # The contract is "returns findings", not "raises".
        findings = validate_post_install(real, repo_root=tmp_path)

        assert isinstance(findings, list)

    def test_a_manifest_that_is_not_a_mapping_at_all_is_skipped(self, tmp_path):
        from robothor.templates.validators import validate_post_install

        manifest_dir = tmp_path / "docs" / "agents"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "list.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")
        real = manifest_dir / "main.yaml"
        real.write_text(
            yaml.safe_dump({"id": "main", "name": "Main", "model": {"primary": "x"}}),
            encoding="utf-8",
        )

        assert isinstance(validate_post_install(real, repo_root=tmp_path), list)
