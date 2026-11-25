"""Every alert reaches the log; a configured webhook is a fallback route.

Where no alert channel was configured, an alert ended at "can't deliver alert"
and nobody saw it. Two changes:

* Every alert, whatever its level and whatever happens to delivery, writes one
  ERROR line: ``event=alert kind=<kind> severity=<level> subject="…"``. A log
  pipeline is always there, so the alert is never only in a table. A repeat
  suppressed inside its cooldown writes no second line, and an alert raised by
  benchmark traffic is not an ERROR.
* ``ROBOTHOR_ALERT_WEBHOOK_URL``, already the target of ``channel="webhook"``,
  is tried after the fallback channels when a page has not been delivered.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from unittest.mock import AsyncMock

import pytest

import robothor.crm.dal as dal
from robothor.engine import alerts
from robothor.engine.delivery import set_telegram_sender


def _alert_lines(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("event=alert ")]


def _fields(line: str) -> dict[str, str]:
    pairs = re.findall(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)', line)
    return {k: (v[1:-1] if v.startswith('"') else v) for k, v in pairs}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, caplog):
    from robothor.settings import reset_settings

    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(dal, "send_notification", lambda **kw: rows.append(kw) or "notif-1")
    for name in (
        "ROBOTHOR_ALERT_FALLBACK_CHANNELS",
        "ROBOTHOR_TELEGRAM_CHAT_ID",
        "TELEGRAM_CHAT_ID",
        "ROBOTHOR_ALERT_WEBHOOK_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    set_telegram_sender(None)  # type: ignore[arg-type]
    alerts._delivered_at.clear()
    caplog.set_level(logging.DEBUG)
    yield rows
    set_telegram_sender(None)  # type: ignore[arg-type]
    alerts._delivered_at.clear()
    reset_settings()


class TestEveryAlertIsLogged:
    @pytest.mark.asyncio
    async def test_an_undeliverable_page_writes_one_error_line(self, caplog) -> None:
        await alerts.alert("critical", "Tool outage: example_tool", "9/9 calls failed")
        (line,) = _alert_lines(caplog)
        assert line.levelno == logging.ERROR
        f = _fields(line.getMessage())
        assert f["kind"] == "tool_outage"
        assert f["subject"] == "example_tool"
        assert f["severity"] == "critical"

    @pytest.mark.asyncio
    async def test_a_digest_alert_is_logged_too(self, caplog) -> None:
        await alerts.alert("warning", "Stuck workflow run", "running for 3h")
        (line,) = _alert_lines(caplog)
        assert line.levelno == logging.ERROR
        f = _fields(line.getMessage())
        assert f["kind"] == "stuck_workflow_run"
        assert f["severity"] == "warning"

    @pytest.mark.asyncio
    async def test_an_explicit_kind_wins(self, caplog) -> None:
        await alerts.alert("warning", "Something odd", "detail", kind="example_kind")
        assert _fields(_alert_lines(caplog)[0].getMessage())["kind"] == "example_kind"

    @pytest.mark.asyncio
    async def test_the_body_never_reaches_the_line(self, caplog) -> None:
        await alerts.alert("critical", "Tool outage: example_tool", "token=abcdef0123456789abcdef")
        assert "abcdef0123456789abcdef" not in _alert_lines(caplog)[0].getMessage()

    @pytest.mark.asyncio
    async def test_a_suppressed_repeat_writes_no_second_line(self, monkeypatch, caplog) -> None:
        from robothor.settings import reset_settings

        async def sender(chat_id, text):
            return ["message"]

        set_telegram_sender(sender)
        monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "123")
        reset_settings()
        await alerts.alert("critical", "Database down", "x", alert_id="db-down")
        await alerts.alert("critical", "Database down", "x", alert_id="db-down")
        assert len(_alert_lines(caplog)) == 1

    @pytest.mark.asyncio
    async def test_a_benchmark_alert_is_not_an_error(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr("robothor.engine.run_context.in_benchmark_run", lambda: True)
        await alerts.alert("critical", "Tool outage: example_tool", "graded run")
        assert all(r.levelno < logging.ERROR for r in _alert_lines(caplog))

    @pytest.mark.asyncio
    async def test_an_alert_raised_in_a_run_names_its_tenant_and_trace(self, caplog) -> None:
        await alerts.alert(
            "critical",
            "Runaway-token hard cap: example-agent",
            "x",
            tenant_id="acme-child-a",
            trace_id="0af7651916cd43dd8448eb211c80319c",
        )
        f = _fields(_alert_lines(caplog)[0].getMessage())
        assert f["tenant_id"] == "acme-child-a"
        assert f["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
        assert list(f)[-2:] == ["tenant_id", "trace_id"]

    def test_run_scope_reads_the_tenant_and_trace_off_a_session(self) -> None:
        from types import SimpleNamespace

        session = SimpleNamespace(run=SimpleNamespace(tenant_id="acme-child-a"), trace_id="ab" * 16)
        assert alerts.run_scope(session) == {"tenant_id": "acme-child-a", "trace_id": "ab" * 16}
        assert alerts.run_scope(None) == {"tenant_id": "", "trace_id": ""}

    @pytest.mark.asyncio
    async def test_an_alert_outside_any_run_omits_both_keys(self, caplog) -> None:
        await alerts.alert("critical", "Tool outage: example_tool", "9/9 calls failed")
        f = _fields(_alert_lines(caplog)[0].getMessage())
        assert "tenant_id" not in f
        assert "trace_id" not in f


class TestWebhookFallback:
    @pytest.mark.asyncio
    async def test_an_unset_primary_pages_through_the_webhook(self, monkeypatch) -> None:
        send = AsyncMock(return_value=True)
        monkeypatch.setattr(alerts, "alert_webhook_url", lambda: "https://hooks.example.com/x")
        monkeypatch.setattr(alerts, "_send_webhook", send)
        delivered = await alerts.alert("critical", "Database down", "3 failed pings")
        assert delivered is True
        send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_without_a_webhook_the_page_still_fails(self, monkeypatch, _isolated) -> None:
        send = AsyncMock(return_value=True)
        monkeypatch.setattr(alerts, "alert_webhook_url", lambda: "")
        monkeypatch.setattr(alerts, "_send_webhook", send)
        delivered = await alerts.alert("critical", "Database down", "3 failed pings")
        assert delivered is False
        send.assert_not_awaited()
        assert any(r.get("notification_type") == "alert_fallback" for r in _isolated)


@pytest.mark.asyncio
async def test_a_fallback_delivery_prints_no_cant_deliver_warning(monkeypatch, caplog) -> None:
    """The primary not being set up is said once, in the line naming the fallback."""
    monkeypatch.setattr(alerts, "alert_webhook_url", lambda: "https://hooks.example.com/x")
    monkeypatch.setattr(alerts, "_send_webhook", AsyncMock(return_value=True))
    assert await alerts.alert("critical", "Database down", "3 failed pings") is True
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "can't deliver alert" in r.getMessage()
    ]
