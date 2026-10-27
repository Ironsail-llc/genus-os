"""A plugin ships agents, workflows, manifest patches and workspace files.

``genus-plugin.yaml`` declares them (``agents``, ``workflows``, ``patches``,
``files``) as package-relative paths inside the wheel, and ``genus plugin
install`` / ``sync`` / ``remove`` manage them on the workspace. What is pinned
here:

* **Agents render through the agent-template installer**, are recorded in
  ``.robothor/installed.yaml`` with ``source: plugin:<dist>``, and keep the
  operator's ``.robothor/overrides/<id>.yaml`` across an upgrade.
* **Install is idempotent**, and the first overwrite of a file the plugin did
  not create leaves a ``<dst>.pre-<plugin>`` copy, once.
* **Patches** edit an agent the plugin does not own, keep its comments, and are
  undone by their own ``remove:`` section.
* **Remove retires** (``docs/agents/retired/``) rather than deletes.
* **Unsafe paths are refused before pip runs.**
* **Manifest mode still means what it meant**: in ``enforce`` a ``sync`` does
  not write assets for a distribution the platform did not install.
"""

from __future__ import annotations

import argparse
import hashlib
import zipfile
from importlib import metadata
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import yaml

from robothor.cli.plugins import cmd_plugin
from robothor.plugins import installer, lockfile

if TYPE_CHECKING:
    from pathlib import Path

DIST = "example-agent"

_MANIFEST = """\
name: example-agent
contract_version: 1
handlers:
  - example_probe
entry_points:
  genus.tools:
    - example
agents:
  - agents/desk-monitor
workflows:
  - workflows/example-pass.yaml
patches:
  - agent: main
    file: patches/main.patch.yaml
files:
  - src: extra/webhooks.yaml
    dest: docs/webhooks.yaml
    mode: overwrite
  - src: extra/NOTES.md
    dest: brain/example-agent/NOTES.md
    mode: if_missing
"""

_CODE = 'PLUGIN = {"genus_contract_version": "1.0", "handlers": {"example_probe": lambda: None}}\n'

_SETUP = """\
agent_id: desk-monitor
version: "1.0.0"
instruction_file_path: brain/agents/desk-monitor.md
variables:
  cron_expr:
    type: string
    default: "0 * * * *"
"""

_AGENT_MANIFEST = """\
id: desk-monitor
name: Desk Monitor
description: {description}
version: "1.0.0"
department: custom
instruction_file: brain/agents/desk-monitor.md
schedule:
  cron: "{{{{ cron_expr }}}}"
  timezone: {{{{ timezone }}}}
delivery:
  mode: none
"""

_INSTRUCTIONS = "# Desk Monitor\n\nYou watch the example desk for Alice.\n"

_WORKFLOW = "id: example-pass\nsteps: []\n"

_PATCH = """\
install:
  set:
    tools_allowed: [example_probe]
    schedule:
      heartbeat_minutes: 30
remove:
  set:
    schedule:
      heartbeat_minutes: 60
  delete:
    - tools_allowed
"""

_MAIN = """\
# The operator's main agent. Keep this comment.
id: main
name: Main
schedule:
  cron: ""  # interactive only
  heartbeat_minutes: 60
"""


def _wheel(
    path: Path,
    *,
    version: str = "1.0.0",
    description: str = "Watches the example desk",
    manifest: str = _MANIFEST,
    extra: dict[str, str] | None = None,
) -> bytes:
    pkg = "example_agent"
    info = f"example_agent-{version}.dist-info"
    members = {
        f"{pkg}/__init__.py": _CODE,
        f"{pkg}/genus-plugin.yaml": manifest,
        f"{pkg}/agents/desk-monitor/setup.yaml": _SETUP,
        f"{pkg}/agents/desk-monitor/manifest.template.yaml": _AGENT_MANIFEST.format(
            description=description
        ),
        f"{pkg}/agents/desk-monitor/instructions.template.md": _INSTRUCTIONS,
        f"{pkg}/workflows/example-pass.yaml": _WORKFLOW,
        f"{pkg}/patches/main.patch.yaml": _PATCH,
        f"{pkg}/extra/webhooks.yaml": "channels: []\n",
        f"{pkg}/extra/NOTES.md": "notes from the plugin\n",
        f"{info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {DIST}\nVersion: {version}\nSummary: Example\n"
        ),
        f"{info}/entry_points.txt": "[genus.tools]\nexample = example_agent:PLUGIN\n",
    }
    members.update(extra or {})
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return path.read_bytes()


