"""``alerts.delivery``: is there an alert channel that can deliver a page?

The primary pager is Telegram; ``ROBOTHOR_ALERT_FALLBACK_CHANNELS`` names the
fallbacks. Each configured channel gets its own row, and the check fails when
no configured channel can deliver -- including when nothing is configured, the
state in which every page reaches no one.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from robothor.doctor.checks import channels as channel_checks
from robothor.doctor.tests.conftest import make_ctx

FAKE_TOKEN = "1234567:AAfake-telegram-token-value"


class FakeChannel:
    inbound_router: Any | None = None

    def __init__(self, name: str, *, can_send: bool) -> None:
        self.name = name
        self.can_send = can_send
        self.sent = 0

    async def health(self) -> dict[str, Any]:
        report: dict[str, Any] = {"channel": self.name, "ok": self.can_send}
        if not self.can_send:
            report["error"] = "no send credentials configured"
        return report

    async def send(self, *a: Any, **kw: Any) -> Any:
        self.sent += 1
        raise AssertionError("the doctor must never send")


@pytest.fixture
def settings(monkeypatch):
    def _set(**pairs: str) -> None:
        from robothor.settings import reset_settings

        for name, value in pairs.items():
            monkeypatch.setenv(name, value)
        reset_settings()

    return _set


@pytest.fixture
def channels(monkeypatch):
    from robothor.engine.channels import registry

    table: dict[str, FakeChannel] = {}
    monkeypatch.setattr(registry, "get_channel", lambda name: table.get((name or "").strip()))
    return table


def _check():
    return next(item for item in channel_checks.CHECKS if item.id == "alerts.delivery")


def _run(ctx) -> list:
    answer = asyncio.run(_check().run(ctx))
    return answer if isinstance(answer, list) else [answer]


def test_the_check_is_registered_and_degrades_the_instance() -> None:
    check = _check()
    assert check.category == "channels"
    assert check.severity in ("required", "recommended")


def test_nothing_configured_fails_with_no_alert_channel(channels) -> None:
    rows = _run(make_ctx())
    assert [row.status for row in rows] == ["fail"]
    assert "no alert channel can deliver" in rows[0].detail


def test_doctor_only_alerts_delivery_exits_non_zero_with_no_channel(channels) -> None:
    from robothor.doctor.runner import run_sync

    report = run_sync(make_ctx(), only="alerts.delivery")
    assert not report.errored, report.error_detail
    assert any("no alert channel can deliver" in row.detail for row in report.results)
    assert report.exit_code == 1


def test_doctor_only_alerts_delivery_exits_zero_when_a_channel_can_send(settings, channels) -> None:
    from robothor.doctor.runner import run_sync

    settings(ROBOTHOR_ALERT_FALLBACK_CHANNELS="email:agent@example.com")
    channels["email"] = FakeChannel("email", can_send=True)
    assert run_sync(make_ctx(), only="alerts.delivery").exit_code == 0


def test_a_configured_primary_passes(settings, channels) -> None:
    settings(ROBOTHOR_TELEGRAM_BOT_TOKEN=FAKE_TOKEN, ROBOTHOR_TELEGRAM_CHAT_ID="12345")
    rows = _run(make_ctx(offline=True))
    assert all(row.status == "pass" for row in rows), rows
    assert any("telegram" in row.detail for row in rows), rows
    assert all(FAKE_TOKEN not in row.detail for row in rows)


def test_a_fallback_that_can_deliver_passes_with_no_primary(settings, channels) -> None:
    settings(ROBOTHOR_ALERT_FALLBACK_CHANNELS="email:agent@example.com")
    channels["email"] = FakeChannel("email", can_send=True)
    rows = _run(make_ctx())
    assert all(row.status == "pass" for row in rows), rows
    assert any("email" in row.detail for row in rows), rows
    assert channels["email"].sent == 0


def test_a_fallback_with_no_send_credentials_is_reported_unable(settings, channels) -> None:
    settings(ROBOTHOR_ALERT_FALLBACK_CHANNELS="email:agent@example.com")
    channels["email"] = FakeChannel("email", can_send=False)
    rows = _run(make_ctx())
    failed = [row for row in rows if row.status == "fail"]
    assert any("email" in row.detail and "cannot deliver" in row.detail for row in failed), rows
    assert any("no alert channel can deliver" in row.detail for row in failed), rows


def test_one_unable_channel_fails_even_when_another_delivers(settings, channels) -> None:
    settings(
        ROBOTHOR_TELEGRAM_BOT_TOKEN=FAKE_TOKEN,
        ROBOTHOR_TELEGRAM_CHAT_ID="12345",
        ROBOTHOR_ALERT_FALLBACK_CHANNELS="email:agent@example.com",
    )
    channels["email"] = FakeChannel("email", can_send=False)
    rows = _run(make_ctx(offline=True))
    statuses = {row.sub_id: row.status for row in rows}
    assert statuses.get("telegram") == "pass", rows
    assert statuses.get("email") == "fail", rows
    assert not any("no alert channel can deliver" in row.detail for row in rows)


def test_an_unregistered_fallback_channel_is_reported(settings, channels) -> None:
    settings(ROBOTHOR_ALERT_FALLBACK_CHANNELS="no-such-channel:somewhere")
    rows = _run(make_ctx())
    assert any(row.status == "fail" and "no-such-channel" in row.detail for row in rows), rows


def test_a_fallback_target_never_reaches_a_row(settings, channels) -> None:
    """The target is an address; a doctor row names the channel, not the person."""
    settings(ROBOTHOR_ALERT_FALLBACK_CHANNELS="email:agent@example.com")
    channels["email"] = FakeChannel("email", can_send=True)
    rows = _run(make_ctx())
    assert all("agent@example.com" not in row.detail for row in rows), rows
