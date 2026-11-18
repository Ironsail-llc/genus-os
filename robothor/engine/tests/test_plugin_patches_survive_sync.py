"""A plugin's patch onto another plugin's agent survives that agent's re-render.

One plugin (the *owner*) ships an agent; another (a *patcher*) appends tools
to it through ``patches:``. Sync applies plugins in name order, and the owner
re-renders its agent from its template whenever the file on disk differs from
the render -- which a patched agent always does. Before this fix the owner's
render dropped every foreign patch, so an owner that sorted after its patcher
lost the patch on every sync, and reinstalling the patcher alone did not help
(the same sync re-ran the owner after it).

What is pinned here: after any sync, install or upgrade -- of one plugin or of
all of them, in either name order -- every installed patcher's patch is in
effect; removal still detaches only what the remover added; a patch whose
target is not installed is skipped with a warning and takes effect once the
target arrives; ``genus plugin sync --check`` reports a missing patch; and an
agent no plugin patches is written byte for byte as before.
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from importlib import metadata
from typing import TYPE_CHECKING

import pytest
import yaml

from robothor.cli.plugins import cmd_plugin
from robothor.plugins import assets, lockfile

if TYPE_CHECKING:
    from pathlib import Path

AGENT = "desk-monitor"
AGENT_YAML = f"docs/agents/{AGENT}.yaml"

_SETUP = f"""\
agent_id: {AGENT}
version: "{{version}}"
instruction_file_path: brain/agents/{AGENT}.md
variables: {{{{}}}}
"""

_AGENT_MANIFEST = f"""\
id: {AGENT}
name: Desk Monitor
description: {{description}}
version: "{{version}}"
department: custom
instruction_file: brain/agents/{AGENT}.md
tools_allowed:
  - list_tasks
  - create_note
delivery:
  mode: none
