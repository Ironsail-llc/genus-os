"""``genus channel`` — list what can deliver, prove one works, add a token.

Three contracts decide whether this command is safe to run and worth running:

* **no credential reaches a terminal, a log, or ``argv``.** ``add`` takes a
  token; ``genus vault set`` already learned to prompt with ``getpass`` rather
  than accept it on a command line, and everything written here says WHERE the
  value went and at most a fingerprint of it. A token in a scrollback is a
  token in a bug report.
* **``verify`` reports what it proved, step by step, and its exit code means
  something.** 0 every step passed, 1 a step failed, 2 nothing to verify —
  unknown channel, or one this instance has not configured. An operator
  scripting around it needs those three apart.
* **``list`` names the channels a manifest could actually resolve**, which is
  the question ``failed:no_channel:<name>`` leaves an operator asking.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pytest

from robothor.cli.channel import cmd_channel

#: Visibly fake, and distinctive enough that searching output for it means
#: something. Any real token here would be a credential in the repository.
FAKE_BOT_TOKEN = "xoxb-test-not-a-real-token"
FAKE_APP_TOKEN = "xapp-test-not-a-real-token"
CHANNEL_ID = "C0000000000"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """A workspace of our own, with none of the box's own configuration."""
    from robothor.settings import reset_settings
    from robothor.settings.aliases import DEPRECATED_ALIASES

    for name in list(os.environ):
        if name.startswith(("ROBOTHOR_", "GENUS_")) or name in DEPRECATED_ALIASES:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    reset_settings()
    yield
    reset_settings()


def _args(**kwargs: Any) -> argparse.Namespace:
    kwargs.setdefault("json", False)
    return argparse.Namespace(**kwargs)


def _add_args(**kwargs: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "channel_command": "add",
        "name": "slack",
        "bot_token": None,
        "app_token": None,
        "verify_target": None,
        "to": None,
    }
    defaults.update(kwargs)
    return _args(**defaults)


