"""Memory generation must rotate credentials like every other caller.

2026-08-27: ``generation.py`` read ``os.environ['OPENROUTER_API_KEY']``
directly, so it could never see a spare key even once one was configured.
It produced 1,135 remote fallbacks in 48h — the single highest-volume
consumer of the dead credential — and each one hammered a key the engine's
pool had already retired.
"""

from __future__ import annotations

from robothor.memory import generation


def test_the_primary_key_is_used_when_healthy(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-primary")
    monkeypatch.delenv("OPENROUTER_API_KEY_2", raising=False)
    generation._reset_key_pool()
    assert generation._remote_api_key() == "sk-primary"


def test_a_spare_is_used_after_the_primary_is_retired(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-primary")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-spare")
    generation._reset_key_pool()

    assert generation._remote_api_key() == "sk-primary"
    generation._retire_remote_key("sk-primary", status=402)
    assert generation._remote_api_key() == "sk-spare", (
        "memory generation still cannot rotate — a spare key does not help it"
    )


def test_an_exhausted_pool_reports_no_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-only")
    monkeypatch.delenv("OPENROUTER_API_KEY_2", raising=False)
    generation._reset_key_pool()
    generation._retire_remote_key("sk-only", status=401)
    assert generation._remote_api_key() is None


def test_a_weekly_cap_is_retired_as_periodic(monkeypatch):
    """Shares the engine's classification, so it is not retried every 15 min."""
    from robothor.engine.key_pool import Retirement

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-only")
    monkeypatch.delenv("OPENROUTER_API_KEY_2", raising=False)
    generation._reset_key_pool()
    generation._retire_remote_key("sk-only", status=403, detail="Key limit exceeded (weekly limit)")
    pool = generation._key_pool()
    assert pool is not None
    assert pool.status()[0].reason is Retirement.QUOTA_EXHAUSTED_PERIODIC


def test_no_key_configured_is_not_a_crash(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_2", raising=False)
    generation._reset_key_pool()
    assert generation._remote_api_key() is None