"""


def _owner(dist: str, *, version: str = "1.0.0", description: str = "Watches a desk") -> dict:
    pkg = dist.replace("-", "_")
    bundle = f"{pkg}/agents/{AGENT}"
    return {
        "dist": dist,
        "version": version,
        "manifest": (
            f"name: {dist}\ncontract_version: 1\nhandlers:\n  - {pkg}_probe\n"
            f"agents:\n  - agents/{AGENT}\n"
        ),
        "files": {
            f"{bundle}/setup.yaml": _SETUP.format(version=version),
            f"{bundle}/manifest.template.yaml": _AGENT_MANIFEST.format(
                version=version, description=description
            ),
            f"{bundle}/instructions.template.md": "# Desk Monitor\n\nWatch the desk.\n",
        },
    }


def _patcher(dist: str, tools: list[str], *, target: str = AGENT, version: str = "1.0.0") -> dict:
    pkg = dist.replace("-", "_")
    items = "".join(f"      - {t}\n" for t in tools)
    return {
        "dist": dist,
        "version": version,
        "manifest": (
            f"name: {dist}\ncontract_version: 2\nhandlers:\n  - {pkg}_probe\n"
            f"patches:\n  - agent: {target}\n    file: patches/agent.patch.yaml\n"
        ),
        "files": {
            f"{pkg}/patches/agent.patch.yaml": f"install:\n  append:\n    tools_allowed:\n{items}",
        },
    }


def _distribution(root: Path, spec: dict) -> metadata.Distribution:
    """*spec* as pip leaves it on a plugin path, RECORD included."""
    dist, version = spec["dist"], spec["version"]
    pkg = dist.replace("-", "_")
    site = root / f"{dist}-{version}"
    info = f"{pkg}-{version}.dist-info"
    members = {
        f"{pkg}/__init__.py": "PLUGIN = {}\n",
        f"{pkg}/genus-plugin.yaml": spec["manifest"],
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n",
        f"{info}/entry_points.txt": f"[genus.tools]\nx = {pkg}:PLUGIN\n",
        **spec["files"],
    }
    whl = root / f"{dist}-{version}.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    with zipfile.ZipFile(whl) as zf:
        zf.extractall(site)
    record = [f"{n},," for n in members] + [f"{info}/RECORD,,"]
    (site / info / "RECORD").write_text("\n".join(record) + "\n")
    return metadata.PathDistribution(site / info)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    (workspace / "docs" / "agents").mkdir(parents=True)
    (workspace / "brain" / "agents").mkdir(parents=True)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(tmp_path / "plugins.lock"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", raising=False)
    from robothor.settings import reset_settings

    reset_settings()
    lockfile.forget_warnings()
    yield workspace
    reset_settings()
    for name, module in list(sys.modules.items()):
        origin = getattr(getattr(module, "__spec__", None), "origin", None) or ""
        if origin.startswith(f"{tmp_path}{os.sep}"):
            del sys.modules[name]


class Fleet:
    """The installed plugins, and the two ways an operator syncs them."""

    def __init__(self, ws: Path, monkeypatch) -> None:
        self.ws = ws
        self.root = ws.parent / "plugins"
        self.root.mkdir()
        self.dists: dict[str, metadata.Distribution] = {}
        monkeypatch.setattr(lockfile, "installed_distributions", lambda: dict(self.dists))
        monkeypatch.setattr(
            lockfile, "entry_point_groups", lambda: dict.fromkeys(self.dists, {"genus.tools"})
        )

    def put(self, spec: dict) -> None:
        self.dists[spec["dist"]] = _distribution(self.root, spec)

    def drop(self, dist: str) -> None:
        self.dists.pop(dist)

    def sync_all(self) -> int:
        """`genus plugin sync`: every installed plugin, in name order."""
        return cmd_plugin(argparse.Namespace(plugin_command="sync", force=False, strict=True))

    def sync_one(self, dist: str) -> assets.Report:
        """What an install, reinstall or upgrade of one plugin does to the workspace."""
        found = self.dists[dist]
        return assets.apply(assets.declaration_from_distribution(found), dist, str(found.version))

    def check(self) -> int:
        return cmd_plugin(argparse.Namespace(plugin_command="sync", check=True, strict=False))

    def tools(self) -> list[str]:
        return yaml.safe_load((self.ws / AGENT_YAML).read_text()).get("tools_allowed") or []


@pytest.fixture
def fleet(ws, monkeypatch) -> Fleet:
    return Fleet(ws, monkeypatch)


BASE = ["list_tasks", "create_note"]


@pytest.mark.parametrize(
    ("owner", "patcher"),
    [("alpha-owner", "omega-patcher"), ("omega-owner", "alpha-patcher")],
    ids=["owner-sorts-first", "owner-sorts-last"],
)
def test_every_sync_keeps_the_patch_in_either_name_order(fleet, capsys, owner, patcher) -> None:
    fleet.put(_owner(owner))
    fleet.put(_patcher(patcher, ["card_lookup", "log_contact"]))
    for _ in range(3):
        assert fleet.sync_all() == 0, capsys.readouterr().err
        assert fleet.tools() == [*BASE, "card_lookup", "log_contact"]


def test_a_second_sync_writes_nothing_when_the_patch_is_in_place(fleet, capsys) -> None:
    fleet.put(_owner("omega-owner"))
    fleet.put(_patcher("alpha-patcher", ["card_lookup"]))
    assert fleet.sync_all() == 0
    manifest = fleet.ws / AGENT_YAML
    before = (manifest.read_bytes(), manifest.stat().st_mtime_ns)
    capsys.readouterr()
    assert fleet.sync_all() == 0
    assert (manifest.read_bytes(), manifest.stat().st_mtime_ns) == before
    assert f"agent    {AGENT}" not in capsys.readouterr().out


def test_upgrading_the_owner_alone_keeps_the_patch_and_takes_the_new_release(fleet) -> None:
    fleet.put(_owner("omega-owner"))
    fleet.put(_patcher("alpha-patcher", ["card_lookup"]))
    assert fleet.sync_all() == 0
    fleet.put(_owner("omega-owner", version="1.1.0", description="Watches the desk closely"))
    fleet.sync_one("omega-owner")
    data = yaml.safe_load((fleet.ws / AGENT_YAML).read_text())
    assert data["description"] == "Watches the desk closely"
    assert data["tools_allowed"] == [*BASE, "card_lookup"]


def test_reinstalling_the_patcher_alone_restores_a_lost_patch(fleet) -> None:
    fleet.put(_owner("omega-owner"))
    fleet.put(_patcher("alpha-patcher", ["card_lookup"]))
    assert fleet.sync_all() == 0
    # The patch was lost somehow (an older engine, a hand edit of the list).
    path = fleet.ws / AGENT_YAML
    data = yaml.safe_load(path.read_text())
    data["tools_allowed"] = list(BASE)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    fleet.sync_one("alpha-patcher")
    assert fleet.tools() == [*BASE, "card_lookup"]
    fleet.sync_one("omega-owner")  # and the owner after it does not drop it again
    assert fleet.tools() == [*BASE, "card_lookup"]


def test_two_patchers_on_one_agent_both_survive_in_a_stable_order(fleet, capsys) -> None:
    fleet.put(_owner("middle-owner"))
    fleet.put(_patcher("alpha-patcher", ["card_lookup"]))
    fleet.put(_patcher("omega-patcher", ["ledger_read"]))
    assert fleet.sync_all() == 0, capsys.readouterr().err
    expected = [*BASE, "card_lookup", "ledger_read"]
    assert fleet.tools() == expected
    fleet.sync_one("middle-owner")
    assert fleet.tools() == expected
    fleet.sync_one("omega-patcher")
    fleet.sync_one("middle-owner")
    assert fleet.tools() == expected


def test_removing_one_patcher_detaches_only_its_own_items(fleet) -> None:
    fleet.put(_owner("middle-owner"))
    fleet.put(_patcher("alpha-patcher", ["card_lookup"]))
    fleet.put(_patcher("omega-patcher", ["ledger_read"]))
    assert fleet.sync_all() == 0
    assets.retire("alpha-patcher")
    fleet.drop("alpha-patcher")
    assert fleet.tools() == [*BASE, "ledger_read"]
    assert fleet.sync_all() == 0  # the owner's re-render does not bring it back
    fleet.sync_one("middle-owner")
    assert fleet.tools() == [*BASE, "ledger_read"]


def test_a_patch_whose_target_is_not_installed_is_skipped_then_applied_when_it_arrives(
    fleet, capsys
) -> None:
    fleet.put(_patcher("alpha-patcher", ["card_lookup"]))
    report = fleet.sync_one("alpha-patcher")
    assert any("skipped" in w and AGENT in w for w in report.warnings)
    assert not (fleet.ws / AGENT_YAML).exists()
    fleet.put(_owner("omega-owner"))
    fleet.sync_one("omega-owner")  # the owner alone: the waiting patch takes effect
    assert fleet.tools() == [*BASE, "card_lookup"]


def test_sync_check_reports_a_missing_patch_and_changes_nothing(fleet, capsys) -> None:
    fleet.put(_owner("omega-owner"))
    fleet.put(_patcher("alpha-patcher", ["card_lookup"]))
    assert fleet.sync_all() == 0
    capsys.readouterr()
    assert fleet.check() == 0
    assert "out of sync" not in capsys.readouterr().out

    path = fleet.ws / AGENT_YAML
    data = yaml.safe_load(path.read_text())
    data["tools_allowed"] = list(BASE)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    before = path.read_bytes()
    assert fleet.check() == 1
    out = capsys.readouterr().out
    assert AGENT in out and "alpha-patcher" in out and "out of sync" in out
    assert path.read_bytes() == before


def test_an_agent_no_plugin_patches_is_written_exactly_as_before(fleet, capsys) -> None:
    """Regression: the owner's render is the file, byte for byte."""
    fleet.put(_owner("omega-owner"))
    assert fleet.sync_all() == 0, capsys.readouterr().err
    rendered = assets._render(  # what the template installer writes for this agent
        assets.declaration_from_distribution(fleet.dists["omega-owner"]).agents[0],
        "omega-owner",
        "1.0.0",
        fleet.ws,
        fleet.ws / ".robothor",
    )
    assert (fleet.ws / AGENT_YAML).read_text() == rendered.manifest_text
    manifest = fleet.ws / AGENT_YAML
    before = (manifest.read_bytes(), manifest.stat().st_mtime_ns)
    assert fleet.sync_all() == 0
    assert (manifest.read_bytes(), manifest.stat().st_mtime_ns) == before
