"""The record of what was installed on purpose, and what it looked like then.

Until this file there was no way to turn an installed plugin OFF short of
uninstalling the distribution, no record of what an operator had accepted, and
nothing at all that noticed a plugin's declaration changing underneath a
running engine. ``pip install`` was both the install step and the entire
governance story.

The lockfile is the missing record. Four properties are what it is FOR, and
each is pinned here:

* **Opt-in.** No lockfile at all is today's behaviour exactly — a platform that
  refused every plugin until an operator ran a new command would be an upgrade
  that breaks every install that already works.
* **A row with ``enabled: false`` is never imported.** Not "loaded and hidden":
  the refusal happens before ``ep.load()``, which is the only point at which
  refusing means anything (see ``test_plugin_refusal_precedes_import``).
* **A manifest that changed since it was recorded is refused**, the same rule
  ``verify_adapter_integrity`` applies to a pinned stdio command — a swapped
  declaration is the interesting attack, and "it used to be fine" is not a
  verification.
* **A corrupt lockfile never blocks boot.** The file is operator convenience;
  an unparseable one degrades to "no lockfile" and is reported by the doctor,
  because a governance record that can brick an engine will be deleted rather
  than fixed.
"""

from __future__ import annotations

import json
import os
import stat
from unittest.mock import patch

import pytest

from robothor.plugins import loader, lockfile
from robothor.plugins.manifest import MANIFEST_NAME

_PAYLOAD = {"genus_contract_version": "1.0", "handlers": {"probe": lambda: None}}

_MANIFEST = "name: acme-tools\ncontract_version: 1\nhandlers:\n  - probe\n"


class _Dist:
    """The packaging-layer view of an installed distribution.

    Only what ``read_manifest`` and the lockfile touch: a name, a version and
    the manifest text, read through the metadata API and never by importing
    the package.
    """

    def __init__(self, name: str, version: str = "0.1.0", manifest: str | None = _MANIFEST):
        self.name = name
        self.version = version
        self._manifest = manifest
        self.files: tuple[str, ...] = ()

    def read_text(self, filename: str) -> str | None:
        return self._manifest if filename == MANIFEST_NAME else None


class _EP:
    def __init__(self, name, group="genus.tools", payload=None, dist=None):
        self.name, self.group, self.dist = name, group, dist
        self._payload = payload if payload is not None else _PAYLOAD

    def load(self):
        return self._payload


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    """Point the lockfile at this test's own directory, never the box's."""
    path = tmp_path / "plugins.lock"
    monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(path))
    lockfile.forget_warnings()
    return path


