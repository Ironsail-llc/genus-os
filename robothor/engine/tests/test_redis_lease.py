"""Redis lease primitive (Wave-2, W2-1).

Uses a minimal in-memory fake redis (fakeredis isn't installed) implementing
exactly the ops the lease needs: SET NX PX, GET, and the two compare-and-act
Lua scripts (release = compare-and-del, renew = compare-and-pexpire).
"""

from __future__ import annotations

from robothor.engine import redis_lease


class FakeRedis:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    def set(self, key, value, nx=False, px=None):
        if nx and key in self.store:
            return None
        self.store[key] = value.encode() if isinstance(value, str) else value
        return True

    def get(self, key):
        return self.store.get(key)

    def eval(self, script, numkeys, key, *args):
        owner = args[0]
        owner_b = owner.encode() if isinstance(owner, str) else owner
        held = self.store.get(key)
        if held != owner_b:
            return 0
        if "del" in script:  # release
            del self.store[key]
            return 1
        if "pexpire" in script:  # renew (no real TTL in the fake; just confirm ownership)
            return 1
        return 0


def test_acquire_then_contended():
    r = FakeRedis()
    tok = redis_lease.acquire("k", 5000, owner="A", redis_client=r)
    assert tok == "A"
    # second acquirer is blocked while A holds it
    assert redis_lease.acquire("k", 5000, owner="B", redis_client=r) is None


def test_renew_only_owner():
    r = FakeRedis()
    redis_lease.acquire("k", 5000, owner="A", redis_client=r)
    assert redis_lease.renew("k", "A", 5000, redis_client=r) is True
    assert redis_lease.renew("k", "B", 5000, redis_client=r) is False  # not the owner


def test_release_only_owner_then_reacquire():
    r = FakeRedis()
    redis_lease.acquire("k", 5000, owner="A", redis_client=r)
    assert redis_lease.release("k", "B", redis_client=r) is False  # B can't release A's lease
    assert redis_lease.release("k", "A", redis_client=r) is True
    # after release, B can acquire (the reclaim path)
    assert redis_lease.acquire("k", 5000, owner="B", redis_client=r) == "B"


def test_current_owner():
    r = FakeRedis()
    assert redis_lease.current_owner("k", redis_client=r) is None
    redis_lease.acquire("k", 5000, owner="A", redis_client=r)
    assert redis_lease.current_owner("k", redis_client=r) == "A"


def test_default_owner_is_stable_within_process():
    assert redis_lease.default_owner() == redis_lease.default_owner()
    assert str(redis_lease.os.getpid()) in redis_lease.default_owner()
