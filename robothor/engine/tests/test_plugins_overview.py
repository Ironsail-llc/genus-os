"""``plugins_overview`` — what every installed plugin contributes, in one call.

The main agent was asked "what does the desk plugin give us?" and had no
answer short of reading three files by hand: the loader knows the tools and
checks, the asset ledger knows the agents, workflows, patches and files, and
``installed.yaml`` knows which agents a plugin owns. This tool joins them.

The loader's report is faked (``inventory``), the ledger and the workspace are
real files in a temporary directory, and the registry is a stub that answers
one description per tool.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from robothor.plugins.inventory import PluginStatus

if TYPE_CHECKING:
    from pathlib import Path

_LEDGER = {
    "plugins": {
        "acme-desk": {
            "version": "1.0.0",
            "agents": ["desk-monitor"],
            "files": {
                "docs/workflows/desk-pass.yaml": {"sha256": "0" * 64, "created": True},
                "docs/workflows/gone.yaml": {"sha256": "0" * 64, "created": True},
                "docs/webhooks.yaml": {"sha256": "0" * 64, "created": False},
            },
            "patches": [{"agent": "main", "keys": ["tools_allowed"], "remove": {}}],
        },
        # Ships workspace files and no entry point at all: the loader never
        # sees it, and the overview must still name it.
        "acme-assets-only": {
            "version": "0.3.0",
            "agents": [],
            "files": {"brain/notes/readme.md": {"sha256": "0" * 64, "created": True}},
            "patches": [],
        },
    }
}

_INSTALLED = {
    "agents": {
        "desk-monitor": {"source": "plugin:acme-desk", "source_path": "acme-desk"},
        # Recorded as the plugin's, absent from the ledger (an adopted agent).
        "desk-helper": {"source": "plugin:acme-desk", "source_path": "acme-desk"},
        "main": {"source": "local", "source_path": "main"},
    }
}

_DESK_MONITOR = {
    "id": "desk-monitor",
    "name": "Desk Monitor",
    "schedule": {"cron": "*/15 * * * *", "timezone": "UTC"},
}

_DESK_PASS = {
    "id": "desk-pass",
    "name": "Desk pass",
    "triggers": [
        {"type": "cron", "cron": "0 7 * * *"},
        {"type": "hook", "stream": "email", "event_type": "email.new"},
    ],
    "steps": [{"id": "s1", "type": "noop"}],
}


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(root))
    return root


@pytest.fixture
def populated(workspace) -> Path:
    _write(workspace / ".robothor" / "plugin-assets.yaml", _LEDGER)
    _write(workspace / ".robothor" / "installed.yaml", _INSTALLED)
    _write(workspace / "docs" / "agents" / "desk-monitor.yaml", _DESK_MONITOR)
    _write(workspace / "docs" / "workflows" / "desk-pass.yaml", _DESK_PASS)
    return workspace


@pytest.fixture
def fake_loader(monkeypatch):
    rows = (
        PluginStatus(
            name="acme-desk",
            version="1.0.0",
            groups=("genus.doctor", "genus.tools"),
            manifest={
                "contract_version": 1,
                "declared": {"handlers": ["desk_status"], "checks": ["acme-desk.configured"]},
            },
        ),
        PluginStatus(
            name="genus-hostinfo",
            version="0.1.0",
            groups=("genus.tools",),
            manifest={"contract_version": 1, "declared": {"handlers": ["host_state"]}},
        ),
    )
    monkeypatch.setattr("robothor.plugins.inventory.inventory", lambda: rows)
    return rows


@pytest.fixture
def fake_registry(monkeypatch):
    def get_schema(name: str, **_: Any) -> dict[str, Any] | None:
        if name == "host_state":
            return {"type": "function", "function": {"name": name, "description": "Host facts."}}
        return None

    monkeypatch.setattr(
        "robothor.engine.tools.registry.get_registry",
        lambda: SimpleNamespace(get_schema=get_schema),
    )


def _call(args: dict[str, Any] | None = None) -> dict[str, Any]:
    from robothor.engine.tools.handlers.plugins import HANDLERS

    ctx = SimpleNamespace(agent_id="main", tenant_id="test-tenant")
    result: dict[str, Any] = asyncio.run(HANDLERS["plugins_overview"](args or {}, ctx))
    return result


def _by_name(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["name"]: p for p in result["plugins"]}


class TestWiring:
    def test_the_handler_is_dispatched(self) -> None:
        from robothor.engine.tools.dispatch import builtin_handlers

        assert "plugins_overview" in builtin_handlers()

    def test_the_tool_has_a_schema_with_no_required_arguments(self) -> None:
        from robothor.engine.tools.schemas import get_engine_schemas

        schema = get_engine_schemas()["plugins_overview"]
        assert schema["function"]["name"] == "plugins_overview"
        assert schema["function"]["description"]
        assert not schema["function"]["parameters"].get("required")

    def test_it_is_classified_read_only(self) -> None:
        from robothor.engine.tools.constants import READONLY_TOOLS

        assert "plugins_overview" in READONLY_TOOLS

    def test_benchmark_agents_do_not_receive_it(self) -> None:
        """It reads the REAL instance's plugins, like ``config_get`` its settings."""
        from robothor.engine.tools.handlers.benchmark import (
            _BENCHMARK_WITHHELD_READS,
            benchmark_readonly_tools,
        )

        assert "plugins_overview" in _BENCHMARK_WITHHELD_READS
        assert "plugins_overview" not in benchmark_readonly_tools()