class _Pip:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command, **_):
        self.calls.append(list(command))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A workspace, instance dir and lockfile of this test's own."""
    workspace = tmp_path / "ws"
    (workspace / "docs" / "agents").mkdir(parents=True)
    (workspace / "brain" / "agents").mkdir(parents=True)
    (workspace / "docs" / "agents" / "main.yaml").write_text(_MAIN)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(tmp_path / "plugins.lock"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", raising=False)
    from robothor.settings import reset_settings

    reset_settings()
    lockfile.forget_warnings()
    monkeypatch.setattr(installer, "_run_pip", _Pip())
    yield workspace
    reset_settings()


def _args(command, **kw):
    defaults = {
        "name": "",
        "version": None,
        "index": None,
        "accept_review": True,
        "dry_run": False,
        "sha256": None,
        "scan_prompts": False,
        "force": False,
    }
    defaults.update(kw)
    return argparse.Namespace(plugin_command=command, **defaults)


def _install(ws: Path, **wheel_kw) -> int:
    version = wheel_kw.get("version", "1.0.0")
    path = ws.parent / f"example_agent-{version}-py3-none-any.whl"
    data = _wheel(path, **wheel_kw)
    return cmd_plugin(_args("install", name=str(path), sha256=hashlib.sha256(data).hexdigest()))


def _installed(ws: Path) -> dict:
    return (yaml.safe_load((ws / ".robothor" / "installed.yaml").read_text()) or {})["agents"]


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_renders_the_agent_and_records_its_source(ws, capsys) -> None:
    assert _install(ws) == 0, capsys.readouterr().err
    manifest = yaml.safe_load((ws / "docs" / "agents" / "desk-monitor.yaml").read_text())
    assert manifest["id"] == "desk-monitor"
    assert manifest["schedule"]["cron"] == "0 * * * *"
    assert (ws / "brain" / "agents" / "desk-monitor.md").read_text() == _INSTRUCTIONS
    record = _installed(ws)["desk-monitor"]
    assert record["source"] == f"plugin:{DIST}"
    out = capsys.readouterr().out
    assert "desk-monitor" in out


def test_install_copies_workflows_and_files(ws) -> None:
    assert _install(ws) == 0
    assert (ws / "docs" / "workflows" / "example-pass.yaml").read_text() == _WORKFLOW
    assert (ws / "docs" / "webhooks.yaml").read_text() == "channels: []\n"
    assert (ws / "brain" / "example-agent" / "NOTES.md").read_text() == "notes from the plugin\n"


def test_install_applies_the_patch_and_keeps_comments(ws) -> None:
    assert _install(ws) == 0
    text = (ws / "docs" / "agents" / "main.yaml").read_text()
    data = yaml.safe_load(text)
    assert data["tools_allowed"] == ["example_probe"]
    # deep merge: the sibling key survives
    assert data["schedule"]["cron"] == ""
    assert data["schedule"]["heartbeat_minutes"] == 30
    # the plugin did not create main.yaml, so it was backed up first
    assert (ws / "docs" / "agents" / f"main.yaml.pre-{DIST}").read_text() == _MAIN


def test_a_patch_keeps_the_manifest_comments_when_ruamel_is_installed(ws) -> None:
    """The comment-preserving round trip is ``genus agent``'s own, and it is
    only comment-preserving when ruamel.yaml is importable -- the platform does
    not depend on it. The backup is what protects the original either way."""
    pytest.importorskip("ruamel.yaml")
    assert _install(ws) == 0
    assert "Keep this comment." in (ws / "docs" / "agents" / "main.yaml").read_text()
    assert cmd_plugin(_args("remove", name=DIST)) == 0
    assert "Keep this comment." in (ws / "docs" / "agents" / "main.yaml").read_text()


def test_install_twice_is_idempotent(ws) -> None:
    assert _install(ws) == 0
    snapshot = {
        p.relative_to(ws).as_posix(): p.read_bytes()
        for p in sorted(ws.rglob("*"))
        if p.is_file() and ".robothor" not in p.parts
    }
    assert _install(ws) == 0
    again = {
        p.relative_to(ws).as_posix(): p.read_bytes()
        for p in sorted(ws.rglob("*"))
        if p.is_file() and ".robothor" not in p.parts
    }
    assert again == snapshot


def test_upgrade_updates_the_agent_but_keeps_operator_overrides(ws) -> None:
    assert _install(ws) == 0
    overrides = ws / ".robothor" / "overrides" / "desk-monitor.yaml"
    overrides.write_text('cron_expr: "30 6 * * *"\n')
    assert _install(ws, version="1.1.0", description="Watches the desk, better") == 0
    manifest = yaml.safe_load((ws / "docs" / "agents" / "desk-monitor.yaml").read_text())
    assert manifest["description"] == "Watches the desk, better"
    assert manifest["schedule"]["cron"] == "30 6 * * *"
    assert overrides.exists()


def test_first_overwrite_of_a_foreign_file_is_backed_up_once(ws) -> None:
    (ws / "docs" / "webhooks.yaml").write_text("channels: [mine]\n")
    assert _install(ws) == 0
    backup = ws / "docs" / f"webhooks.yaml.pre-{DIST}"
    assert backup.read_text() == "channels: [mine]\n"
    assert (ws / "docs" / "webhooks.yaml").read_text() == "channels: []\n"
    assert _install(ws) == 0
    assert backup.read_text() == "channels: [mine]\n"


def test_if_missing_never_replaces_an_existing_file(ws) -> None:
    notes = ws / "brain" / "example-agent" / "NOTES.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("the operator's notes\n")
    assert _install(ws) == 0
    assert notes.read_text() == "the operator's notes\n"
    assert not notes.with_name(f"NOTES.md.pre-{DIST}").exists()


def test_an_agent_installed_from_elsewhere_is_not_taken_over(ws, capsys) -> None:
    from robothor.templates.instance import InstanceConfig

    (ws / "docs" / "agents" / "desk-monitor.yaml").write_text("id: desk-monitor\n")
    InstanceConfig.load(ws / ".robothor").record_install(
        agent_id="desk-monitor",
        source="local",
        source_path="templates/agents/custom/desk-monitor",
        version="0.1.0",
        variables={},
        manifest_path="docs/agents/desk-monitor.yaml",
        instruction_path="",
    )
    pip = installer._run_pip
    assert _install(ws) == 2
    assert "desk-monitor" in capsys.readouterr().err
    assert pip.calls == []  # refused before pip


@pytest.mark.parametrize(
    "entry",
    [
        "files:\n  - src: extra/webhooks.yaml\n    dest: /etc/webhooks.yaml\n",
        "files:\n  - src: extra/webhooks.yaml\n    dest: docs/../../outside.yaml\n",
        "files:\n  - src: extra/webhooks.yaml\n    dest: docs/agents/evil.yaml\n",
        "files:\n  - src: extra/webhooks.yaml\n    dest: docs/webhooks.yaml\n    mode: append\n",
        "files:\n  - src: ../outside.yaml\n    dest: docs/webhooks.yaml\n",
        "workflows:\n  - ../outside.yaml\n",
        "agents:\n  - /abs/agent\n",
        "patches:\n  - agent: ../main\n    file: patches/main.patch.yaml\n",
        "patches:\n  - agent: desk-monitor\n    file: patches/main.patch.yaml\nagents:\n  - agents/desk-monitor\n",
        "workflows:\n  - workflows/missing.yaml\n",
    ],
)
def test_unsafe_or_broken_declarations_are_refused_before_pip(ws, capsys, entry) -> None:
    manifest = (
        "name: example-agent\ncontract_version: 1\nhandlers:\n  - example_probe\n"
        "entry_points:\n  genus.tools:\n    - example\n" + entry
    )
    pip = installer._run_pip
    assert _install(ws, manifest=manifest) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert pip.calls == []
    assert not (ws / "docs" / "webhooks.yaml").exists()


def test_a_symlinked_destination_directory_is_refused(ws, tmp_path, capsys) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / "docs" / "workflows").symlink_to(outside, target_is_directory=True)
    pip = installer._run_pip
    assert _install(ws) == 2
    assert pip.calls == []
    assert list(outside.iterdir()) == []


