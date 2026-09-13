"""One reader for this instance's Slack credentials — or four surfaces disagree.

The defect this pins shipped in the first cut of the channel: ``genus channel
add slack`` writes both tokens to the **vault** whenever the instance has a
master key, which is the normal full install. But the daemon's inbound gate and
``SlackBot.start`` each read ``os.environ`` directly, and the doctor read
``ctx.settings`` — neither of which consults the vault, and nothing preloads
channel credentials into the process environment (``key_pool`` filters its
vault→env export down to *provider* key names).

So one box answered the question "is Slack configured?" three different ways at
once: ``genus channel list`` said yes, ``genus doctor`` said "not configured",
and the Socket Mode bot silently never started — the env gate logs nothing at
all when it declines. Meanwhile ``genus channel verify slack`` printed four
green rows, including one whose docstring claims it proves "whether the inbound
half will start".

Every surface now asks :func:`slack_credentials`, so there is one answer. These
tests are the proof: with the tokens ONLY in a vault, all three say configured;
with them nowhere, all three say not configured.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from robothor.engine.channels.slack_credentials import (
    APP_TOKEN_ENV,
    APP_TOKEN_VAULT_KEY,
    BOT_TOKEN_ENV,
    BOT_TOKEN_VAULT_KEY,
    slack_credentials,
)

FAKE_BOT_TOKEN = "xoxb-test-not-a-real-token"
FAKE_APP_TOKEN = "xapp-test-not-a-real-token"


@pytest.fixture(autouse=True)
def _no_ambient_slack(monkeypatch, tmp_path):
    """Neither this box's environment nor its workspace reaches a test."""
    from robothor.settings import reset_settings

    monkeypatch.delenv(BOT_TOKEN_ENV, raising=False)
    monkeypatch.delenv(APP_TOKEN_ENV, raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def vault_only(monkeypatch):
    """Both tokens in a vault and nowhere else — the default ``add`` destination."""
    rows = {BOT_TOKEN_VAULT_KEY: FAKE_BOT_TOKEN, APP_TOKEN_VAULT_KEY: FAKE_APP_TOKEN}

    def _read(name: str, vault_key: str | None, tenant_id: str, *, live: bool = False):
        return rows.get(vault_key or ""), True

    monkeypatch.setattr("robothor.secrets._vault_read", _read)
    return rows


@pytest.fixture
def empty_vault(monkeypatch):
    """A vault that answers and holds nothing. Not an unavailable one."""

    def _read(name: str, vault_key: str | None, tenant_id: str, *, live: bool = False):
        return None, True

    monkeypatch.setattr("robothor.secrets._vault_read", _read)


class TestTheReaderItself:
    def test_the_environment_wins_over_the_vault(self, vault_only, monkeypatch):
        """An operator who exported a token meant it, and a rotation that
        reaches the environment must not be shadowed by a stale vault row."""
        monkeypatch.setenv(BOT_TOKEN_ENV, "xoxb-test-exported-token")
        found = slack_credentials()

        assert found.bot_token == "xoxb-test-exported-token"
        assert found.bot_source == "env"
        assert found.app_source == "vault"

    def test_the_vault_answers_when_the_environment_does_not(self, vault_only):
        found = slack_credentials()
        assert (found.bot_token, found.app_token) == (FAKE_BOT_TOKEN, FAKE_APP_TOKEN)
        assert found.can_send is True
        assert found.can_listen is True

    def test_nothing_anywhere_is_not_configured(self, empty_vault):
        found = slack_credentials()
        assert (found.bot_token, found.app_token) == (None, None)
        assert found.can_send is False
        assert found.can_listen is False

    def test_a_bot_token_alone_can_send_but_not_listen(self, empty_vault, monkeypatch):
        """The two halves are independent: posting a briefing needs no socket."""
        monkeypatch.setenv(BOT_TOKEN_ENV, FAKE_BOT_TOKEN)
        found = slack_credentials()
        assert found.can_send is True
        assert found.can_listen is False


class TestEverySurfaceAgrees:
    """The three judges of "is Slack configured?" on one box."""

    @staticmethod
    async def _daemon_started_slack() -> bool:
        from robothor.engine import daemon

        tasks: list[Any] = []
        bot = await daemon._start_channels(SimpleNamespace(), SimpleNamespace(), tasks)
        for task in tasks:
            task.cancel()
        return bot is not None and any(task.get_name() == "slack" for task in tasks)

    @staticmethod
    async def _doctor_says_configured() -> bool:
        from robothor.doctor.checks import channels as channel_checks
        from robothor.doctor.context import DoctorContext

        check = next(item for item in channel_checks.CHECKS if item.id == "slack.token")
        answer = await check.run(DoctorContext(timeout_s=2.0, offline=True))
        rows = answer if isinstance(answer, list) else [answer]
        return any(row.sub_id == "token" and row.status == "pass" for row in rows)

    @staticmethod
    async def _channel_says_configured() -> bool:
        from robothor.engine.channels.slack import SlackChannel

        return bool((await SlackChannel().health())["configured"])

    @pytest.mark.asyncio
    async def test_tokens_only_in_the_vault_configure_every_surface(self, vault_only):
        """The install `genus channel add slack` produces by default.

        Before this, the daemon gate read an empty environment and the inbound
        bot never started, while `verify` reported Socket Mode green.
        """
        assert await self._daemon_started_slack() is True, (
            "the daemon's Slack gate is blind to the vault, so the inbound bot "
            "never starts on the CLI's own default destination"
        )
        assert await self._doctor_says_configured() is True, (
            "genus doctor reports an instance unconfigured that genus channel "
            "list reports configured"
        )
        assert await self._channel_says_configured() is True

    @pytest.mark.asyncio
    async def test_tokens_nowhere_configure_nothing(self, empty_vault):
        assert await self._daemon_started_slack() is False
        assert await self._doctor_says_configured() is False
        assert await self._channel_says_configured() is False


class TestNoSurfaceReadsTheEnvironmentDirectly:
    """A second reader is how the four surfaces drifted apart in the first place."""

    def test_no_module_spells_a_slack_token_environment_name_itself(self):
        import ast
        import importlib
        from pathlib import Path

        owner = "robothor/engine/channels/slack_credentials.py"
        offenders: list[str] = []
        for dotted in (
            "robothor.engine.slack",
            "robothor.engine.daemon",
            "robothor.engine.channels.slack",
            "robothor.doctor.checks.channels",
            "robothor.cli.channel",
        ):
            module = importlib.import_module(dotted)
            assert module.__file__ is not None
            if module.__file__.endswith(owner):
                continue
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            offenders.extend(
                f"{dotted}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
                in {"get", "getenv", "setdefault", "pop"}
                and any(
                    isinstance(arg, ast.Constant) and arg.value in (BOT_TOKEN_ENV, APP_TOKEN_ENV)
                    for arg in node.args
                )
            )
        assert not offenders, (
            f"these read a Slack token name themselves instead of calling "
            f"slack_credentials(): {offenders}"
        )
