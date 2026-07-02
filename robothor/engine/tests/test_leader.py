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


async def test_run_demotes_after_persistent_errors(monkeypatch):
    """If the leader can't reach Redis for longer than the lease TTL, it must
    demote itself rather than believe it's still leader (split-brain guard)."""
    monkeypatch.setenv("ROBOTHOR_HA_LEADER_ENABLED", "1")
    monkeypatch.setattr(redis_lease, "acquire", lambda *a, **k: "tok")

    def _renew_boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis_lease, "renew", _renew_boom)

    e = leader.LeaderElector()
    calls = {"n": 0}

    async def _fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] >= 4:  # stop after the demote tick
            e._stop = True

    monkeypatch.setattr(leader.asyncio, "sleep", _fake_sleep)
    await e.run()
    # tick1 acquired leadership, ticks 2-4 failed to renew (3 consecutive) → demote.
    assert e.is_leader() is False
    assert e._owner is None


async def test_run_resets_error_count_on_success(monkeypatch):
    """A single transient error must not demote — the counter resets on the next
    successful tick."""
    monkeypatch.setenv("ROBOTHOR_HA_LEADER_ENABLED", "1")
    monkeypatch.setattr(redis_lease, "acquire", lambda *a, **k: "tok")

    outcomes = iter([RuntimeError("blip"), True, True, True])

    def _renew(*a, **k):
        nxt = next(outcomes, True)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(redis_lease, "renew", _renew)
    e = leader.LeaderElector()
    calls = {"n": 0}

    async def _fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] >= 4:
            e._stop = True

    monkeypatch.setattr(leader.asyncio, "sleep", _fake_sleep)
    await e.run()
    assert e.is_leader() is True  # one blip, then recovered — never demoted