class TestMalformedRecords:
    def test_a_ledger_agents_value_that_is_not_a_list_is_ignored(
        self, workspace, fake_loader, fake_registry
    ) -> None:
        _write(
            workspace / ".robothor" / "plugin-assets.yaml",
            {"plugins": {"acme-desk": {"version": "1.0.0", "agents": "desk-monitor"}}},
        )
        desk = _by_name(_call())["acme-desk"]
        assert desk["agents"] == [], "a string must not be iterated character by character"

    def test_installed_yaml_with_no_agents_key_is_not_a_warning(
        self, workspace, fake_loader, fake_registry
    ) -> None:
        for body in ({}, {"agents": None}):
            _write(workspace / ".robothor" / "installed.yaml", body)
            result = _call()
            assert "warnings" not in result, body

    def test_an_empty_installed_yaml_is_not_a_warning(
        self, workspace, fake_loader, fake_registry
    ) -> None:
        path = workspace / ".robothor" / "installed.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        assert "warnings" not in _call()

    def test_a_corrupted_installed_yaml_warns(self, workspace, fake_loader, fake_registry) -> None:
        path = workspace / ".robothor" / "installed.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("agents: [unclosed", encoding="utf-8")
        result = _call()
        assert any("not readable" in w for w in result.get("warnings", []))

    def test_installed_yaml_agents_that_is_not_a_mapping_warns(
        self, workspace, fake_loader, fake_registry
    ) -> None:
        _write(workspace / ".robothor" / "installed.yaml", {"agents": ["desk-monitor"]})
        assert _call().get("warnings")


class TestOverview:
    def test_every_installed_plugin_is_listed_with_its_version(
        self, populated, fake_loader, fake_registry
    ) -> None:
        plugins = _by_name(_call())
        assert set(plugins) == {"acme-desk", "genus-hostinfo", "acme-assets-only"}
        assert plugins["acme-desk"]["version"] == "1.0.0"
        assert plugins["acme-assets-only"]["version"] == "0.3.0"

    def test_tools_carry_the_registered_description(
        self, populated, fake_loader, fake_registry
    ) -> None:
        plugins = _by_name(_call())
        assert plugins["genus-hostinfo"]["tools"] == [
            {"name": "host_state", "description": "Host facts."}
        ]
        # A declared tool with no registered schema is still named.
        assert plugins["acme-desk"]["tools"] == [{"name": "desk_status", "description": ""}]

    def test_doctor_checks_are_listed(self, populated, fake_loader, fake_registry) -> None:
        plugins = _by_name(_call())
        assert plugins["acme-desk"]["checks"] == ["acme-desk.configured"]
        assert plugins["genus-hostinfo"]["checks"] == []

    def test_agents_come_from_the_ledger_and_installed_yaml(
        self, populated, fake_loader, fake_registry
    ) -> None:
        agents = _by_name(_call())["acme-desk"]["agents"]
        assert agents == [
            {"id": "desk-helper", "name": "desk-helper", "schedule": None},
            {"id": "desk-monitor", "name": "Desk Monitor", "schedule": "*/15 * * * *"},
        ]

    def test_workflows_are_parsed_for_name_and_triggers(
        self, populated, fake_loader, fake_registry
    ) -> None:
        workflows = _by_name(_call())["acme-desk"]["workflows"]
        assert workflows == [
            {
                "id": "desk-pass",
                "name": "Desk pass",
                "triggers": ["cron 0 7 * * *", "hook email:email.new"],
            },
            # Recorded in the ledger, file missing: named by its file.
            {"id": "gone", "name": "gone", "triggers": []},
        ]

    def test_patches_and_other_files(self, populated, fake_loader, fake_registry) -> None:
        plugins = _by_name(_call())
        assert plugins["acme-desk"]["patches"] == ["main"]
        assert plugins["acme-desk"]["files"] == ["docs/webhooks.yaml"]
        assert plugins["acme-assets-only"]["files"] == ["brain/notes/readme.md"]

    def test_a_plugin_with_no_assets_has_empty_lists(
        self, populated, fake_loader, fake_registry
    ) -> None:
        hostinfo = _by_name(_call())["genus-hostinfo"]
        for key in ("agents", "workflows", "checks", "patches", "files"):
            assert hostinfo[key] == [], key

    def test_no_ledger_is_not_an_error(self, workspace, fake_loader, fake_registry) -> None:
        result = _call()
        assert "error" not in result
        assert set(_by_name(result)) == {"acme-desk", "genus-hostinfo"}
        assert not (workspace / ".robothor").exists(), "a read must not create the instance dir"

    def test_an_unreadable_ledger_is_reported_not_raised(
        self, workspace, fake_loader, fake_registry
    ) -> None:
        ledger = workspace / ".robothor" / "plugin-assets.yaml"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("plugins: [unclosed", encoding="utf-8")
        result = _call()
        assert set(_by_name(result)) == {"acme-desk", "genus-hostinfo"}
        assert result.get("warnings")

    def test_nothing_installed(self, workspace, monkeypatch, fake_registry) -> None:
        monkeypatch.setattr("robothor.plugins.inventory.inventory", lambda: ())
        assert _call()["plugins"] == []
