"""A plugin should be able to add an operator command.

`robothor <verb>` is a fixed list: every subcommand is an `add_parser` call
and an `if args.command == ...` branch inside the CLI. An instance shipping
its own operational verb — a restore drill, a tenant report — had to patch
the platform to expose it.

Commands are `ctx.commands` in DeepSeek Harness, and this is one of the
extension kinds behind the breadth gap.

Built-ins win, always. A package that could claim `migrate` or `snapshot`
would be redefining the operator's recovery tools, so the built-in verb
names are passed to the loader as reserved and a plugin claiming one is
refused and reported rather than silently ignored.
"""

from __future__ import annotations

import pytest

from robothor.plugins import reload_plugins


def _cmd(func=None, **over):
    spec = {"help": "a plugin command", "func": func or (lambda args: 0)}
    spec.update(over)
    return spec


class _CmdEP:
    group = "genus.commands"

    def __init__(self, commands: dict, name: str = "testcmds"):
        self.name = name
        self._commands = commands

    def load(self):
        return {"genus_contract_version": "1.0", "commands": self._commands}


@pytest.fixture
def install(monkeypatch):
    def _install(commands: dict):
        from robothor.plugins import loader

        monkeypatch.setattr(loader, "_discover", lambda: [_CmdEP(commands)])
        reload_plugins()

    yield _install
    from robothor.plugins import loader

    monkeypatch.setattr(loader, "_discover", list)
    reload_plugins()


class TestTheGroupExists:
    def test_the_loader_declares_it(self):
        from robothor.plugins.loader import _GROUPS

        assert _GROUPS.get("genus.commands") == "commands"

    def test_the_pluginset_carries_it(self, install):
        from robothor.plugins import load_plugins

        install({"drill": _cmd()})
        assert "drill" in (load_plugins(reserved_names=set()).commands or {})


class TestResolution:
    def test_a_valid_command_resolves(self, install):
        from robothor.cli import plugin_commands

        install({"drill": _cmd(help="run a restore drill")})
        cmds = plugin_commands()
        assert "drill" in cmds
        assert cmds["drill"]["help"] == "run a restore drill"

    def test_a_command_without_a_callable_is_skipped(self, install):
        from robothor.cli import plugin_commands

        install({"broken": {"help": "no func"}, "fine": _cmd()})
        cmds = plugin_commands()
        assert "fine" in cmds
        assert "broken" not in cmds

    def test_a_non_identifier_verb_is_skipped(self, install):
        """A verb has to be usable on a command line."""
        from robothor.cli import plugin_commands

        install({"not a verb": _cmd(), "ok-verb": _cmd()})
        cmds = plugin_commands()
        assert "ok-verb" in cmds
        assert "not a verb" not in cmds

    def test_a_broken_plugin_yields_no_commands(self, monkeypatch):
        from robothor.cli import plugin_commands
        from robothor.plugins import loader

        class _Boom:
            name = "boom"
            group = "genus.commands"

            def load(self):
                raise RuntimeError("bad package")

        monkeypatch.setattr(loader, "_discover", lambda: [_Boom()])
        reload_plugins()
        assert plugin_commands() == {}


class TestBuiltinsWin:
    @pytest.mark.parametrize("verb", ["migrate", "snapshot", "serve", "version"])
    def test_a_plugin_cannot_claim_a_builtin_verb(self, install, verb):
        """These are the operator's recovery tools. A package must not own them."""
        from robothor.cli import plugin_commands

        install({verb: _cmd()})
        assert verb not in plugin_commands(), f"a plugin claimed the built-in {verb!r}"

    def test_the_builtin_list_is_derived_not_hand_maintained(self):
        """A hand-copied verb list would drift the moment a command is added.

        This project has been bitten three times by a second copy of a rule
        that stopped matching the first.
        """
        from robothor.cli import builtin_command_names

        names = builtin_command_names()
        assert {"migrate", "snapshot", "serve"} <= names
        assert len(names) > 8


class TestItIsReachable:
    def test_the_cli_registers_plugin_commands(self):
        """Resolution nothing wires into argparse is a function, not a verb."""
        import inspect

        from robothor import cli

        src = inspect.getsource(cli)
        assert "plugin_commands(" in src
        assert src.count("plugin_commands(") >= 2, (
            "plugin_commands is defined but never consumed by the parser/dispatch"
        )


class TestDispatch:
    """Registering a subparser is not running the command."""

    def test_the_plugin_function_is_actually_invoked(self, install):
        from robothor.cli import main

        called = {}

        def _run(args):
            called["yes"] = True
            return 7

        install({"drill": _cmd(func=_run)})
        rc = main(["drill"])
        assert called.get("yes"), "the plugin command was registered but never dispatched"
        assert rc == 7, "the plugin's exit code was discarded"

    def test_a_failing_plugin_command_does_not_crash_the_cli(self, install):
        from robothor.cli import main

        def _boom(args):
            raise RuntimeError("the command failed")

        install({"drill": _cmd(func=_boom)})
        rc = main(["drill"])
        assert rc != 0, "a failed plugin command reported success"

    def test_builtin_dispatch_is_unaffected(self, install):
        from robothor.cli import main

        install({"drill": _cmd()})
        rc = main(["version"])
        assert rc == 0
