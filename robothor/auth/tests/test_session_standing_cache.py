"""The standing cache is keyed by (account, home tenant), not by account.

The answer depends on the home tenant the lookup is bound to: with RLS on, an
account moved from A to B is invisible from A and visible from B. Keyed by the
account alone, the old home-A token cached "absent" and the fresh home-B token
issued at the next sign-in read that same entry — signing the user straight back
out for up to the TTL.
"""

from __future__ import annotations

import pytest

from robothor.auth import session_standing

ALICE = "00000000-0000-4000-8000-0000000000a1"


@pytest.fixture(autouse=True)
def _clean():
    session_standing.forget_all()
    yield
    session_standing.forget_all()


def _moved_from_a_to_b(monkeypatch):
    calls: list[tuple[str, str]] = []

    def _lookup(user_id, home_tenant_id):
        calls.append((user_id, home_tenant_id))
        # RLS: invisible from the old home, active in the new one.
        return ("acme-child-b", "active") if home_tenant_id == "acme-child-b" else (None, "")

    monkeypatch.setattr(session_standing, "_lookup", _lookup)
    return calls


def test_two_homes_for_one_account_are_answered_separately(monkeypatch):
    _moved_from_a_to_b(monkeypatch)
    assert session_standing.stands(ALICE, "acme-child-a") is False
    assert session_standing.stands(ALICE, "acme-child-b") is True
    assert session_standing.stands(ALICE, "acme-child-a") is False


def test_each_home_is_cached_on_its_own(monkeypatch):
    calls = _moved_from_a_to_b(monkeypatch)
    for _ in range(3):
        session_standing.stands(ALICE, "acme-child-a")
        session_standing.stands(ALICE, "acme-child-b")
    assert sorted(set(calls)) == [(ALICE, "acme-child-a"), (ALICE, "acme-child-b")]
    assert len(calls) == 2


def test_forget_drops_every_home_for_the_account(monkeypatch):
    calls = _moved_from_a_to_b(monkeypatch)
    session_standing.stands(ALICE, "acme-child-a")
    session_standing.stands(ALICE, "acme-child-b")
    session_standing.forget(ALICE)
    session_standing.stands(ALICE, "acme-child-a")
    session_standing.stands(ALICE, "acme-child-b")
    assert len(calls) == 4


def test_forget_leaves_other_accounts_cached(monkeypatch):
    calls = _moved_from_a_to_b(monkeypatch)
    session_standing.stands(ALICE, "acme-child-b")
    session_standing.stands("someone-else", "acme-child-b")
    session_standing.forget(ALICE)
    session_standing.stands("someone-else", "acme-child-b")
    assert calls.count(("someone-else", "acme-child-b")) == 1
