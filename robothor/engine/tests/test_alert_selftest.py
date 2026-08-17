"""Alert delivery self-test hook — a code-free live probe.

ROBOTHOR_ALERT_SELFTEST=1 fires one info alert shortly after daemon
startup, once subsystems (and the Telegram sender) are up, so the
fixed alert() send_fn(chat_id, text) arity can be verified on a
running box without waiting for a real incident to trip it.
"""

from __future__ import annotations

import pytest

from robothor.engine import daemon


@pytest.mark.asyncio
async def test_selftest_fires_one_alert_when_env_set(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_ALERT_SELFTEST", "1")

    calls: list[tuple[str, str, str]] = []

    async def fake_alert(level, title, body, **kwargs):
        calls.append((level, title, body))
        return True

    monkeypatch.setattr("robothor.engine.alerts.alert", fake_alert)

    await daemon._maybe_run_alert_selftest()

    assert len(calls) == 1, "ROBOTHOR_ALERT_SELFTEST=1 must fire exactly one alert"
    level, title, _body = calls[0]
    assert level == "info"
    assert "self-test" in title.lower()


@pytest.mark.asyncio
async def test_selftest_does_not_fire_when_env_unset(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_ALERT_SELFTEST", raising=False)

    calls: list[tuple[str, str, str]] = []

    async def fake_alert(level, title, body, **kwargs):
        calls.append((level, title, body))
        return True

    monkeypatch.setattr("robothor.engine.alerts.alert", fake_alert)

    await daemon._maybe_run_alert_selftest()

    assert calls == []
