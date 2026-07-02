"""Redis lease primitive (Wave-2, W2-1).

Uses a minimal in-memory fake redis (fakeredis isn't installed) implementing
exactly the ops the lease needs: SET NX PX, GET, and the two compare-and-act
Lua scripts (release = compare-and-del, renew = compare-and-pexpire).
"""

from __future__ import annotations

from robothor.engine import redis_lease


class FakeRedis:
    """In-memory fake honoring SET NX PX, GET, and the two CAS Lua scripts.

    Models TTL with a manual millisecond clock (``advance``) so lease expiry and
    renew-extends-TTL are actually exercised — the previous fake ignored PX, so
    the headline "crashed holder auto-expires, survivor reclaims" behavior was
    never tested.
    """

    def __init__(self):
        self.store: dict[str, tuple[bytes, int | None]] = {}
        self.now = 0  # ms

    def advance(self, ms: int) -> None:
        self.now += ms

    def _live(self, key):
        entry = self.store.get(key)
        if entry is None:
            return None
        val, exp = entry
        if exp is not None and self.now >= exp:
            del self.store[key]
            return None
        return val

    def set(self, key, value, nx=False, px=None):
        if nx and self._live(key) is not None:
            return None
        exp = self.now + px if px is not None else None
        self.store[key] = (value.encode() if isinstance(value, str) else value, exp)
        return True

    def get(self, key):
        return self._live(key)

    def eval(self, script, numkeys, key, *args):
        owner = args[0]
        owner_b = owner.encode() if isinstance(owner, str) else owner
        held = self._live(key)
        if held != owner_b:
            return 0
        if "del" in script:  # release
            del self.store[key]
            return 1
        if "pexpire" in script:  # renew — extend expiry from the current clock
            ttl = int(args[1])
            val, _ = self.store[key]
            self.store[key] = (val, self.now + ttl)
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


def test_ttl_expiry_allows_reacquire():
    """A crashed holder's lease auto-expires; a survivor reclaims it."""
    r = FakeRedis()
    assert redis_lease.acquire("k", 100, owner="A", redis_client=r) == "A"
    # B cannot acquire while A's lease is live.
    assert redis_lease.acquire("k", 100, owner="B", redis_client=r) is None
    # A "crashes" and stops renewing; its lease TTLs out.
    r.advance(101)
    assert redis_lease.current_owner("k", redis_client=r) is None
    assert redis_lease.acquire("k", 100, owner="B", redis_client=r) == "B"


def test_renew_extends_ttl():
    """Renewing before expiry keeps the lease alive past the original TTL."""
    r = FakeRedis()
    redis_lease.acquire("k", 100, owner="A", redis_client=r)
    r.advance(80)
    assert redis_lease.renew("k", "A", 100, redis_client=r) is True
    # Original TTL (100) would have expired at 100; renewed at 80 → expires 180.
    r.advance(50)  # now 130 — still owned because of the renew
    assert redis_lease.current_owner("k", redis_client=r) == "A"
    assert redis_lease.acquire("k", 100, owner="B", redis_client=r) is None


def test_renew_fails_after_expiry_and_reacquire():
    """Once A's lease expires and B reclaims it, A's renew must not steal it back."""
    r = FakeRedis()
    redis_lease.acquire("k", 100, owner="A", redis_client=r)
    r.advance(101)  # A expires
    assert redis_lease.acquire("k", 100, owner="B", redis_client=r) == "B"
    # A (crashed, now back) tries to renew — CAS on the wrong owner fails.
    assert redis_lease.renew("k", "A", 100, redis_client=r) is False
    assert redis_lease.current_owner("k", redis_client=r) == "B"