@pytest.fixture
def exported(monkeypatch):
    """Tokens in the environment — the non-interactive path `add` supports.

    Passing them as flags is refused (see
    :class:`TestATokenOnACommandLineCannotBeTakenBack`), so this is how a test
    that is about the WRITE, not about the input path, supplies them.
    """

    def _set(bot: str | None = FAKE_BOT_TOKEN, app: str | None = FAKE_APP_TOKEN):
        for name, value in (
            ("ROBOTHOR_SLACK_BOT_TOKEN", bot),
            ("ROBOTHOR_SLACK_APP_TOKEN", app),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

    return _set


class _FakeVault:
    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    def __call__(self, key: str, value: str) -> None:
        self.written[key] = value


@pytest.fixture
def vault(monkeypatch, tmp_path):
    """A vault that exists and records what it was asked to store."""
    from robothor.cli import channel as channel_cmd

    (tmp_path / ".vault-key").write_bytes(b"0" * 32)
    fake = _FakeVault()
    monkeypatch.setattr(channel_cmd, "_vault_set", fake)
    return fake


class TestAddWritesTheCredentialSomewhereDeliberate:
    def test_add_slack_writes_through_the_secrets_accessor(self, vault, exported):
        """The KEY is the contract — a write under a name nothing reads is the
        failure ``vault/naming.py`` exists to make impossible."""
        exported()
        assert cmd_channel(_add_args()) == 0

        assert set(vault.written) == {
            "channels/slack/bot_token",
            "channels/slack/app_token",
        }

    def test_add_slack_never_prints_the_token(self, vault, exported, capsys, caplog):
        exported()
        with caplog.at_level("DEBUG"):
            cmd_channel(_add_args())
        captured = capsys.readouterr()

        for stream in (captured.out, captured.err, caplog.text):
            assert FAKE_BOT_TOKEN not in stream
            assert FAKE_APP_TOKEN not in stream
            # Not a prefix of it either: "xoxb-test-not-a" identifies the token
            # to anyone holding a copy, which is the whole risk.
            assert "xoxb-test" not in stream

    def test_add_says_where_it_wrote(self, vault, exported, capsys):
        exported()
        cmd_channel(_add_args())
        out = capsys.readouterr().out

        assert "vault" in out
        assert "channels/slack/bot_token" in out

    def test_add_falls_back_to_the_instance_env_file_without_a_vault(
        self, tmp_path, exported, capsys
    ):
        """The most common install has no master key. A vault-only ``add``
        would fail on it, and telling the operator to run three other commands
        first is how a channel never gets configured."""
        from robothor.secrets.env_file import instance_env_path, parse_env_file

        exported()
        assert cmd_channel(_add_args()) == 0

        path = instance_env_path(tmp_path)
        values = parse_env_file(path.read_text(encoding="utf-8"))
        assert values["ROBOTHOR_SLACK_BOT_TOKEN"] == FAKE_BOT_TOKEN
        assert values["ROBOTHOR_SLACK_APP_TOKEN"] == FAKE_APP_TOKEN
        assert str(path) in capsys.readouterr().out

    def test_the_env_file_is_private(self, tmp_path, exported):
        import stat

        exported(app=None)
        cmd_channel(_add_args())
        from robothor.secrets.env_file import instance_env_path

        mode = stat.S_IMODE(instance_env_path(tmp_path).stat().st_mode)
        assert mode == 0o600, f"the instance credential file is mode {mode:o}"

    def test_a_second_add_replaces_rather_than_stacks(self, tmp_path, exported):
        """A superseded token left in the file is still a readable credential."""
        from robothor.secrets.env_file import instance_env_path

        exported(app=None)
        cmd_channel(_add_args())
        exported(bot="xoxb-test-rotated-token", app=None)
        cmd_channel(_add_args())

        text = instance_env_path(tmp_path).read_text(encoding="utf-8")
        assert FAKE_BOT_TOKEN not in text
        assert text.count("ROBOTHOR_SLACK_BOT_TOKEN=") == 1

    def test_add_slack_prompts_without_echo_when_no_flag(self, vault, monkeypatch):
        """``input()`` echoes. A token on a terminal is a token on a screen
        share, and ``genus vault set`` set this precedent."""
        prompts: list[str] = []

        def _getpass(prompt: str = "") -> str:
            prompts.append(prompt)
            return FAKE_BOT_TOKEN if "bot" in prompt.lower() else FAKE_APP_TOKEN

        def _never(*_a: Any, **_k: Any) -> str:
            raise AssertionError("a credential must never be read with input()")

        monkeypatch.setattr("getpass.getpass", _getpass)
        monkeypatch.setattr("builtins.input", _never)
        # There IS an operator at a terminal here. Without this the command
        # correctly declines to prompt into a pipe.
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        assert cmd_channel(_add_args()) == 0
        assert len(prompts) == 2
        assert vault.written["channels/slack/bot_token"] == FAKE_BOT_TOKEN

    def test_the_verify_target_is_a_setting_not_a_secret(self, vault, exported, tmp_path):
        exported(app=None)
        cmd_channel(_add_args(verify_target=CHANNEL_ID))

        from robothor.settings.sources import config_yaml_path

        path = config_yaml_path()
        assert path is not None
        assert CHANNEL_ID in path.read_text(encoding="utf-8")
        assert "slack_verify_target" not in "".join(vault.written)

    def test_an_unknown_channel_cannot_be_added(self, capsys):
        assert cmd_channel(_add_args(name="teams")) == 2
        assert "teams" in capsys.readouterr().err


class TestVerifyExitCodes:
    @staticmethod
    def _verify_args(name: str = "slack", **kwargs: Any) -> argparse.Namespace:
        return _args(channel_command="verify", name=name, target=None, **kwargs)

    def _install(self, monkeypatch, steps, *, configured: bool = True):
        from robothor.engine.channels import register_channel, reset_channels

        class _Fake:
            name = "fake"
            inbound_router = None

            async def start(self) -> None: ...
            async def stop(self) -> None: ...
            async def send(self, target: str, text: str, **kw: Any) -> Any: ...

            async def health(self) -> dict[str, Any]:
                return {"channel": "fake", "configured": configured}

            async def verify(self, target: str | None = None) -> list[tuple[str, bool, str]]:
                return steps

        reset_channels()
        register_channel("fake", _Fake())
        monkeypatch.setattr("robothor.engine.channels.reset_channels", lambda: None, raising=False)
        return _Fake

    def test_every_step_passing_is_zero(self, monkeypatch, capsys):
        self._install(monkeypatch, [("auth.test", True, "ok"), ("post", True, "ok")])
        assert cmd_channel(self._verify_args("fake")) == 0
        assert "auth.test" in capsys.readouterr().out

    def test_any_step_failing_is_one(self, monkeypatch):
        self._install(monkeypatch, [("auth.test", True, "ok"), ("post", False, "missing_scope")])
        assert cmd_channel(self._verify_args("fake")) == 1

    def test_an_unconfigured_channel_is_two(self, monkeypatch):
        """Not a failure: an instance that never wanted Slack has not failed a
        verification it did not ask for.

        Read off `verify`'s own answer — one step named `configuration`, failed
        — rather than from a separate `health()` call, which cost a second
        authenticated round trip and reported a channel whose `health` raised as
        verifiable.
        """
        from robothor.engine.channels import UNCONFIGURED_STEP

        self._install(
            monkeypatch,
            [(UNCONFIGURED_STEP, False, "a token is set nowhere this instance reads")],
            configured=False,
        )
        assert cmd_channel(self._verify_args("fake")) == 2

    def test_a_failed_step_that_is_not_the_configuration_one_is_still_one(self, monkeypatch):
        """The exit-2 shape is ONE step under that exact name. A real failure
        that happens to be first must not be downgraded to "never set up"."""
        self._install(
            monkeypatch,
            [("auth.test", False, "invalid_auth"), ("conversations.list", False, "missing_scope")],
        )
        assert cmd_channel(self._verify_args("fake")) == 1

    def test_an_unknown_channel_is_two(self, capsys):
        assert cmd_channel(self._verify_args("teams")) == 2
        assert "teams" in capsys.readouterr().err

    def test_a_channel_with_no_verify_is_two_not_a_false_pass(self, capsys):
        """``telegram`` declares none. Reporting "0 of 0 steps failed" as a pass
        would be a green result nobody checked anything for."""
        assert cmd_channel(self._verify_args("telegram")) == 2
        assert "verify" in capsys.readouterr().err

    def test_the_failed_step_detail_reaches_the_operator(self, monkeypatch, capsys):
        self._install(
            monkeypatch,
            [("conversations.list", False, "missing_scope: the app needs channels:read")],
        )
        cmd_channel(self._verify_args("fake"))
        assert "channels:read" in capsys.readouterr().out


class TestList:
    def test_list_reports_registered_channels(self, capsys):
        assert cmd_channel(_args(channel_command="list")) == 0
        out = capsys.readouterr().out
        for name in ("telegram", "event_bus", "slack"):
            assert name in out

    def test_list_json_is_machine_readable(self, capsys):
        assert cmd_channel(_args(channel_command="list", json=True)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert {"telegram", "event_bus", "slack"} <= set(payload)
        assert payload["slack"]["configured"] is False


class TestTheVerbIsReachable:
    """A ``cmd_*`` implementation that is never wired into the parser is the
    2026-04-09 export/import/tenant defect, and ``test_cli_surface.py`` exists
    because it shipped."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["channel", "--help"],
            ["channel", "list", "--help"],
            ["channel", "verify", "--help"],
            ["channel", "add", "--help"],
        ],
    )
    def test_help_exits_zero(self, argv, capsys):
        from robothor.cli import main

        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()

    def test_channel_is_a_builtin_verb_plugins_may_not_claim(self):
        from robothor.cli import builtin_command_names

        assert "channel" in builtin_command_names()

    def test_list_dispatches(self):
        from unittest.mock import patch

        from robothor.cli import main

        with patch("robothor.cli.channel.cmd_channel", return_value=0) as command:
            assert main(["channel", "list"]) == 0
        assert command.call_args.args[0].channel_command == "list"


class TestATokenOnACommandLineCannotBeTakenBack:
    """``--bot-token`` is refused, and the reason is demonstrable.

    The first cut kept the flag and "scrubbed" it by reassigning ``sys.argv``.
    That rebinds a Python list; it does not touch the kernel's copy, so ``ps``
    and ``/proc/<pid>/cmdline`` went on showing the token for the whole run —
    which for ``add`` includes the vault round trip and the file write. The
    docstring claimed all three readers were closed and the test asserted
    ``sys.argv``, i.e. it certified precisely the one that worked. That is the
    inert-control shape this campaign keeps finding.

    There is no portable way to clear the kernel argv from CPython, so the flag
    is refused instead and the operator is sent to the prompt or the
    environment, neither of which appears in a command line.
    """

    def test_a_python_level_scrub_cannot_clear_proc_cmdline(self):
        """The premise, proved rather than asserted.

        A child process rebinds ``sys.argv`` and then reads its OWN
        ``/proc/self/cmdline``. If a Python-level scrub worked, the sentinel
        would be gone from it.
        """
        import subprocess
        import sys

        sentinel = "xoxb-test-sentinel-argv-token"
        child = (
            "import sys, pathlib;"
            "sys.argv = ['scrubbed'];"
            "print(pathlib.Path('/proc/self/cmdline').read_bytes().decode(errors='replace'))"
        )
        done = subprocess.run(  # noqa: S603 - a fixed interpreter and a literal script
            [sys.executable, "-c", child, sentinel],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert sentinel in done.stdout, (
            "the premise of this refusal is wrong: reassigning sys.argv DID "
            "clear /proc/self/cmdline, so a scrub would be a real control"
        )

    def test_the_bot_token_flag_is_refused_rather_than_scrubbed(self, vault, capsys):
        assert cmd_channel(_add_args(bot_token=FAKE_BOT_TOKEN)) == 2
        assert vault.written == {}, "a token from a command line was stored anyway"

        err = capsys.readouterr().err
        assert "/proc" in err or " ps " in err, "the refusal does not say why"
        assert FAKE_BOT_TOKEN not in err

    def test_the_app_token_flag_is_refused_too(self, vault):
        assert cmd_channel(_add_args(app_token=FAKE_APP_TOKEN)) == 2
        assert vault.written == {}

    def test_a_flag_is_refused_before_the_channel_name_is_even_checked(self, capsys):
        """A misspelled name used to return 2 first, so the credential problem
        was never reported and the advice to rotate never reached the operator."""
        assert cmd_channel(_add_args(name="teams", bot_token=FAKE_BOT_TOKEN)) == 2
        err = capsys.readouterr().err
        assert "command line" in err, "the name error hid the credential problem"

    def test_the_refusal_names_both_safe_paths(self, vault, capsys):
        cmd_channel(_add_args(bot_token=FAKE_BOT_TOKEN))
        err = capsys.readouterr().err
        assert "ROBOTHOR_SLACK_BOT_TOKEN" in err
        assert "prompt" in err.lower()


class TestEveryFailurePathIsAMessageNotATraceback:
    """``add`` holds two plaintext tokens in its frames for its whole run.

    A standard CPython traceback does not print locals — but `rich`, `cgitb`,
    Sentry and every `--verbose` handler anyone bolts on later do, and the frames
    of `_write_env_file` hold `pairs` and `body`: both tokens, formatted. So an
    exception must not leave this command. Every other exit here is a clean code
    with a sentence; the env-file write was the one path with neither.
    """

    def test_an_unwritable_workspace_is_a_sentence_and_an_exit_code(
        self, tmp_path, exported, capsys
    ):
        import stat

        exported()
        workspace = tmp_path / "readonly"
        workspace.mkdir()
        workspace.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x------
        try:
            os.environ["ROBOTHOR_WORKSPACE"] = str(workspace)
            rc = cmd_channel(_add_args())
        finally:
            workspace.chmod(stat.S_IRWXU)
            os.environ["ROBOTHOR_WORKSPACE"] = str(tmp_path)

        assert rc == 1, "the write failure escaped as an exception instead of an exit code"
        captured = capsys.readouterr()
        assert "Could not write" in captured.err
        for stream in (captured.out, captured.err):
            assert FAKE_BOT_TOKEN not in stream
            assert FAKE_APP_TOKEN not in stream
            assert "xoxb-test" not in stream

    def test_a_symlink_at_the_env_path_is_refused_and_its_target_untouched(
        self, tmp_path, exported, capsys
    ):
        """`secrets/env_file.py` refuses a symlink here on READ, for "it is the
        shape of 'make the daemon read a file it would not otherwise open'".
        The writer followed it: the linked-to file's contents were copied into
        the new genus.env verbatim, and the rename replaced the link with a
        regular file — so anything the operator could read and an attacker could
        point at landed in the one file the engine setdefaults into its own
        environment.
        """
        from robothor.secrets.env_file import instance_env_path

        decoy = tmp_path / "someone-elses-file"
        decoy.write_text("SOMEONE_ELSES_SECRET=hunter2\n", encoding="utf-8")
        link = instance_env_path(tmp_path)
        link.symlink_to(decoy)

        exported()
        assert cmd_channel(_add_args()) == 1

        assert link.is_symlink(), "the symlink was replaced by a regular file"
        assert decoy.read_text(encoding="utf-8") == "SOMEONE_ELSES_SECRET=hunter2\n"
        assert FAKE_BOT_TOKEN not in decoy.read_text(encoding="utf-8")
        assert "symlink" in capsys.readouterr().err

    def test_a_partial_vault_write_says_what_was_stored(self, vault, exported, monkeypatch, capsys):
        """Credentials go in one at a time. An operator told only "the vault
        refused the app token" will re-run, or assume nothing landed and go
        looking in the wrong place."""
        from robothor.cli import channel as channel_cmd

        stored: dict[str, str] = {}

        def _one_then_refuse(key: str, value: str) -> None:
            if stored:
                raise RuntimeError("the vault is full of bees")
            stored[key] = value

        monkeypatch.setattr(channel_cmd, "_vault_set", _one_then_refuse)
        exported()
        assert cmd_channel(_add_args()) == 1

        captured = capsys.readouterr()
        assert "channels/slack/bot_token" in captured.out, "the stored key was not reported"
        assert "WERE stored" in captured.err
        assert FAKE_BOT_TOKEN not in captured.out + captured.err
