"""Leader election for the scheduler (Wave-2, W2-5)."""

from __future__ import annotations

import pytest

from robothor.engine import leader, redis_lease


@pytest.fixture(autouse=True)
def _reset():
    leader.set_elector(None)
    yield
    leader.set_elector(None)


def test_single_node_is_always_leader(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_HA_LEADER_ENABLED", raising=False)
    assert leader.is_leader() is True  # no elector + HA off
    e = leader.LeaderElector()
    assert e.is_leader() is True


async def test_ha_acquires_then_renews(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_HA_LEADER_ENABLED", "1")
    monkeypatch.setattr(redis_lease, "acquire", lambda key, ttl, **k: "tok")
    monkeypatch.setattr(redis_lease, "renew", lambda key, owner, ttl, **k: True)

    e = leader.LeaderElector()
    assert e.is_leader() is False  # follower until acquired
    await e._tick()
    assert e.is_leader() is True
    await e._tick()  # renew keeps leadership
    assert e.is_leader() is True


async def test_ha_loses_leadership_on_failed_renew(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_HA_LEADER_ENABLED", "1")
    monkeypatch.setattr(redis_lease, "acquire", lambda key, ttl, **k: "tok")
    monkeypatch.setattr(redis_lease, "renew", lambda key, owner, ttl, **k: False)
    e = leader.LeaderElector()
    await e._tick()  # acquire
    assert e.is_leader() is True
    await e._tick()  # renew fails → lost
    assert e.is_leader() is False


async def test_ha_follower_when_lease_held_elsewhere(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_HA_LEADER_ENABLED", "1")
    monkeypatch.setattr(redis_lease, "acquire", lambda key, ttl, **k: None)  # someone else holds it
    e = leader.LeaderElector()
    await e._tick()
    assert e.is_leader() is False


def test_singleton_is_leader(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_HA_LEADER_ENABLED", "1")
    e = leader.LeaderElector()
    leader.set_elector(e)
    assert leader.is_leader() is False  # follower until it acquires