class TestPath:
    def test_the_setting_wins(self, lock_path):
        assert lockfile.lockfile_path() == lock_path

    def test_it_defaults_under_the_instance_config_directory(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_PLUGIN_LOCKFILE", raising=False)
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        resolved = lockfile.lockfile_path()
        assert resolved == tmp_path / ".robothor" / lockfile.LOCKFILE_NAME

    def test_absent_until_written(self, lock_path):
        lock = lockfile.read_lockfile()
        assert lock.present is False
        assert lock.rows == {}


class TestSync:
    def test_it_records_one_row_per_distribution(self, lock_path):
        dist = _Dist("acme-tools", "1.2.3")
        eps = [
            _EP("probe", "genus.tools", dist=dist),
            _EP("probe", "genus.schemas", dist=dist),
        ]
        with patch.object(loader, "_discover", lambda: eps):
            result = lockfile.sync()

        assert [row.name for row in result.recorded] == ["acme-tools"]
        row = result.recorded[0]
        assert row.version == "1.2.3"
        assert row.enabled is True
        assert row.verdict == "unscanned", "a verdict nothing scanned must never read 'safe'"
        assert sorted(row.kinds) == ["genus.schemas", "genus.tools"]
        assert row.manifest_sha256 == lockfile.manifest_digest(dist)
        assert row.recorded_at

    def test_the_file_is_json_and_only_the_operator_may_read_it(self, lock_path):
        with patch.object(loader, "_discover", lambda: [_EP("probe", dist=_Dist("acme-tools"))]):
            lockfile.sync()

        data = json.loads(lock_path.read_text(encoding="utf-8"))
        assert data["lockfile_version"] == lockfile.LOCKFILE_VERSION
        assert [p["name"] for p in data["plugins"]] == ["acme-tools"]
        mode = stat.S_IMODE(os.stat(lock_path).st_mode)
        assert mode == 0o600, f"lockfile is mode {mode:o}"

    def test_a_second_sync_keeps_the_recorded_enablement(self, lock_path):
        dist = _Dist("acme-tools")
        with patch.object(loader, "_discover", lambda: [_EP("probe", dist=dist)]):
            lockfile.sync()
            assert lockfile.set_enabled("acme-tools", False) is not None
            lockfile.sync()

        row = lockfile.read_lockfile().row("acme-tools")
        assert row is not None and row.enabled is False, "sync re-enabled a disabled plugin"

    def test_it_reports_a_distribution_that_is_gone(self, lock_path):
        with patch.object(loader, "_discover", lambda: [_EP("probe", dist=_Dist("acme-tools"))]):
            lockfile.sync()
        with patch.object(loader, "_discover", list):
            result = lockfile.sync()

        assert result.removed == ("acme-tools",)
        assert lockfile.read_lockfile().rows == {}

    def test_it_re_records_a_manifest_that_changed(self, lock_path):
        with patch.object(loader, "_discover", lambda: [_EP("probe", dist=_Dist("acme-tools"))]):
            lockfile.sync()
        before = lockfile.read_lockfile().row("acme-tools")

        changed = _Dist("acme-tools", manifest=_MANIFEST + "services:\n  - anything\n")
        with patch.object(loader, "_discover", lambda: [_EP("probe", dist=changed)]):
            result = lockfile.sync()

        assert result.updated == ("acme-tools",)
        after = lockfile.read_lockfile().row("acme-tools")
        assert before is not None and after is not None
        assert after.manifest_sha256 != before.manifest_sha256


class TestEnableDisable:
    def test_an_unknown_name_is_not_invented(self, lock_path):
        with patch.object(loader, "_discover", lambda: [_EP("probe", dist=_Dist("acme-tools"))]):
            lockfile.sync()
        assert lockfile.set_enabled("not-installed", False) is None
        assert set(lockfile.read_lockfile().rows) == {"acme-tools"}

    def test_it_round_trips(self, lock_path):
        with patch.object(loader, "_discover", lambda: [_EP("probe", dist=_Dist("acme-tools"))]):
            lockfile.sync()
        assert lockfile.set_enabled("acme-tools", False).enabled is False
        assert lockfile.read_lockfile().row("acme-tools").enabled is False
        assert lockfile.set_enabled("acme-tools", True).enabled is True
        assert lockfile.read_lockfile().row("acme-tools").enabled is True


class TestCorruption:
    def test_a_corrupt_file_reads_as_absent_and_says_so(self, lock_path, caplog):
        lock_path.write_text("{not json at all", encoding="utf-8")
        lock = lockfile.read_lockfile()
        assert lock.malformed is True
        assert lock.rows == {}
        assert lock.present is True

    def test_a_corrupt_file_does_not_stop_a_plugin_loading(self, lock_path):
        lock_path.write_text("[[[", encoding="utf-8")
        eps = [_EP("probe", dist=_Dist("acme-tools"))]
        with patch.object(loader, "_discover", lambda: eps):
            result = loader.load_plugins()
        assert "probe" in result.tools, "a corrupt governance file blocked boot"
        assert result.failures == []

    def test_a_list_where_an_object_belongs_is_malformed_not_a_crash(self, lock_path):
        lock_path.write_text('["acme-tools"]', encoding="utf-8")
        assert lockfile.read_lockfile().malformed is True


class TestTheLoaderConsultsIt:
    def test_no_lockfile_is_todays_behaviour(self, lock_path):
        eps = [_EP("probe", dist=_Dist("acme-tools"))]
        with patch.object(loader, "_discover", lambda: eps):
            result = loader.load_plugins()
        assert "probe" in result.tools
        assert result.failures == []

    def test_a_distribution_with_no_row_still_loads(self, lock_path):
        """The lockfile is opt-in: syncing one plugin must not refuse another."""
        with patch.object(loader, "_discover", lambda: [_EP("probe", dist=_Dist("acme-tools"))]):
            lockfile.sync()
        other = [_EP("second", dist=_Dist("other-tools"))]
        with patch.object(loader, "_discover", lambda: other):
            result = loader.load_plugins()
        assert "probe" in result.tools
        assert result.failures == []

    def test_a_disabled_plugin_is_never_imported(self, lock_path):
        imported: list[str] = []

        class _Exploding(_EP):
            def load(self):
                imported.append(self.name)
                return _PAYLOAD

        eps = [_Exploding("probe", dist=_Dist("acme-tools"))]
        with patch.object(loader, "_discover", lambda: eps):
            lockfile.sync()
            lockfile.set_enabled("acme-tools", False)
            result = loader.load_plugins()

        assert imported == [], "a disabled plugin's module body ran anyway"
        assert result.tools == {}
        assert [f.reason for f in result.failures] == [lockfile.DISABLED_REASON]
        assert result.failures[0].name == "probe"

    def test_a_manifest_swapped_after_sync_is_refused(self, lock_path):
        imported: list[str] = []

        class _Exploding(_EP):
            def load(self):
                imported.append(self.name)
                return _PAYLOAD

        with patch.object(
            loader, "_discover", lambda: [_Exploding("probe", dist=_Dist("acme-tools"))]
        ):
            lockfile.sync()

        swapped = _Dist("acme-tools", manifest=_MANIFEST + "services:\n  - takeover\n")
        with patch.object(loader, "_discover", lambda: [_Exploding("probe", dist=swapped)]):
            result = loader.load_plugins()

        assert imported == [], "the swapped plugin was imported before it was refused"
        assert result.tools == {}
        assert result.failures[0].reason == lockfile.DRIFT_REASON

    def test_a_plugin_that_loses_its_manifest_is_refused(self, lock_path):
        with patch.object(loader, "_discover", lambda: [_EP("probe", dist=_Dist("acme-tools"))]):
            lockfile.sync()
        stripped = _Dist("acme-tools", manifest=None)
        with patch.object(loader, "_discover", lambda: [_EP("probe", dist=stripped)]):
            result = loader.load_plugins()
        assert [f.reason for f in result.failures] == [lockfile.DRIFT_REASON]

    def test_an_unchanged_manifest_loads(self, lock_path):
        eps = [_EP("probe", dist=_Dist("acme-tools"))]
        with patch.object(loader, "_discover", lambda: eps):
            lockfile.sync()
            result = loader.load_plugins()
        assert "probe" in result.tools
        assert result.failures == []


class TestReloadPicksItUp:
    def test_disable_then_reload_removes_the_tools_and_names_the_plugin(self, lock_path):
        """`genus plugin disable X` + SIGHUP, with no restart."""
        eps = [_EP("probe", dist=_Dist("acme-tools"))]
        with patch.object(loader, "_discover", lambda: eps):
            lockfile.sync()
            assert "probe" in loader.load_plugins().tools

            lockfile.set_enabled("acme-tools", False)
            loader.reload_plugins()
            after = loader.load_plugins()
            assert after.tools == {}
            assert [f.name for f in after.failures] == ["probe"]

            lockfile.set_enabled("acme-tools", True)
            loader.reload_plugins()
            assert "probe" in loader.load_plugins().tools
