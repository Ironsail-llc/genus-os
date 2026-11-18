"""How the agent talks to the person in a conversation.

An owner's chat with the main agent went badly in ways no single tool caused:
three calls were refused by the per-minute limit and the agent told the owner
about "the rate_limit policy gate"; it asked the owner to paste lists and
send links it could have looked up; and it presented weeks-old records as
current. Rule 21 now keeps runtime internals out of what the person reads,
rule 22 holds the conduct of an interactive turn, and rule 13 is narrowed so
the two do not contradict: asking is for intent, not for facts.

Also here, because it is the same chat surface: the Telegram ``/status``
command merged the fleet's 24-hour figures from a key the analytics call
never returns, so every agent showed none.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.capabilities import Capability, Facts, render


@pytest.fixture(autouse=True)
def _no_verification(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_RUN_VERIFICATION_MODE", "off")


def _rule(n: int) -> str:
    from robothor.engine.prompts import behavioral_rules

    rules = behavioral_rules()
    start = rules.index(f"\n{n}. ")
    end = rules.find(f"\n{n + 1}. ", start + 1)
    return rules[start : end if end != -1 else None]


class TestRule21KeepsInternalsFromThePerson:
    def test_a_person_is_told_what_is_needed_and_who_can_grant_it(self) -> None:
        rule = _rule(21)
        assert "a permission, an approval" in rule
        assert "in plain words what is needed and who can grant it" in rule

    def test_it_never_mentions_throttles_gate_names_or_field_names(self) -> None:
        rule = _rule(21)
        assert "never mention runtime throttles, internal gate names or field names" in rule

    def test_it_no_longer_says_to_tell_the_person_the_gate(self) -> None:
        rule = _rule(21)
        assert "tell the person the gate" not in rule
        assert "(`gate_name`)" not in rule


class TestRule22ConductInAConversation:
    def test_lead_with_the_answer(self) -> None:
        assert "lead with the answer" in _rule(22).lower()

    def test_use_your_own_tools_before_asking(self) -> None:
        assert "use your own tools before asking" in _rule(22).lower()

    def test_never_ask_for_facts_the_platform_holds(self) -> None:
        rule = _rule(22)
        assert "never ask the person for facts the platform holds" in rule
        for kind in ("tenants", "companies", "agents", "links", "lists"):
            assert kind in rule

    def test_what_cannot_be_seen_is_said_once_then_what_can_be(self) -> None:
        rule = _rule(22)
        assert "say so once, in one sentence" in rule
        assert "what can be seen" in rule

    def test_old_records_are_dated_and_never_presented_as_current(self) -> None:
        rule = _rule(22)
        assert "how old" in rule
        assert "never present an old record as current" in rule

    def test_the_rule_is_short(self) -> None:
        """It is in every system prompt; a paragraph here costs every run."""
        assert len(_rule(22)) < 700


class TestRule13AsksAboutIntentOnly:
    def test_asking_is_for_intent(self) -> None:
        rule = _rule(13)
        assert "what the person intends" in rule

    def test_facts_are_looked_up_not_asked(self) -> None:
        rule = _rule(13)
        assert "look facts up yourself" in rule
        assert "rule 22" in rule


class TestNumbering:
    def test_the_honest_claims_rule_follows_the_new_rule(self, monkeypatch) -> None:
        monkeypatch.setenv("ROBOTHOR_RUN_VERIFICATION_MODE", "enforce")
        from robothor.engine.prompts import behavioral_rules

        text = behavioral_rules()
        numbers = [int(m) for m in re.findall(r"^(\d+)\. \*\*", text, re.MULTILINE)]
        assert numbers == list(range(1, 24))


class TestTheCapabilityBlockAgrees:
    def _block(self) -> str:
        gated = Capability(
            key="git_pr",
            label="open pull requests",
            tools=("create_pull_request",),
            available=False,
            gate="runtime",
            gate_name="GITHUB_TOKEN",
            unlock="set the token",
        )
        return render([gated], Facts(agent_id="example-agent", service_role="service"))

    def test_the_block_no_longer_says_to_tell_the_person_the_gate(self) -> None:
        assert "tell the person both" not in self._block()

    def test_the_block_says_plain_words_and_who_can_grant(self) -> None:
        block = self._block()
        assert "plain words" in block
        assert "who can grant it" in block


# ─── Telegram /status ────────────────────────────────────────────────


@pytest.fixture
def bot(engine_config):
    from robothor.engine.chat import _sessions
    from robothor.engine.telegram import TelegramBot

    _sessions.clear()
    with (
        patch("robothor.engine.telegram.Bot") as mock_bot_cls,
        patch("robothor.engine.telegram.Dispatcher"),
    ):
        mock_bot_cls.return_value = MagicMock(send_message=AsyncMock())
        yield TelegramBot(engine_config, MagicMock())
    _sessions.clear()


@pytest.mark.asyncio
async def test_status_shows_each_agents_24h_figures(bot) -> None:
    health = MagicMock()
    health.json.return_value = {"status": "ok", "agents": {}}
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=health)
    fleet = {
        "agents": [
            {"agent_id": "alpha", "total_runs": 4, "completed": 3, "avg_cost_usd": None},
        ],
        "fleet_totals": {},
    }
    message = MagicMock()
    message.chat.id = "12345"
    message.answer = AsyncMock()

    with (
        patch("robothor.engine.telegram_handlers.httpx.AsyncClient", return_value=client),
        patch("robothor.engine.analytics.get_fleet_health", return_value=fleet),
    ):
        await bot.cmd_status(message)

    text = message.answer.call_args.args[0]
    assert "alpha" in text
    assert "3/4" in text
