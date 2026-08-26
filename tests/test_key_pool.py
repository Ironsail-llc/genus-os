"""A single exhausted credential must not be able to stop the fleet."""

from __future__ import annotations

import pytest

from robothor.engine.key_pool import (
    CREDIT_COOLDOWN_SECONDS,
    KeyPool,
    Retirement,
    keys_from_env,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- discovery -------------------------------------------------------------


def test_single_key_is_a_pool_of_one(monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY", "sk-alpha")
    monkeypatch.delenv("PROVIDER_KEY_2", raising=False)
    assert keys_from_env("PROVIDER_KEY") == ["sk-alpha"]


def test_numbered_siblings_extend_the_pool_in_order(monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY", "sk-alpha")
    monkeypatch.setenv("PROVIDER_KEY_2", "sk-beta")
    monkeypatch.setenv("PROVIDER_KEY_3", "sk-gamma")
    assert keys_from_env("PROVIDER_KEY") == ["sk-alpha", "sk-beta", "sk-gamma"]


def test_the_sequence_stops_at_the_first_gap(monkeypatch):
    """_2 then _4 yields two keys, not a silent skip to _4.

    A gap is far more likely a typo than an intentional hole, and quietly
    honouring it hides the key the operator believes is loaded.
    """
    monkeypatch.setenv("PROVIDER_KEY", "sk-alpha")
    monkeypatch.setenv("PROVIDER_KEY_2", "sk-beta")
    monkeypatch.delenv("PROVIDER_KEY_3", raising=False)
    monkeypatch.setenv("PROVIDER_KEY_4", "sk-delta")
    assert keys_from_env("PROVIDER_KEY") == ["sk-alpha", "sk-beta"]


def test_blank_and_duplicate_entries_are_dropped(monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY", "sk-alpha")
    monkeypatch.setenv("PROVIDER_KEY_2", "   ")
    monkeypatch.setenv("PROVIDER_KEY_3", "sk-alpha")
    # A blank stops the walk, so _3 is never reached; the point is that a
    # whitespace-only value never becomes a usable credential.
    assert keys_from_env("PROVIDER_KEY") == ["sk-alpha"]


def test_missing_primary_is_an_empty_pool(monkeypatch):
    monkeypatch.delenv("PROVIDER_KEY", raising=False)
    assert keys_from_env("PROVIDER_KEY") == []


# --- rotation --------------------------------------------------------------


def test_current_returns_the_primary_first():
    pool = KeyPool(["sk-alpha", "sk-beta"])
    assert pool.current() == "sk-alpha"


def test_retiring_the_primary_advances_to_the_next():
    pool = KeyPool(["sk-alpha", "sk-beta"])
    pool.retire("sk-alpha", Retirement.CREDIT_EXHAUSTED)
    assert pool.current() == "sk-beta"


def test_retiring_every_key_leaves_nothing():
    pool = KeyPool(["sk-alpha", "sk-beta"])
    pool.retire("sk-alpha", Retirement.CREDIT_EXHAUSTED)
    pool.retire("sk-beta", Retirement.CREDIT_EXHAUSTED)
    assert pool.current() is None
    assert pool.exhausted()


def test_an_empty_pool_is_exhausted_and_yields_none():
    """With no keys configured the caller must behave exactly as it does today."""
    pool = KeyPool([])
    assert pool.current() is None
    assert pool.exhausted()


def test_retiring_a_key_that_is_not_ours_is_ignored():
    pool = KeyPool(["sk-alpha"])
    pool.retire("sk-unknown", Retirement.AUTH_FAILED)
    assert pool.current() == "sk-alpha"


def test_retiring_twice_does_not_skip_a_key():
    """A concurrent double-report must not burn the key behind it.

    Two in-flight calls on the same credential both fail with 402; each
    reports it. The second report must be a no-op, not an advance.
    """
    pool = KeyPool(["sk-alpha", "sk-beta", "sk-gamma"])
    pool.retire("sk-alpha", Retirement.CREDIT_EXHAUSTED)
    pool.retire("sk-alpha", Retirement.CREDIT_EXHAUSTED)
    assert pool.current() == "sk-beta"


# --- recovery --------------------------------------------------------------


def test_a_credit_exhausted_key_returns_after_the_cooldown():
    """Topping up the account must not require an engine restart."""
    clock = FakeClock()
    pool = KeyPool(["sk-alpha"], clock=clock)
    pool.retire("sk-alpha", Retirement.CREDIT_EXHAUSTED)
    assert pool.current() is None

    clock.advance(CREDIT_COOLDOWN_SECONDS + 1)
    assert pool.current() == "sk-alpha"


def test_an_auth_failure_never_comes_back():
    """A revoked key is not a temporary condition; retrying it is noise."""
    clock = FakeClock()
    pool = KeyPool(["sk-alpha"], clock=clock)
    pool.retire("sk-alpha", Retirement.AUTH_FAILED)

    clock.advance(CREDIT_COOLDOWN_SECONDS * 100)
    assert pool.current() is None


def test_recovery_restores_the_original_priority_order():
    """Once the primary is back it is used again, not left at the end.

    Order is the operator's stated preference — usually the cheapest or
    highest-limit key first — so recovery must restore it, not append.
    """
    clock = FakeClock()
    pool = KeyPool(["sk-alpha", "sk-beta"], clock=clock)
    pool.retire("sk-alpha", Retirement.CREDIT_EXHAUSTED)
    assert pool.current() == "sk-beta"

    clock.advance(CREDIT_COOLDOWN_SECONDS + 1)
    assert pool.current() == "sk-alpha"


# --- disclosure ------------------------------------------------------------


def test_fingerprint_does_not_contain_the_key():
    """Every log line and alert about a key goes through this.

    An OpenRouter key already leaked into a bench log through an exception
    repr once. Rotation multiplies the number of places a key gets mentioned,
    so the identifier used in those mentions must not be reversible.
    """
    key = "sk-or-v1-0123456789abcdef0123456789abcdef"
    fp = KeyPool([key]).fingerprint(key)
    assert key not in fp
    assert "0123456789" not in fp
    assert fp[-4:] not in key


def test_fingerprint_is_stable_and_distinguishes_keys():
    pool = KeyPool(["sk-alpha", "sk-beta"])
    assert pool.fingerprint("sk-alpha") == pool.fingerprint("sk-alpha")
    assert pool.fingerprint("sk-alpha") != pool.fingerprint("sk-beta")


def test_status_reports_fingerprints_never_keys():
    pool = KeyPool(["sk-alpha", "sk-beta"])
    pool.retire("sk-alpha", Retirement.CREDIT_EXHAUSTED)
    rendered = repr(pool.status())
    assert "sk-alpha" not in rendered
    assert "sk-beta" not in rendered
    assert "credit_exhausted" in rendered


def test_repr_does_not_leak_keys():
    """A pool caught in a traceback frame must not print its credentials."""
    pool = KeyPool(["sk-or-v1-secret"])
    assert "sk-or-v1-secret" not in repr(pool)


@pytest.mark.parametrize("reason", list(Retirement))
def test_every_retirement_reason_is_loggable_without_the_key(reason):
    pool = KeyPool(["sk-alpha"])
    pool.retire("sk-alpha", reason)
    assert "sk-alpha" not in repr(pool.status())