def test_dry_run_lists_the_workspace_changes_and_writes_none(ws, capsys) -> None:
    path = ws.parent / "example_agent-1.0.0-py3-none-any.whl"
    data = _wheel(path)
    code = cmd_plugin(
        _args("install", name=str(path), sha256=hashlib.sha256(data).hexdigest(), dry_run=True)
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "desk-monitor" in out
    assert "docs/workflows/example-pass.yaml" in out
    assert not (ws / "docs" / "agents" / "desk-monitor.yaml").exists()
    assert (ws / "docs" / "agents" / "main.yaml").read_text() == _MAIN


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_retires_agents_and_undoes_the_rest(ws, capsys) -> None:
    (ws / "docs" / "webhooks.yaml").write_text("channels: [mine]\n")
    assert _install(ws) == 0
    assert cmd_plugin(_args("remove", name=DIST)) == 0, capsys.readouterr().err

    assert not (ws / "docs" / "agents" / "desk-monitor.yaml").exists()
    assert (ws / "docs" / "agents" / "retired" / "desk-monitor.yaml").is_file()
    # the instruction file is the operator's to keep, as with any retire
    assert (ws / "brain" / "agents" / "desk-monitor.md").exists()
    assert "desk-monitor" not in _installed(ws)
    assert not (ws / "docs" / "workflows" / "example-pass.yaml").exists()
    # the file it overwrote is put back; the one it created is gone
    assert (ws / "docs" / "webhooks.yaml").read_text() == "channels: [mine]\n"
    assert not (ws / "docs" / f"webhooks.yaml.pre-{DIST}").exists()
    assert not (ws / "brain" / "example-agent" / "NOTES.md").exists()

    main = (ws / "docs" / "agents" / "main.yaml").read_text()
    data = yaml.safe_load(main)
    assert "tools_allowed" not in data
    assert data["schedule"]["heartbeat_minutes"] == 60


def test_remove_leaves_a_file_the_operator_edited(ws, capsys) -> None:
    assert _install(ws) == 0
    workflow = ws / "docs" / "workflows" / "example-pass.yaml"
    workflow.write_text("id: example-pass\nsteps: [edited]\n")
    assert cmd_plugin(_args("remove", name=DIST)) == 0
    assert workflow.read_text() == "id: example-pass\nsteps: [edited]\n"
    assert "example-pass.yaml" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# sync, and the manifest mode
# ---------------------------------------------------------------------------


def _site_distribution(tmp_path: Path, data: bytes) -> metadata.Distribution:
    """The wheel as pip would have left it in site-packages, RECORD included."""
    site = tmp_path / "site"
    site.mkdir()
    whl = tmp_path / "w.whl"
    whl.write_bytes(data)
    with zipfile.ZipFile(whl) as zf:
        zf.extractall(site)
        names = zf.namelist()
    info = next(n.split("/")[0] for n in names if ".dist-info/" in n)
    record = [f"{n},," for n in names] + [f"{info}/RECORD,,"]
    (site / info / "RECORD").write_text("\n".join(record) + "\n")
    return metadata.PathDistribution(site / info)


@pytest.fixture
def hand_installed(ws, tmp_path, monkeypatch):
    """A distribution pip-installed by hand: on the path, not governed."""
    data = _wheel(tmp_path / "x.whl")
    dist = _site_distribution(tmp_path, data)
    monkeypatch.setattr(lockfile, "installed_distributions", lambda: {DIST: dist})
    monkeypatch.setattr(lockfile, "entry_point_groups", lambda: {DIST: {"genus.tools"}})
    return dist


def test_sync_installs_assets_of_an_installed_distribution_in_observe(
    ws, hand_installed, capsys
) -> None:
    assert cmd_plugin(_args("sync")) == 0, capsys.readouterr().err
    assert (ws / "docs" / "agents" / "desk-monitor.yaml").is_file()
    assert (ws / "docs" / "workflows" / "example-pass.yaml").is_file()
    assert _installed(ws)["desk-monitor"]["source"] == f"plugin:{DIST}"


def _enforce(monkeypatch) -> None:
    monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", "enforce")
    from robothor.settings import reset_settings

    reset_settings()


def test_sync_in_enforce_installs_a_declared_hand_installed_plugin(
    ws, hand_installed, capsys, monkeypatch
) -> None:
    """Production never calls ``genus plugin install``: it pip-installs into
    the plugin dir and syncs, under enforce. The loader imports such a plugin
    in enforce (it ships a manifest), so its declared files are written too."""
    _enforce(monkeypatch)
    assert cmd_plugin(_args("sync")) == 0
    assert (ws / "docs" / "agents" / "desk-monitor.yaml").is_file()
    assert (ws / "docs" / "workflows" / "example-pass.yaml").is_file()
    assert yaml.safe_load((ws / "docs" / "agents" / "main.yaml").read_text())["tools_allowed"]


def test_sync_twice_is_idempotent(ws, hand_installed) -> None:
    assert cmd_plugin(_args("sync")) == 0
    first = {
        p.relative_to(ws).as_posix(): p.read_bytes()
        for p in sorted(ws.rglob("*"))
        if p.is_file() and ".robothor" not in p.parts
    }
    assert cmd_plugin(_args("sync")) == 0
    again = {
        p.relative_to(ws).as_posix(): p.read_bytes()
        for p in sorted(ws.rglob("*"))
        if p.is_file() and ".robothor" not in p.parts
    }
    assert again == first


def test_sync_after_an_installer_already_copied_and_patched_is_a_no_op(ws, hand_installed) -> None:
    """The transition period: an infra installer copies the files and applies
    the patch (keeping its own ``.pre-<plugin>`` backup) BEFORE calling sync."""
    workflows = ws / "docs" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "example-pass.yaml").write_text(_WORKFLOW)
    (ws / "docs" / "webhooks.yaml").write_text("channels: []\n")
    main = ws / "docs" / "agents" / "main.yaml"
    backup = main.with_name(f"main.yaml.pre-{DIST}")
    backup.write_text("the installer's backup\n")
    data = yaml.safe_load(main.read_text())
    data["tools_allowed"] = ["example_probe"]
    data["schedule"]["heartbeat_minutes"] = 30
    main.write_text(yaml.safe_dump(data, sort_keys=False))
    patched = main.read_bytes()

    assert cmd_plugin(_args("sync")) == 0
    assert main.read_bytes() == patched  # set values already present: unchanged
    assert backup.read_text() == "the installer's backup\n"  # never overwritten
    assert not (workflows / f"example-pass.yaml.pre-{DIST}").exists()  # identical: no backup
    assert not (ws / "docs" / f"webhooks.yaml.pre-{DIST}").exists()
    assert (ws / "docs" / "agents" / "desk-monitor.yaml").is_file()


