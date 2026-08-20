"""A model that is down should stop costing us a timeout on every run.

`broken_models` is per-run: it resets each time. So a model that is genuinely
dead — a revoked key, a banned account — gets retried on EVERY run, burning the
full 120s per-call timeout before falling back, forever. That is not
hypothetical: codex/* auth was dead from 2026-06-01 and the fleet kept dialling
it for a month, silently paying OpenRouter for the fallback while nobody
noticed the primary had died.

A circuit breaker fixes both halves:
  * skip a model that has failed N times in a row until a cooldown expires, so
    the fleet stops paying the timeout tax; and
  * tell the operator the first time it trips, so a dead primary cannot go
    unnoticed for a month.
"""

from __future__ import annotations

from robothor.engine.model_breaker import ModelBreaker, get_model_breaker


def test_closed_by_default():
    b = ModelBreaker(threshold=3, cooldown_seconds=60)
    assert not b.is_open("gpt-x")


def test_opens_after_consecutive_failures():
    b = ModelBreaker(threshold=3, cooldown_seconds=60, now=lambda: 100.0)
    for _ in range(2):
        b.record_failure("gpt-x")
    assert not b.is_open("gpt-x"), "should not trip before the threshold"
    b.record_failure("gpt-x")
    assert b.is_open("gpt-x"), "third consecutive failure must open the circuit"


def test_success_resets_the_count():
    b = ModelBreaker(threshold=3, cooldown_seconds=60, now=lambda: 100.0)
    b.record_failure("gpt-x")
    b.record_failure("gpt-x")
    b.record_success("gpt-x")
    b.record_failure("gpt-x")
    assert not b.is_open("gpt-x"), "a success must clear the streak"


def test_circuit_closes_again_after_the_cooldown():
    clock = {"t": 100.0}
    b = ModelBreaker(threshold=1, cooldown_seconds=60, now=lambda: clock["t"])
    b.record_failure("gpt-x")
    assert b.is_open("gpt-x")
    clock["t"] = 161.0  # past the cooldown
    assert not b.is_open("gpt-x"), "cooldown expiry must half-open the circuit"


def test_alerts_the_operator_once_when_it_trips():
    alerts: list[tuple[str, str]] = []
    b = ModelBreaker(
        threshold=1,
        cooldown_seconds=60,
        now=lambda: 100.0,
        on_open=lambda model, reason: alerts.append((model, reason)),
    )
    b.record_failure("gpt-x", reason="401 unauthorized")
    b.record_failure("gpt-x", reason="401 unauthorized")

    assert len(alerts) == 1, (
        "the operator must be told the first time a model trips — a dead primary "
        "went unnoticed for a month — but must not be spammed on every failure"
    )
    assert alerts[0][0] == "gpt-x"
    assert "401" in alerts[0][1]


# ─── The global breaker must be inert under pytest ─────────────────────
#
# The production singleton is wired to _alert_operator, which writes a real
# crm_agent_notifications row and posts to Telegram. 92 of the 145 breaker
# escalations in the operator's inbox were written by pytest runs tripping the
# global breaker with the fixture model 'openrouter/test/model'. The autouse
# fixture in robothor/engine/tests/conftest.py swaps in a fresh breaker with
# on_open=None per test; these tests pin that behaviour.


def test_global_breaker_is_inert_under_pytest():
    b = get_model_breaker()
    assert b.on_open is None, (
        "tests must never send real operator escalations — the engine tests "
        "conftest must swap the global breaker for one with on_open=None"
    )
    # Tripping it fully must not raise and must not alert anyone.
    for _ in range(b.threshold):
        b.record_failure("openrouter/test/model", reason="unit-test trip")
    assert b.is_open("openrouter/test/model")


def test_global_breaker_state_does_not_leak_between_tests():
    # The previous test opened the circuit for the fixture model; a fresh
    # breaker per test means this test must see it closed again.
    assert not get_model_breaker().is_open("openrouter/test/model")
