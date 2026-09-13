"""Slack channel activation (Wave-1 hardening, PR-16).

slack.py:SlackBot was fully implemented but never instantiated or started, and
had no tests. The daemon now starts it (env-gated on the Slack tokens). These
tests cover construction + the no-token / no-SDK no-op paths and the daemon wiring.
"""

from __future__ import annotations

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


def test_daemon_wires_slack_through_the_one_credential_reader():
    """AST, not a substring. The old form searched the WHOLE daemon module for
    three strings, so a comment naming the env var, an unrelated ``SlackBot(...)``
    and a task called ``slack`` anywhere in 1,300 lines satisfied it.

    The gate used to be two ``os.environ`` reads, and that was the defect: the
    tokens ``genus channel add slack`` writes to the vault by default never
    reach the process environment, so the inbound bot silently never started
    while the outbound channel — which did use the accessor — worked fine.
    ``slack_credentials`` is the one reader every surface now asks.
    """
    from robothor.engine.tests.astcheck import called_names, function_def, string_constants

    branch = function_def("robothor.engine.daemon", "_start_channels")
    assert "SlackBot" in called_names(branch), "the daemon no longer constructs the Slack bot"
    assert "create_task" in called_names(branch), "the bot is no longer started as a task"
    assert "slack_credentials" in called_names(branch), (
        "the daemon's Slack gate no longer goes through the one credential reader, "
        "so it can disagree with the channel and the doctor about the same box"
    )
    assert "slack" in string_constants(branch), (
        "the Slack task lost the name the supervisor reports it by"
    )


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