def _broken_distribution(tmp_path: Path, ws: Path) -> metadata.Distribution:
    """A second plugin whose agent the template installer refuses: its
    instruction file already exists and no agent claims it."""
    (ws / "brain" / "agents" / "taken.md").write_text("somebody else's file\n")
    site = tmp_path / "site-broken"
    pkg = site / "broken_agent"
    (pkg / "agents" / "broken").mkdir(parents=True)
    (pkg / "workflows").mkdir()
    (pkg / "__init__.py").write_text(_CODE)
    (pkg / "genus-plugin.yaml").write_text(
        "name: broken-agent\ncontract_version: 1\nhandlers:\n  - example_probe\n"
        "agents:\n  - agents/broken\nworkflows:\n  - workflows/broken-pass.yaml\n"
    )
    (pkg / "agents" / "broken" / "manifest.template.yaml").write_text(
        "id: broken\nname: Broken\ndescription: refused\nversion: '1'\n"
        "department: custom\ninstruction_file: brain/agents/taken.md\n"
    )
    (pkg / "agents" / "broken" / "instructions.template.md").write_text("# Broken\n")
    (pkg / "workflows" / "broken-pass.yaml").write_text("id: broken-pass\n")
    info = site / "broken_agent-1.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: broken-agent\nVersion: 1.0.0\n")
    files = [p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file()]
    (info / "RECORD").write_text("\n".join(f"{f},," for f in files) + "\n")
    return metadata.PathDistribution(info)


