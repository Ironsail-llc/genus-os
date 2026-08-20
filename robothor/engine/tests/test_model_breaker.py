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

import pytest

from robothor.engine import model_breaker
from robothor.engine.model_breaker import ModelBreaker


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


# ─── operator alert path: copy, dedup, guardrail evidence ───────────────
#
# The 145-row escalation spam had two causes: the alert re-armed on every
# 600s cooldown cycle (in-process ``alerted`` flag resets on half-open), and
# the alert path never wrote agent_guardrail_events, so dashboards showed
# nothing while the operator's inbox filled up. These tests pin the fix:
# a persistent per-model re-alert floor and a guardrail-event record per trip.


@pytest.fixture
def alert_env(monkeypatch, tmp_path):
    """Route the dedup state file to tmp and capture deliveries."""
    monkeypatch.setenv("ROBOTHOR_MODEL_BREAKER_STATE", str(tmp_path / "model-breaker-alerts.json"))
    # The pytest guard suppresses real delivery under test runs — lift it so
    # these tests can observe the delivery calls (which are mocked anyway).
    monkeypatch.setattr(model_breaker, "_in_pytest", lambda: False)

    delivered: dict[str, list] = {"notifications": [], "telegram": [], "guardrail": []}

    def fake_send_notification(**kwargs):
        delivered["notifications"].append(kwargs)
        return "notif-1"

    def fake_post_telegram(text):
        delivered["telegram"].append(text)
        return True

    def fake_log_guardrail_event(run_id, guardrail_name, action, **kwargs):
        delivered["guardrail"].append(
            {"run_id": run_id, "guardrail_name": guardrail_name, "action": action, **kwargs}
        )

    monkeypatch.setattr("robothor.crm.dal.send_notification", fake_send_notification)
    monkeypatch.setattr("robothor.engine.feature_flags._post_telegram", fake_post_telegram)
    monkeypatch.setattr("robothor.engine.tracking.log_guardrail_event", fake_log_guardrail_event)
    return delivered


def test_alert_message_copy(alert_env):
    model_breaker._alert_operator("openrouter/x/model", "timeout after 120s")

    assert len(alert_env["notifications"]) == 1
    body = alert_env["notifications"][0]["body"]
    assert (
        f"model openrouter/x/model circuit OPEN "
        f"({model_breaker.DEFAULT_THRESHOLD} consecutive failures), "
        f"skipped for {model_breaker.DEFAULT_COOLDOWN}s" in body
    )
    assert "timeout after 120s" in body
    # The old borrowed guardrail template lied — the breaker always enforces.
    assert "would have BLOCKED" not in body
    assert len(alert_env["telegram"]) == 1


def test_realert_within_dedup_window_is_suppressed(alert_env, monkeypatch):
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(model_breaker.time, "time", lambda: clock["t"])

    model_breaker._alert_operator("openrouter/x/model", "boom")
    clock["t"] += 3600.0  # one hour later — well inside the 6h floor
    model_breaker._alert_operator("openrouter/x/model", "boom again")

    assert len(alert_env["notifications"]) == 1, "re-trip within 6h must not re-notify"


def test_realert_after_dedup_window_notifies_again(alert_env, monkeypatch):
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(model_breaker.time, "time", lambda: clock["t"])

    model_breaker._alert_operator("openrouter/x/model", "boom")
    clock["t"] += model_breaker.ALERT_DEDUP_SECONDS + 1.0
    model_breaker._alert_operator("openrouter/x/model", "boom again")

    assert len(alert_env["notifications"]) == 2


def test_dedup_is_per_model(alert_env, monkeypatch):
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(model_breaker.time, "time", lambda: clock["t"])

    model_breaker._alert_operator("openrouter/a", "boom")
    model_breaker._alert_operator("openrouter/b", "boom")

    assert len(alert_env["notifications"]) == 2, "a different model's first trip must alert"


def test_dedup_survives_process_restart(alert_env, monkeypatch):
    """The floor is persistent — a restart (fresh in-process state) must not re-alert."""
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(model_breaker.time, "time", lambda: clock["t"])

    model_breaker._alert_operator("openrouter/x/model", "boom")
    # A restart resets all in-process state; only the state file survives.
    clock["t"] += 60.0
    model_breaker._alert_operator("openrouter/x/model", "boom after restart")

    assert len(alert_env["notifications"]) == 1


def test_trip_records_guardrail_event_when_run_context_set(alert_env):
    token = model_breaker._current_run_id_var.set("run-123")
    try:
        model_breaker._alert_operator("openrouter/x/model", "boom")
    finally:
        model_breaker._current_run_id_var.reset(token)

    assert len(alert_env["guardrail"]) == 1
    event = alert_env["guardrail"][0]
    assert event["run_id"] == "run-123"
    assert event["guardrail_name"] == "model_breaker"
    assert event["action"] == "blocked"


def test_no_guardrail_event_without_run_context(alert_env):
    model_breaker._alert_operator("openrouter/x/model", "boom")
    assert alert_env["guardrail"] == []


def test_guardrail_event_recorded_even_when_notification_deduped(alert_env, monkeypatch):
    """Evidence lands on every trip; only the operator ping is deduped."""
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(model_breaker.time, "time", lambda: clock["t"])

    token = model_breaker._current_run_id_var.set("run-123")
    try:
        model_breaker._alert_operator("openrouter/x/model", "boom")
        clock["t"] += 60.0
        model_breaker._alert_operator("openrouter/x/model", "boom again")
    finally:
        model_breaker._current_run_id_var.reset(token)

    assert len(alert_env["guardrail"]) == 2
    assert len(alert_env["notifications"]) == 1


def test_pytest_guard_suppresses_delivery(alert_env, monkeypatch):
    """Test sessions tripping the global breaker must not page the operator
    (92 of 145 production escalation rows came from pytest fixture models)."""
    monkeypatch.setattr(model_breaker, "_in_pytest", lambda: True)
    model_breaker._alert_operator("openrouter/test/model", "boom")
    assert alert_env["notifications"] == []
    assert alert_env["telegram"] == []


def test_unwritable_state_file_still_alerts(alert_env, monkeypatch):
    """Dedup persistence is best-effort — a bad path must fail open (alert)."""
    monkeypatch.setenv("ROBOTHOR_MODEL_BREAKER_STATE", "/proc/nonexistent-dir/state.json")
    model_breaker._alert_operator("openrouter/x/model", "boom")
    assert len(alert_env["notifications"]) == 1
