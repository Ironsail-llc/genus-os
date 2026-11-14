"""A page the primary channel cannot deliver goes to the configured fallback.

The primary pager is Telegram. On an instance where it is not set up, a
critical alert used to end at "Telegram sender not initialized, can't deliver
alert" and a notification row, and the operator was never interrupted. The
fallback is configuration: ``ROBOTHOR_ALERT_FALLBACK_CHANNELS`` is an ordered,
comma-separated list of ``<channel>:<target>`` entries, each one a channel the
registry already knows (email, Slack, a plugin channel). They are tried in
order after the primary, and the first that proves delivery ends the attempt.

The fakes below stand in for the registry's channels with the real contract:
``health()`` reports whether the channel could send at all, and ``send()``
returns a :class:`SendReceipt` whose ``complete`` decides delivery.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

import robothor.crm.dal as dal
from robothor.engine import alerts
from robothor.engine.channels.base import SendReceipt
from robothor.engine.delivery import set_telegram_sender

FALLBACK_TARGET = "agent@example.com"


class FakeChannel:
    """A registry channel with a switch for "can it send" and "does it land"."""

    inbound_router: Any | None = None

    def __init__(self, name: str, *, can_send: bool = True, lands: bool = True) -> None:
        self.name = name
        self.can_send = can_send
        self.lands = lands
        self.sends: list[tuple[str, str, dict[str, Any]]] = []

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def health(self) -> dict[str, Any]:
        report: dict[str, Any] = {"channel": self.name, "ok": self.can_send}
        if not self.can_send:
            report["error"] = "no send credentials configured"
        return report

    async def send(self, target: str, text: str, **kw: Any) -> SendReceipt:
        self.sends.append((target, text, kw))
        return SendReceipt(acknowledged=1 if self.lands else 0, expected=1, target=target)

    async def send_attachment(self, *a: Any, **kw: Any) -> Any:
        raise NotImplementedError

    async def ask(self, *a: Any, **kw: Any) -> Any:
        raise NotImplementedError

    async def resolve_identity(self, native_id: str) -> Any:
        return None


@pytest.fixture(autouse=True)
def notification_rows(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(dal, "send_notification", lambda **kw: rows.append(kw) or "notif-1")
    return rows


@pytest.fixture
def channels(monkeypatch):
    """Route registry lookups to fakes, keyed by channel name."""
    from robothor.engine.channels import registry

    table: dict[str, FakeChannel] = {}
    monkeypatch.setattr(registry, "get_channel", lambda name: table.get((name or "").strip()))
    return table


@pytest.fixture
def configure(monkeypatch):
    """Set alert configuration and re-resolve settings."""
    from robothor.settings import reset_settings

    def _set(*, fallback: str = "", chat_id: str = "") -> None:
        if fallback:
            monkeypatch.setenv("ROBOTHOR_ALERT_FALLBACK_CHANNELS", fallback)
        else:
            monkeypatch.delenv("ROBOTHOR_ALERT_FALLBACK_CHANNELS", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        if chat_id:
            monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", chat_id)
        else:
            monkeypatch.delenv("ROBOTHOR_TELEGRAM_CHAT_ID", raising=False)
        reset_settings()

    yield _set
    reset_settings()


@pytest.fixture
def telegram():
    """A primary that works; flip ``state['lands']`` to make it fail."""
    state: dict[str, Any] = {"lands": True, "sends": []}

    async def sender(chat_id, text):  # the production arity
        state["sends"].append((chat_id, text))
        return ["message"] if state["lands"] else []

    set_telegram_sender(sender)
    yield state
    set_telegram_sender(None)  # type: ignore[arg-type]


@pytest.fixture
def no_telegram():
    set_telegram_sender(None)  # type: ignore[arg-type]
    yield
    set_telegram_sender(None)  # type: ignore[arg-type]


# ── primary unset, fallback email ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unset_primary_delivers_once_through_the_fallback(
    configure, channels, no_telegram, notification_rows, caplog
):
    configure(fallback=f"email:{FALLBACK_TARGET}")
    email = channels["email"] = FakeChannel("email")

    with caplog.at_level(logging.INFO, logger="robothor.engine.alerts"):
        delivered = await alerts.alert(
            "critical", "Database down", "3 failed pings", alert_id="db-down-1"
        )

    assert delivered is True
    assert len(email.sends) == 1, email.sends
    target, text, _kw = email.sends[0]
    assert target == FALLBACK_TARGET
    assert "Database down" in text
    assert "3 failed pings" in text
    assert notification_rows == [], "a delivered fallback must not also leave an undelivered row"
    lines = [rec.getMessage() for rec in caplog.records if "db-down-1" in rec.getMessage()]
    assert any("email" in line and "not configured" in line for line in lines), lines


# ── primary set and working ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_working_primary_is_the_only_channel_used(
    configure, channels, telegram, notification_rows, caplog
):
    configure(fallback=f"email:{FALLBACK_TARGET}", chat_id="12345")
    email = channels["email"] = FakeChannel("email")

    with caplog.at_level(logging.INFO, logger="robothor.engine.alerts"):
        delivered = await alerts.alert("critical", "Disk full", "98%", alert_id="disk-full-1")

    assert delivered is True
    assert len(telegram["sends"]) == 1
    assert email.sends == [], "the fallback must not be used when the primary delivered"
    assert notification_rows == []
    assert any(
        "disk-full-1" in rec.getMessage() and "telegram" in rec.getMessage()
        for rec in caplog.records
    ), "one log line must say which channel delivered"


# ── primary set and failing ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failing_primary_falls_back_and_the_failure_is_logged(
    configure, channels, telegram, notification_rows, caplog
):
    configure(fallback=f"email:{FALLBACK_TARGET}", chat_id="12345")
    telegram["lands"] = False
    email = channels["email"] = FakeChannel("email")

    with caplog.at_level(logging.INFO, logger="robothor.engine.alerts"):
        delivered = await alerts.alert("critical", "Queue stuck", "b", alert_id="queue-1")

    assert delivered is True
    assert len(telegram["sends"]) == 1, "the primary is tried first"
    assert len(email.sends) == 1
    assert notification_rows == []
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("queue-1" in m and "email" in m and "failed" in m for m in messages), messages


@pytest.mark.asyncio
async def test_fallbacks_are_tried_in_the_configured_order(
    configure, channels, no_telegram, notification_rows
):
    configure(fallback=f"slack:C0123456,email:{FALLBACK_TARGET}")
    slack = channels["slack"] = FakeChannel("slack", lands=False)
    email = channels["email"] = FakeChannel("email")

    delivered = await alerts.alert("critical", "t", "b", alert_id="order-1")

    assert delivered is True
    assert [s[0] for s in slack.sends] == ["C0123456"]
    assert [s[0] for s in email.sends] == [FALLBACK_TARGET]


@pytest.mark.asyncio
async def test_the_first_fallback_that_delivers_ends_the_attempt(
    configure, channels, no_telegram, notification_rows
):
    configure(fallback=f"email:{FALLBACK_TARGET},slack:C0123456")
    email = channels["email"] = FakeChannel("email")
    slack = channels["slack"] = FakeChannel("slack")

    assert await alerts.alert("critical", "t", "b", alert_id="first-1") is True
    assert len(email.sends) == 1
    assert slack.sends == []


# ── a channel with no send credentials ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_channel_that_cannot_send_is_not_called_and_the_alert_is_recorded(
    configure, channels, no_telegram, notification_rows, caplog
):
    configure(fallback=f"email:{FALLBACK_TARGET}")
    email = channels["email"] = FakeChannel("email", can_send=False)

    with caplog.at_level(logging.INFO, logger="robothor.engine.alerts"):
        delivered = await alerts.alert("critical", "t", "b", alert_id="nocreds-1")

    assert delivered is False
    assert email.sends == [], "a channel with no send credentials must not be asked to send"
    assert any(
        "would have sent alert nocreds-1 via email" in rec.getMessage() for rec in caplog.records
    ), [rec.getMessage() for rec in caplog.records]
    assert [row["notification_type"] for row in notification_rows] == ["alert_fallback"]


@pytest.mark.asyncio
async def test_an_unknown_fallback_channel_is_not_a_crash(
    configure, channels, no_telegram, notification_rows
):
    configure(fallback="no-such-channel:somewhere")

    assert await alerts.alert("critical", "t", "b", alert_id="unknown-1") is False
    assert [row["notification_type"] for row in notification_rows] == ["alert_fallback"]


# ── suppression ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_same_alert_id_is_not_delivered_twice_inside_its_window(
    configure, channels, no_telegram, notification_rows
):
    configure(fallback=f"email:{FALLBACK_TARGET}")
    email = channels["email"] = FakeChannel("email")

    first = await alerts.alert("critical", "Repeat", "b", alert_id="repeat-1")
    second = await alerts.alert("critical", "Repeat", "b", alert_id="repeat-1")

    assert first is True
    assert second is True, "a suppressed repeat was delivered already; it is not a failure"
    assert len(email.sends) == 1, "a repeat inside the suppression window was delivered again"


@pytest.mark.asyncio
async def test_a_different_alert_id_is_not_suppressed(
    configure, channels, no_telegram, notification_rows
):
    configure(fallback=f"email:{FALLBACK_TARGET}")
    email = channels["email"] = FakeChannel("email")

    await alerts.alert("critical", "Repeat", "b", alert_id="distinct-1")
    await alerts.alert("critical", "Repeat", "b", alert_id="distinct-2")

    assert len(email.sends) == 2


@pytest.mark.asyncio
async def test_an_undelivered_alert_is_not_suppressed(
    configure, channels, no_telegram, notification_rows
):
    """Suppression records a DELIVERY. A repeat of an alert nobody received
    is the retry, and swallowing it would turn one outage into two."""
    configure(fallback=f"email:{FALLBACK_TARGET}")
    email = channels["email"] = FakeChannel("email", lands=False)

    await alerts.alert("critical", "t", "b", alert_id="retry-1")
    email.lands = True
    delivered = await alerts.alert("critical", "t", "b", alert_id="retry-1")

    assert delivered is True
    assert len(email.sends) == 2


# ── primary and fallback both failing ───────────────────────────────────────


@pytest.mark.asyncio
async def test_when_every_channel_fails_one_error_line_names_them_all(
    configure, channels, telegram, notification_rows, caplog
):
    configure(fallback=f"email:{FALLBACK_TARGET}", chat_id="12345")
    telegram["lands"] = False
    channels["email"] = FakeChannel("email", lands=False)

    with caplog.at_level(logging.INFO, logger="robothor.engine.alerts"):
        delivered = await alerts.alert("critical", "t", "b", alert_id="allfail-1")

    assert delivered is False
    errors = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.ERROR and "allfail-1" in rec.getMessage()
    ]
    assert len(errors) == 1, errors
    assert "telegram" in errors[0] and "email" in errors[0], errors
    assert [row["notification_type"] for row in notification_rows] == ["alert_fallback"]


# ── unchanged behaviour ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_digest_levels_never_reach_a_fallback(
    configure, channels, no_telegram, notification_rows
):
    """Only a page is interrupted delivery; warning/info stay in the digest."""
    configure(fallback=f"email:{FALLBACK_TARGET}")
    email = channels["email"] = FakeChannel("email")

    assert await alerts.alert("warning", "t", "b", alert_id="digest-1") is True
    assert email.sends == []
    assert [row["notification_type"] for row in notification_rows] == ["alert_digest"]


def test_the_fallback_list_parses_in_order() -> None:
    assert alerts.parse_fallback_channels(
        f" email:{FALLBACK_TARGET} , slack:C0123456,, plugin-pager:team-a "
    ) == [("email", FALLBACK_TARGET), ("slack", "C0123456"), ("plugin-pager", "team-a")]
    assert alerts.parse_fallback_channels("") == []


# ── the real email channel, with the database down ──────────────────────────
#
# The alert most worth delivering is the one about the database. The email
# channel reads its opt-out list from that database before every send, so the
# fallback must not depend on the list being readable for an operator-configured
# alert target, while an ordinary outbound mail still refuses to send unread.


class _RecordingSMTP:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def __call__(self, host: str, port: int, *, timeout: float = 30.0) -> _RecordingSMTP:
        return self

    def starttls(self) -> None:
        return

    def login(self, user: str, password: str) -> None:
        return

    def send_message(self, message: Any) -> dict[str, Any]:
        self.messages.append(message)
        return {}

    def quit(self) -> None:
        return


@pytest.fixture
def email_with_db_down(monkeypatch, configure):
    """The real EmailChannel over a recording SMTP server, database unreachable."""
    import psycopg2

    from robothor.engine.channels import email as email_module
    from robothor.engine.channels import registry

    monkeypatch.setenv("ROBOTHOR_EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("ROBOTHOR_EMAIL_FROM", "agent@example.com")
    configure(fallback=f"email:{FALLBACK_TARGET}")

    def database_down(*_a: Any, **_kw: Any) -> set[str]:
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(email_module, "_opt_out_lookup", database_down)
    monkeypatch.setattr(email_module, "_dnc_mode", lambda: "enforce")

    smtp = _RecordingSMTP()
    channel = email_module.EmailChannel()
    channel.gws_probe = lambda: False
    channel.smtp_factory = smtp
    monkeypatch.setattr(registry, "get_channel", lambda name: channel if name == "email" else None)
    return channel, smtp


@pytest.mark.asyncio
async def test_a_database_down_alert_still_reaches_the_email_fallback(
    email_with_db_down, no_telegram, notification_rows
):
    _channel, smtp = email_with_db_down

    delivered = await alerts.alert("critical", "Database down", "3 failed pings")

    assert delivered is True, "the alert about the database was blocked by the database"
    assert len(smtp.messages) == 1
    assert smtp.messages[0]["To"] == FALLBACK_TARGET


@pytest.mark.asyncio
async def test_ordinary_mail_still_refuses_when_the_opt_out_list_is_unreadable(
    email_with_db_down,
):
    from types import SimpleNamespace

    from robothor.engine.channels.email import STATUS_DNC_UNREADABLE

    channel, smtp = email_with_db_down
    run = SimpleNamespace(id="", tenant_id="test-tenant", is_benchmark=False)

    receipt = await channel.send("bob@example.com", "hello", run=run)

    assert receipt.status == STATUS_DNC_UNREADABLE
    assert smtp.messages == [], "an ordinary send went out with the opt-out list unread"
