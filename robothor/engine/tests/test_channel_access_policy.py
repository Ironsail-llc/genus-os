"""Who may drive this instance from a channel, and what an unknown sender gets.

Before this gate there were two answers and neither was a policy. Telegram's
was a ladder of fabrications inside ``_resolve_user`` ending in a refusal
string; Slack's was ``_authorized``, which returned **True when no allowlist
was configured at all** — any member of a joined workspace could drive the main
agent, with a startup warning as the only compensating control.

``access.evaluate`` is the one entry point both now go through, and the tests
here pin the three things that must stay true of it:

* **A known identity short-circuits every mode.** An operator who flips a
  channel to ``pairing`` must not lock out the people already bound to it.
* **``open`` and ``allowlist`` reproduce today's behaviour exactly**, so the
  gate landing is not itself a behaviour change. Telegram defaults to ``open``
  for that reason and no other.
* **An unknown sender on a ``pairing`` channel never reaches the runner.** It
  gets a code and nothing else — no operator name, no instance name, no hint
  about who can approve it.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from robothor.engine.channels import access
from robothor.identity import IdentityContext

#: Placeholder native ids. Never a real workspace's.
SLACK_USER = "U0PLACEHOLDER"
TELEGRAM_USER = "100000001"
TENANT = "tenant-a"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """None of the box's own channel configuration, and a clean reply budget."""
    from robothor.settings import reset_settings

    for name in list(os.environ):
        if name.startswith(("ROBOTHOR_", "GENUS_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", "default")
    reset_settings()
    access.reset_reply_budget()
    yield
    reset_settings()
    access.reset_reply_budget()


@pytest.fixture
def unknown(monkeypatch):
    """Nobody is bound to any channel."""
    monkeypatch.setattr(access, "_resolve_known", lambda *a, **kw: None)


@pytest.fixture
def minted(monkeypatch):
    """Record every code the gate minted, without a database."""
    codes: list[dict[str, Any]] = []

    def _mint(**kw: Any) -> str:
        codes.append(kw)
        return "ABC234"

    monkeypatch.setattr(access.identities, "mint_code", _mint)
    return codes


def _evaluate(**kw: Any) -> access.AccessDecision:
    return asyncio.run(access.evaluate(**kw))


# ── pairing ──────────────────────────────────────────────────────────────────


def test_unknown_sender_on_pairing_channel_gets_a_code_and_no_run(unknown, minted, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")

    class _Runner:
        call_count = 0

        async def execute(self, **kw: Any) -> None:  # pragma: no cover - must not run
            _Runner.call_count += 1

    decision = _evaluate(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)

    assert decision.allowed is False
    assert decision.identity is None
    assert decision.pairing_code is not None
    assert len(decision.pairing_code) == access.PAIRING_CODE_LENGTH
    assert set(decision.pairing_code) <= set(access.PAIRING_CODE_ALPHABET)
    assert decision.pairing_code in decision.refusal
    assert _Runner.call_count == 0


def test_the_pairing_reply_names_nobody_and_nothing(unknown, minted, monkeypatch):
    """One neutral sentence and the code.

    A bare six-character code told a stranger nothing about what to do with it,
    so the reply carries a sentence. What it must NOT carry is the point: the
    operator's name, the instance's or the product's name, an address, or any
    "ask <someone>" that hands a stranger a human to social-engineer. A role
    ("the operator of this assistant") is not an identity.
    """
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")
    monkeypatch.setenv("ROBOTHOR_AI_NAME", "Distinctive-Assistant-Name")
    monkeypatch.setenv("ROBOTHOR_BRAND_NAME", "Distinctive-Brand-Name")
    monkeypatch.setenv("ROBOTHOR_DOMAIN", "distinctive.example.com")

    decision = _evaluate(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)

    assert decision.refusal == (
        f"Share this code with the operator of this assistant to be paired: {decision.pairing_code}"
    )
    for leak in ("Distinctive-Assistant-Name", "Distinctive-Brand-Name", "distinctive.example.com"):
        assert leak not in decision.refusal
    assert TENANT not in decision.refusal
    assert SLACK_USER not in decision.refusal


def test_a_retry_asks_the_dal_for_the_same_row_rather_than_minting_a_second(
    unknown, minted, monkeypatch
):
    """The gate holds no per-sender code of its own.

    Idempotence is the partial unique index on ``(tenant_id, channel,
    native_id) WHERE used_at IS NULL AND denied_at IS NULL`` plus the DAL's
    in-process memo (see ``test_channel_pairing.py``); all the gate has to do
    is ask for the same key every time. A second source of truth here is how
    two live codes for one sender would come to exist.
    """
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")

    first = _evaluate(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)
    second = _evaluate(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)

    assert first.pairing_code == second.pairing_code
    assert len(minted) == 2
    assert minted[0] == minted[1]
    assert minted[0]["channel"] == "slack"
    assert minted[0]["native_id"] == SLACK_USER
    assert minted[0]["tenant_id"] == TENANT


def test_a_fourth_reply_within_the_hour_is_suppressed(unknown, minted, monkeypatch):
    """A sender retrying must not be able to make the bot answer forever."""
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")

    replies = [_evaluate(channel="slack", native_id=SLACK_USER, tenant_id=TENANT) for _ in range(4)]

    assert [bool(d.pairing_code) for d in replies] == [True, True, True, False]
    assert replies[3].allowed is False
    assert replies[3].refusal == ""


def test_a_group_surface_never_gets_a_code(unknown, minted, monkeypatch):
    """A code posted in a shared channel is a code anyone can screenshot."""
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")

    decision = _evaluate(
        channel="slack", native_id=SLACK_USER, tenant_id=TENANT, surface=access.GROUP_SURFACE
    )

    assert decision.allowed is False
    assert decision.pairing_code is None
    assert decision.refusal == ""
    assert minted == []


def test_a_known_identity_short_circuits_pairing(monkeypatch, minted):
    """Flipping a channel to pairing must not lock out who is already bound."""
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")
    bound = IdentityContext(
        tenant_id=TENANT, channel="slack", identifier=SLACK_USER, verified=True, role="member"
    )
    monkeypatch.setattr(access, "_resolve_known", lambda *a, **kw: bound)

    decision = _evaluate(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)

    assert decision.allowed is True
    assert decision.identity is bound
    assert decision.pairing_code is None
    assert minted == []


# ── allowlist ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("users", "channels", "expected"),
    [
        # The truth table of engine/slack.py:_authorized, unchanged.
        (set(), set(), True),  # neither list set -> allow
        ({SLACK_USER}, set(), True),
        ({"U0OTHER"}, set(), False),
        (set(), {"C0PLACEHOLDER"}, True),
        (set(), {"C0OTHER"}, False),
        ({"U0OTHER"}, {"C0PLACEHOLDER"}, True),  # user OR channel
        ({SLACK_USER}, {"C0OTHER"}, True),
    ],
)
def test_allowlist_mode_is_unchanged(unknown, monkeypatch, users, channels, expected):
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "allowlist")

    def _listed() -> bool:
        if not users and not channels:
            return True
        return SLACK_USER in users or "C0PLACEHOLDER" in channels

    decision = _evaluate(channel="slack", native_id=SLACK_USER, tenant_id=TENANT, allowlist=_listed)

    assert decision.allowed is expected
    assert decision.pairing_code is None


