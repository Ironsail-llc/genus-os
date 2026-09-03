"""Alert delivery self-test hook — a code-free live probe that must not page.

``ROBOTHOR_ALERT_SELFTEST=1`` fires one alert shortly after daemon startup,
once subsystems are up, so the ``alert()`` path can be exercised on a running
box without waiting for a real incident to trip it.

It has now been wrong in both directions, which is the whole lesson:

* It first fired at ``info``, while its own docstring claimed to verify the
  ``alert() -> send_fn(chat_id, text)`` arity end-to-end. ``info`` is not in
  ``_PAGE_LEVELS``, so it wrote a database row and never touched the sender —
  a probe that could not fail, reading as a pass.
* Raising it to ``critical`` made it honest and made it a pager: the engine
  restarts, so the flag paged the operator CRITICAL on every start — 52 pages
  in 7 days, none of them an incident. A self-test that trains the operator to
  ignore red is worse than the blind spot it replaced.

So the probe is a ``digest`` probe, and says so: it fires at ``info``, which
writes an ``alert_digest`` row the operator agent surfaces on its next
heartbeat, and it asserts that the row was actually written. What it proves is
that ``alert()`` runs and reaches durable storage on this box. What it must
never do is page. Telegram delivery is proved by the paths that page for real
(``scripts/send_failure_alert.sh``, whose own delivery is verified by HTTP
status, and the liveness watchdog) — not by an alert the engine fires at
itself every time it boots.
"""

from __future__ import annotations

import pytest

from robothor.engine import alerts, daemon


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
    _level, title, _body = calls[0]
    assert "self-test" in title.lower()


@pytest.mark.asyncio
async def test_the_selftest_never_pages(monkeypatch):
    """The flag is set on a box that restarts. Paging on every start produced
    52 CRITICAL pages in 7 days and taught the operator to scroll past red."""
    monkeypatch.setenv("ROBOTHOR_ALERT_SELFTEST", "1")

    levels: list[str] = []

    async def fake_alert(level, title, body, **kwargs):
        levels.append(level)
        return True

    monkeypatch.setattr("robothor.engine.alerts.alert", fake_alert)

    await daemon._maybe_run_alert_selftest()

    assert levels and levels[0] not in alerts._PAGE_LEVELS, (
        f"the self-test fired at {levels[0]!r}, which pages the operator on "
        "every single engine start"
    )


@pytest.mark.asyncio
async def test_the_selftest_says_so_when_the_row_was_not_written(monkeypatch, caplog):
    """A probe that fails quietly is worse than no probe: it reads as success.
    The level dropped, the loudness did not."""
    import logging

    monkeypatch.setenv("ROBOTHOR_ALERT_SELFTEST", "1")
    caplog.set_level(logging.ERROR)

    async def fake_alert(level, title, body, **kwargs):
        return False

    monkeypatch.setattr("robothor.engine.alerts.alert", fake_alert)

    await daemon._maybe_run_alert_selftest()

    assert "self-test" in caplog.text.lower()


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
