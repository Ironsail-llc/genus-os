"""Slack channel activation (Wave-1 hardening, PR-16).

slack.py:SlackBot was fully implemented but never instantiated or started, and
had no tests. The daemon now starts it (env-gated on the Slack tokens). These
tests cover construction + the no-token / no-SDK no-op paths and the daemon wiring.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from robothor.engine.slack import SlackBot


def _bot():
    return SlackBot(SimpleNamespace(), SimpleNamespace(tenant_id="default"))


def test_constructs():
    bot = _bot()
    assert bot._started is False
    assert bot._app is None


async def test_start_noops_without_tokens(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ROBOTHOR_SLACK_APP_TOKEN", raising=False)
    bot = _bot()
    await bot.start()  # must not raise
    assert bot._started is False


def test_daemon_wires_slack_env_gated():
    from robothor.engine import daemon

    src = inspect.getsource(daemon)
    assert "ROBOTHOR_SLACK_BOT_TOKEN" in src
    assert "SlackBot(runner, config)" in src
    assert 'name="slack"' in src


class TestSlackAuthorization:
    def test_open_when_no_allowlist(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_SLACK_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("ROBOTHOR_SLACK_ALLOWED_CHANNELS", raising=False)
        # Unconfigured allowlist allows (deliberate activation; warned at start).
        assert _bot()._authorized("U1", "C1") is True

    def test_user_allowlist_enforced(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_SLACK_ALLOWED_USERS", "U_OK, U_ALSO")
        monkeypatch.delenv("ROBOTHOR_SLACK_ALLOWED_CHANNELS", raising=False)
        bot = _bot()
        assert bot._authorized("U_OK", "any") is True
        assert bot._authorized("U_NOPE", "any") is False

    def test_channel_allowlist_enforced(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_SLACK_ALLOWED_USERS", raising=False)
        monkeypatch.setenv("ROBOTHOR_SLACK_ALLOWED_CHANNELS", "C_OPS")
        bot = _bot()
        assert bot._authorized("anyone", "C_OPS") is True
        assert bot._authorized("anyone", "C_RANDOM") is False

    def test_either_list_matches(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_SLACK_ALLOWED_USERS", "U_OK")
        monkeypatch.setenv("ROBOTHOR_SLACK_ALLOWED_CHANNELS", "C_OPS")
        bot = _bot()
        assert bot._authorized("U_OK", "C_RANDOM") is True  # user match
        assert bot._authorized("U_NOPE", "C_OPS") is True  # channel match
        assert bot._authorized("U_NOPE", "C_RANDOM") is False