def test_one_plugin_failing_does_not_stop_the_others(ws, tmp_path, capsys, monkeypatch) -> None:
    good = _site_distribution(tmp_path, _wheel(tmp_path / "x.whl"))
    broken = _broken_distribution(tmp_path, ws)
    found = {DIST: good, "broken-agent": broken}
    monkeypatch.setattr(lockfile, "installed_distributions", lambda: found)
    monkeypatch.setattr(lockfile, "entry_point_groups", lambda: {})

    assert cmd_plugin(_args("sync")) == 0
    captured = capsys.readouterr()
    assert "broken-agent" in captured.out + captured.err
    # the good plugin synced
    assert (ws / "docs" / "agents" / "desk-monitor.yaml").is_file()
    # the broken one wrote nothing: rolled back, not half-applied
    assert not (ws / "docs" / "agents" / "broken.yaml").exists()
    assert not (ws / "docs" / "workflows" / "broken-pass.yaml").exists()
    assert (ws / "brain" / "agents" / "taken.md").read_text() == "somebody else's file\n"


def test_a_failed_upgrade_leaves_the_previous_files(
    ws, hand_installed, tmp_path, monkeypatch
) -> None:
    assert cmd_plugin(_args("sync")) == 0
    before = (ws / "docs" / "agents" / "desk-monitor.yaml").read_bytes()
    bad = _MANIFEST.replace("    mode: if_missing\n", "    mode: if_missing\n") + (
        "  - src: extra/missing.yaml\n    dest: docs/hooks/missing.yaml\n"
    )
    site = tmp_path / "upgrade"
    site.mkdir()
    dist = _site_distribution(site, _wheel(site / "x.whl", version="2.0.0", manifest=bad))
    monkeypatch.setattr(lockfile, "installed_distributions", lambda: {DIST: dist})
    assert cmd_plugin(_args("sync")) == 0
    assert (ws / "docs" / "agents" / "desk-monitor.yaml").read_bytes() == before
    assert (ws / "docs" / "workflows" / "example-pass.yaml").is_file()


