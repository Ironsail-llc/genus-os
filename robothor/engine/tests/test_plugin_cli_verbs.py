"""``genus plugin`` beyond ``list`` — the verbs the lockfile made possible.

``list`` could only ever describe. With a record of what was accepted there is
something to *change*, and the operator surface has to be the one that changes
it: a governance file an operator edits by hand is a file that ends up
malformed, and the loader's own answer to a malformed one is to ignore it.

Three properties are pinned here and nowhere else:

* **An unknown name is exit 2 and a sentence**, never a silently created row.
  A disable that invented a row for a typo would report success and change
  nothing — the exact shape of every inert control this platform has shipped.
* **Nothing here signals the engine.** The CLI writes a file; the running
  process is still serving the old set until somebody reloads it. Saying so on
  every mutation is the difference between a control and a control an operator
  believes has already applied.
* **``list`` says which plugins are off and what the verdict is**, because the
  question an operator asks after disabling something is whether it took.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from robothor.cli.plugins import cmd_plugin
from robothor.plugins import loader, lockfile
from robothor.plugins.manifest import MANIFEST_NAME

_PAYLOAD = {"genus_contract_version": "1.0", "handlers": {"probe": lambda: None}}
_MANIFEST = "name: acme-tools\ncontract_version: 1\nhandlers:\n  - probe\n"


class _Dist:
    def __init__(self, name="acme-tools", version="1.2.3", manifest=_MANIFEST):
        self.name, self.version, self._manifest = name, version, manifest
        self.files: tuple[str, ...] = ()

    def read_text(self, filename):
        return self._manifest if filename == MANIFEST_NAME else None


class _EP:
    def __init__(self, name="probe", group="genus.tools", dist=None):
        self.name, self.group, self.dist = name, group, dist or _Dist()

    def load(self):
        return _PAYLOAD


def _args(command, **kw):
    return argparse.Namespace(plugin_command=command, **kw)


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """One installed plugin and a lockfile of this test's own."""
    monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(tmp_path / "plugins.lock"))
    monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", "enforce")
    lockfile.forget_warnings()
    eps = [_EP("probe", "genus.tools"), _EP("probe", "genus.schemas")]
    with patch.object(loader, "_discover", lambda: eps):
        yield eps


class TestSync:
    def test_it_records_and_says_what_it_recorded(self, installed, capsys):
        assert cmd_plugin(_args("sync")) == 0
        out = capsys.readouterr().out
        assert "acme-tools" in out
        assert "recorded" in out.lower()
        assert lockfile.read_lockfile().row("acme-tools") is not None

    def test_it_names_a_distribution_that_is_gone(self, installed, capsys):
        cmd_plugin(_args("sync"))
        with patch.object(loader, "_discover", list):
            cmd_plugin(_args("sync"))
        out = capsys.readouterr().out
        assert "acme-tools" in out
        assert "no longer installed" in out


class TestEnableDisable:
    def test_disable_writes_the_row_and_says_it_needs_a_reload(self, installed, capsys):
        cmd_plugin(_args("sync"))
        assert cmd_plugin(_args("disable", name="acme-tools")) == 0
        out = capsys.readouterr().out
        assert "SIGHUP" in out, "an operator must know the running engine is unchanged"
        assert lockfile.read_lockfile().row("acme-tools").enabled is False

    def test_enable_puts_it_back(self, installed, capsys):
        cmd_plugin(_args("sync"))
        cmd_plugin(_args("disable", name="acme-tools"))
        assert cmd_plugin(_args("enable", name="acme-tools")) == 0
        assert lockfile.read_lockfile().row("acme-tools").enabled is True
        assert "SIGHUP" in capsys.readouterr().out

    def test_an_unknown_name_exits_2_and_explains(self, installed, capsys):
        cmd_plugin(_args("sync"))
        assert cmd_plugin(_args("disable", name="acme-toolz")) == 2
        err = capsys.readouterr().err
        assert "acme-toolz" in err
        assert "sync" in err, "the fix has to be named, not just the failure"

    def test_it_refuses_before_a_sync_has_ever_run(self, installed, capsys):
        """No lockfile means no rows, so there is nothing to flip — and
        inventing one would record a decision about a plugin nothing checked."""
        assert cmd_plugin(_args("disable", name="acme-tools")) == 2
        assert "sync" in capsys.readouterr().err


class TestList:
    def test_it_shows_enablement_and_the_verdict(self, installed, capsys):
        cmd_plugin(_args("sync"))
        cmd_plugin(_args("disable", name="acme-tools"))
        capsys.readouterr()
        assert cmd_plugin(_args("list")) == 0
        out = capsys.readouterr().out
        assert "acme-tools" in out
        assert "disabled" in out
        assert "unscanned" in out

    def test_a_disabled_plugin_contributes_nothing(self, installed, capsys):
        cmd_plugin(_args("sync"))
        cmd_plugin(_args("disable", name="acme-tools"))
        capsys.readouterr()
        cmd_plugin(_args("list"))
        out = capsys.readouterr().out
        assert lockfile.DISABLED_REASON in out

    def test_a_deliberate_disable_is_not_listed_as_a_fault(self, installed, capsys):
        """The operator's own decision must not come back as "fix the cause".

        It was printed under `Refused:`, whose footer says "Fix the cause or
        uninstall the distribution" — advice to undo a choice they had just
        made on purpose.
        """
        cmd_plugin(_args("sync"))
        cmd_plugin(_args("disable", name="acme-tools"))
        capsys.readouterr()
        cmd_plugin(_args("list"))
        out = capsys.readouterr().out

        assert "Disabled:" in out
        assert "genus plugin enable" in out
        assert "uninstall the distribution" not in out
        assert "Refused:" not in out

    def test_a_real_refusal_still_says_fix_or_uninstall(self, monkeypatch, capsys):
        monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", "enforce")

        class _Broken(_EP):
            def load(self):
                return {"genus_contract_version": "0.1", "handlers": {"x": lambda: None}}

        with patch.object(loader, "_discover", lambda: [_Broken("probe", "genus.tools")]):
            cmd_plugin(_args("list"))
        out = capsys.readouterr().out
        assert "Refused:" in out
        assert "uninstall the distribution" in out

    def test_no_subcommand_still_lists(self, installed, capsys):
        assert cmd_plugin(argparse.Namespace()) == 0
        assert "acme-tools" in capsys.readouterr().out


class TestInfo:
    def test_it_reports_the_manifest_the_groups_and_the_lock_row(self, installed, capsys):
        cmd_plugin(_args("sync"))
        capsys.readouterr()
        assert cmd_plugin(_args("info", name="acme-tools")) == 0
        out = capsys.readouterr().out
        assert "acme-tools" in out
        assert "1.2.3" in out
        assert "genus.tools" in out and "genus.schemas" in out
        assert "unscanned" in out
        assert "loaded" in out

    def test_it_says_when_a_plugin_is_disabled(self, installed, capsys):
        cmd_plugin(_args("sync"))
        cmd_plugin(_args("disable", name="acme-tools"))
        capsys.readouterr()
        cmd_plugin(_args("info", name="acme-tools"))
        assert "disabled" in capsys.readouterr().out

    def test_an_unknown_name_exits_2(self, installed, capsys):
        assert cmd_plugin(_args("info", name="nothing-like-this")) == 2
        assert "nothing-like-this" in capsys.readouterr().err