def test_allowlist_mode_with_no_callback_refuses(unknown, monkeypatch):
    """Fail closed: a channel that offers no membership test is not an open one."""
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "allowlist")

    decision = _evaluate(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)

    assert decision.allowed is False
    assert decision.refusal == ""


# ── open ─────────────────────────────────────────────────────────────────────


def test_open_mode_still_resolves_identity(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_ACCESS", "open")
    bound = IdentityContext(
        tenant_id=TENANT, channel="telegram", identifier=TELEGRAM_USER, verified=True, role="owner"
    )
    monkeypatch.setattr(access, "_resolve_known", lambda *a, **kw: bound)

    decision = _evaluate(channel="telegram", native_id=TELEGRAM_USER, tenant_id=TENANT)

    assert decision.allowed is True
    assert decision.identity is bound


def test_open_mode_allows_an_unknown_sender_with_no_identity(unknown, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_ACCESS", "open")

    decision = _evaluate(channel="telegram", native_id=TELEGRAM_USER, tenant_id=TENANT)

    assert decision.allowed is True
    assert decision.identity is None
    assert decision.pairing_code is None


# ── modes ────────────────────────────────────────────────────────────────────


def test_telegram_defaults_to_open_and_slack_to_pairing():
    """Telegram's default is compatibility, not a recommendation."""
    assert access.access_mode("telegram") == "open"
    assert access.access_mode("slack") == "pairing"


def test_a_channel_with_no_dedicated_setting_uses_the_default(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_CHANNEL_ACCESS_DEFAULT", "allowlist")

    assert access.access_mode("some_plugin_channel") == "allowlist"


def test_a_caller_that_resolved_its_own_mode_is_not_second_guessed(unknown, minted):
    """The mode is decided ONCE, by whoever passes it.

    Slack resolves its own, because it carries a compatibility clause for
    instances that configured an allowlist before modes existed. With the mode
    read a second time here, the bot's ``allowlist`` and this gate's ``pairing``
    would both be "correct" and every allowlisted sender would be handed a
    pairing code. That is the defect this pins, and it shipped once.
    """
    decision = _evaluate(
        channel="slack",
        native_id=SLACK_USER,
        tenant_id=TENANT,
        allowlist=lambda: True,
        mode="allowlist",
    )

    assert decision.allowed is True
    assert minted == []


def test_a_nonsense_mode_override_falls_back_to_the_setting(unknown, minted, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "open")

    decision = _evaluate(channel="slack", native_id=SLACK_USER, tenant_id=TENANT, mode="whatever")

    assert decision.allowed is True
    assert minted == []


def test_a_blank_mode_means_unset_and_takes_the_fields_own_default(monkeypatch):
    """``ROBOTHOR_TELEGRAM_ACCESS=`` is not a request for ``pairing``.

    Blanking a variable asks for whatever the platform ships, and what Telegram
    ships is ``open``. Falling through to ``channel_access_default`` instead
    made an empty string mean the opposite of the field's own declared default,
    and nothing said so.
    """
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_ACCESS", "")
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "   ")

    assert access.access_mode("telegram") == "open"
    assert access.access_mode("slack") == "pairing"
    assert access.mode_was_configured("telegram") is False


def test_a_plugin_channel_with_no_field_uses_the_shared_default(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_CHANNEL_ACCESS_DEFAULT", "open")

    assert access.access_mode("some_plugin_channel") == "open"
    assert access.mode_was_configured("some_plugin_channel") is True


def test_a_channel_name_cannot_reach_a_neighbouring_setting(monkeypatch):
    """``_mode_field`` matches against declared field names rather than doing a
    bare getattr, so a channel called ``verify_target`` resolves to the shared
    default instead of reading ``slack_verify_target``."""
    monkeypatch.setenv("ROBOTHOR_SLACK_VERIFY_TARGET", "C0PLACEHOLDER")

    assert access.access_mode("slack_verify") == "pairing"


def test_an_unset_mode_is_not_reported_as_configured():
    assert access.mode_was_configured("slack") is False
    assert access.mode_was_configured("telegram") is False


def test_an_explicitly_set_mode_is_reported_as_configured(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")

    assert access.mode_was_configured("slack") is True
    assert access.access_mode("slack") == "pairing"


def test_the_configured_check_is_answered_once_per_settings_generation(monkeypatch):
    """``mode_was_configured`` walks the settings registry and re-reads
    config.yaml, and Slack asks it on every inbound message while its mode is
    unset. Memoised against the settings object itself, so ``reset_settings()``
    -- the one thing that changes the answer -- invalidates it for free and
    nothing else has to remember to.
    """
    from robothor.settings import get_settings, provenance, reset_settings

    calls: list[str] = []
    real = provenance.is_configured

    def _counted(field_path: str) -> bool:
        calls.append(field_path)
        return real(field_path)

    monkeypatch.setattr(provenance, "is_configured", _counted)

    get_settings()
    for _ in range(5):
        access.mode_was_configured("slack")
    assert calls == ["channels.slack_access"]

    # A different field is its own question.
    access.mode_was_configured("telegram")
    assert calls == ["channels.slack_access", "channels.telegram_access"]

    # New settings, new answer -- an operator who exported the variable and
    # reloaded must not be told the old thing.
    reset_settings()
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "open")
    assert access.mode_was_configured("slack") is True
    assert calls.count("channels.slack_access") == 2


def test_an_unrecognised_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "everyone")

    assert access.access_mode("slack") == "pairing"


def test_telegram_default_mode_reproduces_closed_onboarding(unknown, minted):
    """No setting, no pairing: the Telegram helper declines to answer at all,
    so ``_handle_unregistered_sender`` falls through to the refusal it already
    sends and the closed-onboarding path is byte-for-byte unchanged."""
    reply = asyncio.run(
        access.pairing_reply("telegram", TELEGRAM_USER, tenant_id=TENANT, display_name="Alice")
    )

    assert reply is None
    assert minted == []


def test_the_telegram_helper_answers_with_a_code_once_pairing_is_on(unknown, minted, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_ACCESS", "pairing")

    reply = asyncio.run(
        access.pairing_reply("telegram", TELEGRAM_USER, tenant_id=TENANT, display_name="Alice")
    )

    assert reply is not None
    assert reply.endswith("ABC234")


def test_a_suppressed_telegram_reply_falls_back_instead_of_sending_nothing(
    unknown, minted, monkeypatch
):
    """``message.answer("")`` is a Telegram API error, so the helper returns
    None and the caller's existing refusal is sent instead."""
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_ACCESS", "pairing")

    replies = [
        asyncio.run(access.pairing_reply("telegram", TELEGRAM_USER, tenant_id=TENANT))
        for _ in range(4)
    ]

    assert all(reply is not None and reply.endswith("ABC234") for reply in replies[:3])
    assert replies[3] is None


def test_a_mint_failure_does_not_take_the_message_down(unknown, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")

    def _boom(**kw: Any) -> str:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(access.identities, "mint_code", _boom)

    decision = _evaluate(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)

    assert decision.allowed is False
    assert decision.pairing_code is None
