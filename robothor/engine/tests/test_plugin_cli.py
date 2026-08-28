"""`robothor plugin` — the operator surface the seam never had.

Nothing told an operator what was installed, what it contributed, or why
something was refused. That was tolerable while the answer was always
"nothing"; it is not now that plugins load.

It matters most for the manifest ladder. `ROBOTHOR_PLUGIN_MANIFEST_MODE`
defaults to `observe` because requiring a genus-plugin.yaml is a breaking
change for anything published before it existed — so the question "which of my
installed plugins would stop loading if I promote to enforce?" is exactly the
one an operator has to answer, and it had no answer at all.
"""

from __future__ import annotations

from unittest.mock import patch

from robothor.cli.plugins import cmd_plugin_list
from robothor.plugins import loader


class _EP:
    def __init__(self, name, group, payload, dist=None):
        self.name, self.group, self.dist = name, group, dist
        self._payload = payload

    def load(self):
        return self._payload


_PAYLOAD = {"genus_contract_version": "1.0", "handlers": {"x": lambda: None}}


def test_it_names_a_refused_plugin_and_why(capsys, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", "enforce")
    with patch.object(loader, "_discover", lambda: [_EP("legacy", "genus.tools", _PAYLOAD)]):
        assert cmd_plugin_list() == 0
    out = capsys.readouterr().out
    assert "legacy" in out
    assert "genus-plugin.yaml" in out
    assert "not a crash" in out, "an operator must know the engine still runs"


def test_observe_says_it_does_not_protect(capsys, monkeypatch):
    """A mode line reading 'observe' must not imply containment it lacks."""
    monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", "observe")
    with patch.object(loader, "_discover", list):
        cmd_plugin_list()
    out = capsys.readouterr().out
    assert "still IMPORTS" in out
    assert "only in enforce" in out


def test_no_plugins_points_at_the_worked_example(capsys, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", "enforce")
    with patch.object(loader, "_discover", list):
        cmd_plugin_list()
    out = capsys.readouterr().out
    assert "No plugins installed" in out
    assert "genus-hostinfo" in out


def test_the_verb_is_registered_on_the_parser():
    """A command nothing can reach is the failure this session is about."""
    from robothor.cli import _build_parser

    actions = [a for a in _build_parser()._actions if isinstance(getattr(a, "choices", None), dict)]
    assert actions, "no subparsers found"
    assert "plugin" in actions[0].choices