def test_the_engine_schema_gate_refuses_an_agent_under_its_enforce(
    ws, tmp_path, capsys, monkeypatch
) -> None:
    """The engine refuses at load a manifest its schema rejects (under
    ROBOTHOR_MANIFEST_SCHEMA_MODE=enforce); sync will not install one."""
    pytest.importorskip("robothor.engine.config")
    monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
    manifest = _AGENT_MANIFEST.replace("mode: none", "mode: carrier-pigeon")
    pkg = "example_agent"
    data = _wheel(
        tmp_path / "x.whl",
        extra={
            f"{pkg}/agents/desk-monitor/manifest.template.yaml": manifest.format(description="x")
        },
    )
    dist = _site_distribution(tmp_path, data)
    monkeypatch.setattr(lockfile, "installed_distributions", lambda: {DIST: dist})
    monkeypatch.setattr(lockfile, "entry_point_groups", lambda: {})
    assert cmd_plugin(_args("sync")) == 0
    captured = capsys.readouterr()
    assert "desk-monitor" in captured.out + captured.err
    assert not (ws / "docs" / "agents" / "desk-monitor.yaml").exists()
    assert not (ws / "docs" / "workflows" / "example-pass.yaml").exists()


def test_a_plugin_that_disappeared_is_reported_and_left_in_place(
    ws, hand_installed, capsys, monkeypatch
) -> None:
    assert cmd_plugin(_args("sync")) == 0
    monkeypatch.setattr(lockfile, "installed_distributions", lambda: {})
    capsys.readouterr()
    assert cmd_plugin(_args("sync")) == 0
    out = capsys.readouterr().out
    assert DIST in out and "remove --force" in out
    assert (ws / "docs" / "agents" / "desk-monitor.yaml").is_file()

    pip = installer._run_pip
    assert cmd_plugin(_args("remove", name=DIST, force=True)) == 0
    assert pip.calls == []  # nothing for pip to uninstall
    assert (ws / "docs" / "agents" / "retired" / "desk-monitor.yaml").is_file()
    assert not (ws / "docs" / "workflows" / "example-pass.yaml").exists()


