"""A DB operator row must change what the engine's readers return — no restart."""

from __future__ import annotations

from robothor.engine import feature_flags
from robothor.flags import store


def test_rip7_mode_reads_the_store(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_RIP_7_ENABLED", "1")
    monkeypatch.setattr(
        store, "resolve", lambda name: "enforce" if name == "ROBOTHOR_RIP_7_MODE" else None
    )
    store.invalidate()
    assert feature_flags.rip_7_enforcement_mode() == "enforce"


def test_env_still_works_when_store_returns_none(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_RIP_7_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_RIP_7_MODE", "alert")
    monkeypatch.setattr(store, "resolve", lambda name: None)
    assert feature_flags.rip_7_enforcement_mode() == "alert"
