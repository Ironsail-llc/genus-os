"""The gate at its two production call sites, driven end to end.

Every other test of the access gate calls ``evaluate`` or ``pairing_reply``
directly, which proves the function and says nothing about the caller. This
repo's most frequent defect is exactly that gap — *correct function, inert
caller* — so these tests drive the real entry points instead:
``SlackBot._on_message`` with a spy runner, and
``TelegramBot._resolve_user`` / ``_handle_unregistered_sender`` with a fake
message.

**These are the mutation tests.** Replacing ``if not decision.allowed:`` in
``engine/slack.py`` with ``if False:`` must fail
``test_a_refused_slack_sender_never_reaches_the_runner``, and replacing
``if paired is not None:`` in ``engine/telegram.py`` with ``if False:`` must
fail ``test_an_unregistered_private_sender_is_answered_with_a_code``. Before
this file, both mutations left CI green.

The Slack mode table is here rather than in ``test_channel_access_policy.py``
because the question it answers is not "what does the gate decide" but "which
mode does the bot ask it about" — and the first cut of ``_access_mode`` got that
wrong in the one direction that opens a surface: it inspected the *resolved*
mode, so an operator who explicitly set ``ROBOTHOR_SLACK_ACCESS=pairing`` while
a stale ``ROBOTHOR_SLACK_ALLOWED_CHANNELS`` was still exported was silently
downgraded to ``allowlist``, and ``_authorized`` is user-OR-**channel**.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.channels import access
from robothor.engine.slack import SlackBot
from robothor.identity import IdentityContext

SLACK_USER = "U0PLACEHOLDER"
SLACK_DM = "D0PLACEHOLDER"
SLACK_ROOM = "C0PLACEHOLDER"
TELEGRAM_USER = "100000001"
DEFAULT_CHAT = "12345"
CODE = "ABC234"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """None of the box's own channel configuration, and no live identities."""
    from robothor.settings import reset_settings

    for name in list(os.environ):
        if name.startswith(("ROBOTHOR_", "GENUS_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", "default")
    reset_settings()
    access.reset_reply_budget()
    monkeypatch.setattr(access, "_resolve_known", lambda *a, **kw: None)
    monkeypatch.setattr(access.identities, "mint_code", lambda **kw: CODE)
    yield
    reset_settings()
    access.reset_reply_budget()


# ── Slack: the call site ─────────────────────────────────────────────────────


class _Runner:
    def __init__(self) -> None:
        self.execute = AsyncMock(return_value=MagicMock(output_text="done"))


def _slack_bot(**config: Any) -> SlackBot:
    cfg = MagicMock()
    cfg.tenant_id = config.get("tenant_id", "default")
    return SlackBot(_Runner(), cfg)


def _event(channel: str = SLACK_DM, kind: str = "im") -> dict[str, Any]:
    return {"text": "hello", "channel": channel, "user": SLACK_USER, "channel_type": kind}


async def _drive(bot: SlackBot, event: dict[str, Any]) -> AsyncMock:
    say = AsyncMock()
    await bot._on_message(event, say)
    return say


@pytest.mark.asyncio
async def test_a_refused_slack_sender_never_reaches_the_runner(monkeypatch):
    """The mutation test for ``slack.py``'s ``if not decision.allowed:``."""
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")
    bot = _slack_bot()

    say = await _drive(bot, _event())

    assert bot.runner.execute.call_count == 0
    assert say.await_count == 1
    assert CODE in say.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_a_refused_slack_sender_in_a_room_is_answered_with_nothing(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")
    bot = _slack_bot()

    say = await _drive(bot, _event(channel=SLACK_ROOM, kind="channel"))

    assert bot.runner.execute.call_count == 0
    assert say.await_count == 0


@pytest.mark.asyncio
async def test_a_paired_slack_sender_runs_as_the_bound_user(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")
    bound = IdentityContext(
        tenant_id="tenant-a",
        channel="slack",
        identifier=SLACK_USER,
        verified=True,
        role="viewer",
        tenant_user_id="u-alice",
    )
    monkeypatch.setattr(access, "_resolve_known", lambda *a, **kw: bound)
    bot = _slack_bot()

    await _drive(bot, _event())

    assert bot.runner.execute.call_count == 1
    kwargs = bot.runner.execute.await_args.kwargs
    assert kwargs["user_id"] == "u-alice"
    assert kwargs["user_role"] == "viewer"
    assert kwargs["tenant_id"] == "tenant-a"
    assert kwargs["identity"] is bound


@pytest.mark.asyncio
async def test_an_open_slack_channel_still_runs_an_unknown_sender(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "open")
    bot = _slack_bot()

    await _drive(bot, _event())

    assert bot.runner.execute.call_count == 1
    assert bot.runner.execute.await_args.kwargs["user_id"] == f"slack:{SLACK_USER}"


# ── Slack: which mode the bot asks about ─────────────────────────────────────


@pytest.mark.asyncio
async def test_an_explicit_pairing_mode_beats_a_legacy_allowlist(monkeypatch):
    """The C1 regression.

    The operator named ``pairing``. A stale ``ROBOTHOR_SLACK_ALLOWED_CHANNELS``
    from before modes existed must not downgrade that to ``allowlist`` — which
    it did, and because ``_authorized`` is user-OR-channel, every unknown member
    of the workspace posting in that leftover channel drove the main agent with
    ``user_role="user"``.
    """
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")
    monkeypatch.setenv("ROBOTHOR_SLACK_ALLOWED_CHANNELS", SLACK_ROOM)
    bot = _slack_bot()

    assert bot._access_mode() == "pairing"

    say = await _drive(bot, _event(channel=SLACK_ROOM, kind="channel"))

    assert bot.runner.execute.call_count == 0
    assert say.await_count == 0


@pytest.mark.asyncio
async def test_an_explicit_pairing_mode_beats_a_legacy_user_allowlist(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")
    monkeypatch.setenv("ROBOTHOR_SLACK_ALLOWED_USERS", SLACK_USER)
    bot = _slack_bot()

    assert bot._access_mode() == "pairing"

    say = await _drive(bot, _event())

    assert bot.runner.execute.call_count == 0
    assert CODE in say.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_an_unset_mode_with_a_legacy_allowlist_stays_on_allowlist(monkeypatch):
    """The compatibility clause, in the one case it is for.

    An instance that configured an allowlist before modes existed and has named
    no mode keeps being governed by that allowlist. Flipping it to ``pairing``
    on upgrade would lock out everyone on the list.
    """
    monkeypatch.setenv("ROBOTHOR_SLACK_ALLOWED_USERS", SLACK_USER)
    bot = _slack_bot()

    assert bot._access_mode() == "allowlist"

    await _drive(bot, _event())

    assert bot.runner.execute.call_count == 1


@pytest.mark.asyncio
async def test_an_unset_mode_with_a_legacy_allowlist_still_refuses_an_unlisted_sender(
    monkeypatch,
):
    monkeypatch.setenv("ROBOTHOR_SLACK_ALLOWED_USERS", "U0SOMEONEELSE")
    bot = _slack_bot()

    say = await _drive(bot, _event())

    assert bot.runner.execute.call_count == 0
    assert say.await_count == 0


@pytest.mark.asyncio
async def test_an_unset_mode_with_no_allowlist_is_pairing(monkeypatch):
    bot = _slack_bot()

    assert bot._access_mode() == "pairing"

    say = await _drive(bot, _event())

    assert bot.runner.execute.call_count == 0
    assert CODE in say.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_an_explicit_allowlist_mode_with_no_allowlist_configured(monkeypatch):
    """``allowlist`` with neither list set is ``_authorized``'s allow case, and
    the operator asked for it by name. That is different from it being the
    DEFAULT posture of every Slack install, which is what it used to be."""
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "allowlist")
    bot = _slack_bot()

    assert bot._access_mode() == "allowlist"

    await _drive(bot, _event())

    assert bot.runner.execute.call_count == 1


# ── Telegram: the call site ──────────────────────────────────────────────────


@pytest.fixture
def telegram_bot(engine_config):
    from robothor.engine.telegram import TelegramBot

    with patch("robothor.engine.telegram.Bot"), patch("robothor.engine.telegram.Dispatcher"):
        bot = TelegramBot(engine_config, MagicMock())
        bot.send_message = AsyncMock(return_value=[MagicMock()])
        yield bot


def _message(chat_type: str = "private", chat_id: str = "999") -> MagicMock:
    message = MagicMock()
    message.from_user.id = int(TELEGRAM_USER)
    message.from_user.first_name = "Alice"
    message.from_user.username = "alice"
    message.chat.type = chat_type
    message.chat.id = int(chat_id)
    message.text = "hello"
    message.caption = None
    return message


@pytest.mark.asyncio
async def test_an_unregistered_private_sender_is_answered_with_a_code(telegram_bot, monkeypatch):
    """The mutation test for ``telegram.py``'s ``if paired is not None:``."""
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_ACCESS", "pairing")

    reply = await telegram_bot._handle_unregistered_sender(_message(), TELEGRAM_USER)

    assert CODE in reply


@pytest.mark.asyncio
async def test_the_closed_onboarding_refusal_is_byte_for_byte_unchanged_when_not_pairing(
    telegram_bot,
):
    """No setting: the sentence that shipped before this gate existed, exactly."""
    reply = await telegram_bot._handle_unregistered_sender(_message(), TELEGRAM_USER)

    assert reply == (
        "This bot is not open for self-registration. "
        "If you believe you should have access, please contact the workspace operator directly."
    )


@pytest.mark.asyncio
async def test_a_group_sender_under_pairing_gets_the_refusal_and_never_a_code(
    telegram_bot, monkeypatch
):
    """A code posted in a group is a code anyone in the group can carry to the
    operator, so a group surface is refused with the ordinary sentence."""
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_ACCESS", "pairing")

    reply = await telegram_bot._handle_unregistered_sender(
        _message(chat_type="group"), TELEGRAM_USER
    )

    assert CODE not in reply
    assert "self-registration" in reply


# ── Telegram: `pairing` closes every surface, not just private chats ─────────


@pytest.fixture
def _unregistered(monkeypatch):
    """``lookup_user`` finds nobody, so every surface takes a fallback branch."""
    monkeypatch.setattr("robothor.engine.users.lookup_user", lambda *a, **kw: None)


def test_open_mode_still_fabricates_a_group_identity(telegram_bot, _unregistered):
    """Today's behaviour, which the `open` default must not change."""
    resolved = telegram_bot._resolve_user("777", _message(chat_type="group", chat_id="777"))

    assert resolved is not None
    assert resolved["role"] == "user"


def test_open_mode_still_fabricates_the_default_chat_owner(telegram_bot, _unregistered):
    resolved = telegram_bot._resolve_user(
        DEFAULT_CHAT, _message(chat_type="private", chat_id=DEFAULT_CHAT)
    )

    assert resolved is not None
    assert resolved["role"] == "owner"


def test_pairing_mode_refuses_an_unregistered_group_sender(
    telegram_bot, _unregistered, monkeypatch
):
    """`pairing` means only known identities run. A fabricated ``user`` in a
    group chat maps to ``("user", "*", "allow")`` — every tool — so leaving that
    branch live would make `pairing` a setting that closes one surface of three
    while the docs said it closed the channel."""
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_ACCESS", "pairing")

    resolved = telegram_bot._resolve_user("777", _message(chat_type="group", chat_id="777"))

    assert resolved is None


def test_pairing_mode_refuses_an_unregistered_default_chat_sender(
    telegram_bot, _unregistered, monkeypatch
):
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_ACCESS", "pairing")

    resolved = telegram_bot._resolve_user(
        DEFAULT_CHAT, _message(chat_type="private", chat_id=DEFAULT_CHAT)
    )

    assert resolved is None


def test_pairing_mode_still_honours_the_owner_escape_hatch(
    telegram_bot, _unregistered, monkeypatch
):
    """A fresh install has no owner row yet, and the operator must not be able
    to lock themselves out of their own bot by closing it."""
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_ACCESS", "pairing")
    monkeypatch.setenv("ROBOTHOR_ALLOW_UNREGISTERED_OWNER_FALLBACK", "1")

    resolved = telegram_bot._resolve_user(
        DEFAULT_CHAT, _message(chat_type="private", chat_id=DEFAULT_CHAT)
    )

    assert resolved is not None
    assert resolved["role"] == "owner"


def test_a_registered_sender_is_untouched_by_pairing_mode(telegram_bot, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_ACCESS", "pairing")
    monkeypatch.setattr(
        "robothor.engine.users.lookup_user",
        lambda *a, **kw: {
            "tenant_id": "tenant-a",
            "display_name": "Alice",
            "role": "member",
            "user_id": "u-alice",
            "person_id": None,
        },
    )

    resolved = telegram_bot._resolve_user("999", _message())

    assert resolved is not None
    assert resolved["role"] == "member"
    assert resolved["user_id"] == "u-alice"
