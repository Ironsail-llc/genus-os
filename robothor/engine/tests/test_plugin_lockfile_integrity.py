"""A governance record that reports healthy while it is not is worse than none.

Whole-file corruption was handled from the start. What was not:

* **F2 — a single unreadable ROW.** The reader dropped it in silence.
  ``malformed`` stayed false, ``usable`` stayed true, and a lockfile holding
  one nameless row was indistinguishable from a sync that had never run: the
  plugin the operator had turned off went back to loading and all three doctor
  checks printed green.
* **F3 — ``sync`` over a file it could not read.** ``existing`` came back
  ``{}``, so every row was rebuilt with ``enabled=True`` — the one thing the
  function's own docstring promises it will never do. And the doctor's remedy
  line for a corrupt lockfile was "run `genus plugin sync`", so the platform
  walked the operator into it.
* **F7 — a relative configured path.** Resolved against the process's working
  directory, so the operator's shell and the daemon's systemd
  ``WorkingDirectory`` read different files and a disable reported success that
  the engine never saw.
* **F9 — an unwritable path.** ``IsADirectoryError`` out of `genus plugin sync`.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from robothor.plugins import loader, lockfile
from robothor.plugins.manifest import MANIFEST_NAME

_PAYLOAD = {"genus_contract_version": "1.0", "handlers": {"probe": lambda: None}}
_MANIFEST = "name: acme-tools\ncontract_version: 1\nhandlers:\n  - probe\n"


class _Dist:
    def __init__(self, name="acme-tools", version="1.0.0", manifest=_MANIFEST):
        self.name, self.version, self._manifest = name, version, manifest
        self.files: tuple[str, ...] = ()

    def read_text(self, filename):
        return self._manifest if filename == MANIFEST_NAME else None


class _EP:
    def __init__(self, name="probe", group="genus.tools", dist=None, tool="probe"):
        self.name, self.group, self.dist = name, group, dist or _Dist()
        self._tool = tool

    def load(self):
        return {"genus_contract_version": "1.0", "handlers": {self._tool: lambda: None}}


@pytest.fixture
def one_plugin():
    with patch.object(loader, "_discover", lambda: [_EP()]):
        yield


def _break_one_row(path, mutate):
    data = json.loads(path.read_text(encoding="utf-8"))
    data["plugins"] = [mutate(row) for row in data["plugins"]]
    path.write_text(json.dumps(data), encoding="utf-8")


# ── F2: an unreadable row is never dropped in silence ───────────────────


class TestUnreadableRows:
    def test_a_row_with_no_name_is_counted_not_dropped(self, plugin_lockfile, one_plugin):
        lockfile.sync()
        _break_one_row(plugin_lockfile, lambda row: {k: v for k, v in row.items() if k != "name"})

        lock = lockfile.read_lockfile()
        assert lock.bad_rows == (0,)
        assert lock.rows == {}
        assert lock.trustworthy is False

    def test_a_row_that_is_not_an_object_is_counted(self, plugin_lockfile, one_plugin):
        lockfile.sync()
        _break_one_row(plugin_lockfile, lambda row: [row["name"]])
        assert lockfile.read_lockfile().bad_rows == (0,)

    def test_it_warns_once_and_names_the_position(self, plugin_lockfile, one_plugin, caplog):
        lockfile.sync()
        _break_one_row(plugin_lockfile, lambda row: {"version": "1"})
        lockfile.forget_warnings()

        with caplog.at_level("WARNING"):
            lockfile.read_lockfile()
            lockfile.read_lockfile()
        warnings = [r for r in caplog.records if "cannot be read" in r.getMessage()]
        assert len(warnings) == 1
        assert "position(s) 0" in warnings[0].getMessage()

    def test_the_readable_rows_still_govern(self, plugin_lockfile):
        """The readable decisions must survive one bad edit.

        Treating the whole file as unusable would be simpler and would put
        every OTHER disabled plugin back into service, which is the failure
        this is guarding against, not a stricter version of it.
        """
        eps = [
            _EP("probe", dist=_Dist("acme-tools"), tool="probe"),
            _EP("other", dist=_Dist("other-tools"), tool="other_probe"),
        ]
        with patch.object(loader, "_discover", lambda: eps):
            lockfile.sync()
            lockfile.set_enabled("acme-tools", False)
            _break_one_row(
                plugin_lockfile,
                lambda row: (
                    {k: v for k, v in row.items() if k != "name"}
                    if row["name"] == "other-tools"
                    else row
                ),
            )
            result = loader.load_plugins()

        assert "probe" not in result.tools, "a readable disable was discarded by a bad row"
        assert "other_probe" in result.tools, "the unrecorded plugin must still load"
        assert [f.reason for f in result.failures] == [lockfile.DISABLED_REASON]

    def test_the_doctor_fails_and_names_the_count(self, plugin_lockfile, one_plugin):
        import asyncio

        from robothor.doctor.checks import plugins as plugin_checks
        from robothor.doctor.tests.conftest import make_ctx

        lockfile.sync()
        _break_one_row(plugin_lockfile, lambda row: {"version": "1"})
        check = next(c for c in plugin_checks.CHECKS if c.id == "plugins.lockfile")
        result = asyncio.run(check.run(make_ctx()))

        assert result.status == "fail"
        assert "1 row" in result.detail
        assert "--force" in result.detail, "the remedy must not be the one that destroys them"


# ── F3: sync refuses rather than erasing a decision it cannot read ──────


class TestSyncRefuses:
    def test_it_refuses_over_a_corrupt_file(self, plugin_lockfile, one_plugin):
        lockfile.sync()
        lockfile.set_enabled("acme-tools", False)
        plugin_lockfile.write_text("{{{ not json", encoding="utf-8")

        result = lockfile.sync()
        assert result.ok is False
        assert "re-enable" in result.refused
        assert "--force" in result.refused

    def test_a_refused_sync_writes_nothing(self, plugin_lockfile, one_plugin):
        lockfile.sync()
        plugin_lockfile.write_text("{{{", encoding="utf-8")
        before = plugin_lockfile.read_bytes()
        lockfile.sync()
        assert plugin_lockfile.read_bytes() == before

    def test_it_refuses_over_an_unreadable_row(self, plugin_lockfile, one_plugin):
        lockfile.sync()
        _break_one_row(plugin_lockfile, lambda row: {"version": "1"})
        assert lockfile.sync().ok is False

    def test_it_never_silently_re_enables(self, plugin_lockfile, one_plugin):
        """The regression itself: disable, corrupt, sync, and the plugin was on."""
        lockfile.sync()
        lockfile.set_enabled("acme-tools", False)
        plugin_lockfile.write_text('{"lockfile_version": 1, "plugins": [[1]]}', encoding="utf-8")
        lockfile.sync()

        raw = json.loads(plugin_lockfile.read_text(encoding="utf-8"))
        assert raw["plugins"] == [[1]], "sync rewrote a file it could not read"

    def test_force_rebuilds_and_says_nothing_was_carried(self, plugin_lockfile, one_plugin):
        lockfile.sync()
        plugin_lockfile.write_text("{{{", encoding="utf-8")
        result = lockfile.sync(force=True)
        assert result.ok is True
        assert result.added == ("acme-tools",)
        assert lockfile.read_lockfile().row("acme-tools").enabled is True

    def test_a_healthy_file_syncs_without_force(self, plugin_lockfile, one_plugin):
        assert lockfile.sync().ok is True
        assert lockfile.sync().ok is True


# ── F7: a relative path is the config dir's, never the shell's ─────────


class TestRelativePath:
    def test_a_relative_setting_resolves_against_the_config_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", "plugins.lock")
        assert lockfile.lockfile_path() == tmp_path / ".robothor" / "plugins.lock"

    def test_the_answer_does_not_depend_on_the_working_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", "plugins.lock")
        first = lockfile.lockfile_path()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert lockfile.lockfile_path() == first

    def test_an_absolute_setting_is_used_as_given(self, tmp_path, monkeypatch):
        target = tmp_path / "somewhere" / "custom.lock"
        monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(target))
        assert lockfile.lockfile_path() == target


# ── F9: an unwritable path is a sentence, not a traceback ──────────────


class TestUnwritablePath:
    def test_sync_reports_rather_than_raising(self, tmp_path, monkeypatch, one_plugin):
        occupied = tmp_path / "plugins.lock"
        occupied.mkdir()
        monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(occupied))
        result = lockfile.sync()
        assert result.ok is False
        # Names what is actually wrong. The first version re-derived the
        # problem at the refusal and told an operator whose path was a
        # DIRECTORY that their JSON did not parse.
        assert "IsADirectoryError" in result.refused

    def test_the_cli_exits_2_rather_than_printing_a_traceback(
        self, tmp_path, monkeypatch, one_plugin, capsys
    ):
        import argparse

        from robothor.cli.plugins import cmd_plugin

        occupied = tmp_path / "plugins.lock"
        occupied.mkdir()
        monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(occupied))
        assert cmd_plugin(argparse.Namespace(plugin_command="sync", force=False)) == 2
        assert "IsADirectoryError" in capsys.readouterr().err

    def test_enable_exits_2_rather_than_raising(self, tmp_path, monkeypatch, one_plugin, capsys):
        import argparse

        from robothor.cli.plugins import cmd_plugin

        lock = tmp_path / "plugins.lock"
        monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(lock))
        lockfile.sync()
        lock.unlink()
        lock.mkdir()
        code = cmd_plugin(argparse.Namespace(plugin_command="disable", name="acme-tools"))
        assert code == 2
        assert "acme-tools" in capsys.readouterr().err


# ── F5: no test reads the operator's real lockfile ─────────────────────


class TestTheSuiteNeverReadsTheRealLockfile:
    def test_the_autouse_fixture_points_somewhere_disposable(self, plugin_lockfile, tmp_path):
        assert lockfile.lockfile_path() == plugin_lockfile
        assert plugin_lockfile.is_relative_to(tmp_path)

    def test_a_bare_load_never_opens_the_instance_path(self, plugin_lockfile, one_plugin):
        """Prove it, rather than trusting the fixture.

        `load_plugins()` consults the lockfile on every load, so without the
        autouse override every pre-existing plugin test would read whatever
        `genus plugin sync` last wrote on the developer's box.
        """
        from pathlib import Path

        opened: list[str] = []
        real_read_text = Path.read_text

        def _record(self, *args, **kwargs):
            opened.append(str(self))
            return real_read_text(self, *args, **kwargs)

        with patch.object(Path, "read_text", _record):
            loader.load_plugins()

        # Narrowly about the LOCKFILE. Settings resolution reads the instance's
        # config.yaml and always has; what must never happen is a load
        # consulting a plugins.lock outside this test's own directory.
        assert str(plugin_lockfile) in opened, "the fixture's lockfile was never consulted"
        stray = [
            p for p in opened if p.endswith(lockfile.LOCKFILE_NAME) and p != str(plugin_lockfile)
        ]
        assert not stray, stray

    def test_the_loader_accepts_an_explicit_path(self, tmp_path, one_plugin):
        elsewhere = tmp_path / "explicit.lock"
        lockfile.sync(elsewhere)
        lockfile.set_enabled("acme-tools", False, elsewhere)

        assert "probe" in loader.load_plugins().tools, "the default path was not used"
        assert loader.load_plugins(lockfile_path=elsewhere).tools == {}