def test_pip_target_install_then_sync_in_the_in_cluster_shape(
    ws, tmp_path, capsys, monkeypatch
) -> None:
    """The production path end to end: ``pip install --no-deps --target
    <plugin dir>``, the plugin dir on PYTHONPATH, enforce, then sync with REAL
    discovery. Templates are read from the plugin dir; nothing is written
    anywhere but the workspace."""
    import subprocess
    import sys

    probe = subprocess.run(
        [sys.executable, "-m", "pip", "--version"], capture_output=True, check=False
    )
    if probe.returncode != 0:
        pytest.skip("pip is not available in this interpreter")
    wheel = tmp_path / "example_agent-1.0.0-py3-none-any.whl"
    _wheel(wheel)
    info = "example_agent-1.0.0.dist-info"
    with zipfile.ZipFile(wheel, "a") as zf:
        zf.writestr(
            f"{info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        names = [*zf.namelist(), f"{info}/RECORD"]
        zf.writestr(f"{info}/RECORD", "".join(f"{n},,\n" for n in names))
    plugins = ws / ".plugins"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--disable-pip-version-check",
            "--target",
            str(plugins),
            str(wheel),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    monkeypatch.syspath_prepend(str(plugins))
    monkeypatch.setenv("ROBOTHOR_PLUGIN_DIR", str(plugins))
    _enforce(monkeypatch)
    snapshot = {p: p.read_bytes() for p in plugins.rglob("*") if p.is_file()}

    assert cmd_plugin(_args("sync")) == 0, capsys.readouterr().err
    assert _installed(ws)["desk-monitor"]["source"] == f"plugin:{DIST}"
    assert (ws / "docs" / "workflows" / "example-pass.yaml").is_file()
    assert {p: p.read_bytes() for p in plugins.rglob("*") if p.is_file()} == snapshot
    assert cmd_plugin(_args("sync")) == 0


def test_sync_skips_a_disabled_plugin(ws, hand_installed, capsys) -> None:
    assert cmd_plugin(_args("sync")) == 0
    lockfile.set_enabled(DIST, False)
    (ws / "docs" / "workflows" / "example-pass.yaml").unlink()
    assert cmd_plugin(_args("sync")) == 0
    assert not (ws / "docs" / "workflows" / "example-pass.yaml").exists()


def test_the_asset_keys_are_not_contribution_declarations() -> None:
    """``agents:`` is a list in the manifest, but it names no tool. The
    loader's enforce comparison must see exactly what it saw before."""
    from robothor.plugins.manifest import parse_manifest

    parsed = parse_manifest(_MANIFEST)
    assert parsed is not None
    assert parsed.declares("handlers") == {"example_probe"}
    for key in ("agents", "workflows", "patches", "files"):
        assert key not in parsed.declared


# ---------------------------------------------------------------------------
# the --target path: install into ROBOTHOR_PLUGIN_DIR, sync, remove
# ---------------------------------------------------------------------------


@pytest.fixture
def plugin_dir(ws, tmp_path, monkeypatch):
    """The wheel unpacked into ROBOTHOR_PLUGIN_DIR the way ``pip install
    --target`` leaves it, on the import path, with REAL discovery."""
    plugins = ws / ".plugins"
    plugins.mkdir()
    whl = tmp_path / "x.whl"
    whl.write_bytes(_wheel(tmp_path / "w.whl"))
    with zipfile.ZipFile(whl) as zf:
        zf.extractall(plugins)
        names = zf.namelist()
    info = next(n.split("/")[0] for n in names if ".dist-info/" in n)
    (plugins / info / "RECORD").write_text("".join(f"{n},,\n" for n in [*names, f"{info}/RECORD"]))
    monkeypatch.syspath_prepend(str(plugins))
    monkeypatch.setenv("ROBOTHOR_PLUGIN_DIR", str(plugins))
    _enforce(monkeypatch)
    return plugins


def test_unpacked_into_the_plugin_dir_then_sync_writes_the_agents_in_enforce(
    ws, plugin_dir, capsys
) -> None:
    """No `genus plugin install` anywhere: the wheel is unpacked, the mode is
    enforce, and sync alone writes the agent."""
    assert cmd_plugin(_args("sync")) == 0, capsys.readouterr().err
    assert (ws / "docs" / "agents" / "desk-monitor.yaml").is_file()
    assert (ws / "brain" / "agents" / "desk-monitor.md").is_file()


def test_remove_of_a_synced_plugin_in_the_plugin_dir_needs_no_force(ws, plugin_dir, capsys) -> None:
    assert cmd_plugin(_args("sync")) == 0
    pip = installer._run_pip
    assert cmd_plugin(_args("remove", name=DIST)) == 0, capsys.readouterr().err
    assert pip.calls == []  # the image has no pip, and pip cannot see --target anyway
    assert not any(plugin_dir.iterdir())  # the package is gone from the import path
    assert (ws / "docs" / "agents" / "retired" / "desk-monitor.yaml").is_file()

    # ...so the next sync does not rediscover it and write the agent back
    assert cmd_plugin(_args("sync")) == 0
    assert not (ws / "docs" / "agents" / "desk-monitor.yaml").exists()
    assert lockfile.read_lockfile().row(DIST) is None


def test_an_editable_install_finds_the_manifest_next_to_its_package(tmp_path, monkeypatch) -> None:
    """An editable dist's file list holds no package files, so the manifest
    beside the package was invisible ("ships no genus-plugin.yaml"). It is
    located through the package's import spec -- found, never imported."""
    import json

    from robothor.plugins.manifest import read_manifest_text

    src = tmp_path / "src"
    pkg = src / "example_agent"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("raise RuntimeError('must not be imported')\n")
    (pkg / "genus-plugin.yaml").write_text(_MANIFEST)
    monkeypatch.syspath_prepend(str(src))

    class _Editable:
        files = ()
        entry_points = (
            metadata.EntryPoint(name="example", value="example_agent:PLUGIN", group="genus.tools"),
        )

        def read_text(self, name):
            if name == "direct_url.json":
                return json.dumps({"url": "file:///src", "dir_info": {"editable": True}})
            return None

    assert read_manifest_text(_Editable()) == _MANIFEST


def test_a_non_editable_dist_does_not_look_beside_the_package(tmp_path, monkeypatch) -> None:
    from robothor.plugins.manifest import read_manifest_text

    src = tmp_path / "src"
    (src / "example_agent").mkdir(parents=True)
    (src / "example_agent" / "genus-plugin.yaml").write_text(_MANIFEST)
    monkeypatch.syspath_prepend(str(src))

    class _Plain:
        files = ()
        entry_points = (
            metadata.EntryPoint(name="example", value="example_agent:PLUGIN", group="genus.tools"),
        )

        def read_text(self, name):
            return None

    assert read_manifest_text(_Plain()) is None
