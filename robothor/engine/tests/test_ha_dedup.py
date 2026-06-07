"""Redis-backed (HA) dedup (Wave-2, W2-3).

When ROBOTHOR_HA_DEDUP_ENABLED is set, dedup uses a Redis lease so concurrent
runs of the same agent are prevented across replicas. Default off = in-process.
Redis errors fall back to in-process.
"""

from __future__ import annotations

import pytest

from robothor.engine import dedup, redis_lease


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    dedup.clear()
    dedup._owners.clear()
    yield
    dedup.clear()
    dedup._owners.clear()


async def test_in_process_by_default(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_HA_DEDUP_ENABLED", raising=False)
    assert await dedup.try_acquire("a") is True
    assert await dedup.try_acquire("a") is False  # already running, same process
    await dedup.release("a")
    assert await dedup.try_acquire("a") is True


async def test_ha_uses_lease(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_HA_DEDUP_ENABLED", "1")
    calls = {"acquire": 0, "release": 0}

    def _acquire(key, ttl, **k):
        calls["acquire"] += 1
        return "tok" if calls["acquire"] == 1 else None  # first wins, second contended

    monkeypatch.setattr(redis_lease, "acquire", _acquire)
    monkeypatch.setattr(
        redis_lease,
        "release",
        lambda key, owner, **k: calls.__setitem__("release", calls["release"] + 1) or True,
    )

    assert await dedup.try_acquire("a") is True
    assert dedup._owners["a"] == "tok"
    assert await dedup.try_acquire("a") is False  # contended via lease
    await dedup.release("a")
    assert calls["release"] == 1
    assert "a" not in dedup._owners


async def test_ha_falls_back_on_redis_error(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_HA_DEDUP_ENABLED", "1")

    def _boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis_lease, "acquire", _boom)
    # falls back to in-process — still works
    assert await dedup.try_acquire("a") is True
    assert await dedup.try_acquire("a") is False
