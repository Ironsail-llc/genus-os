"""Refresh rotation that survives two dashboard replicas refreshing at once.

The dashboard de-duplicates concurrent refreshes in-process only. With two
replicas and no session affinity, both pods can present the SAME refresh token
within a second of each other. Refresh tokens are single-use, so before this
the loser got a 401 and the operator's next page request failed with
"authentication required".

The rule under test: a refresh token that was rotated less than
``tokens.REFRESH_REUSE_GRACE_SECONDS`` ago, and is presented again, answers
with the SAME successor it already handed out — no second session, no new
secret. After the window, or once the successor itself is gone (signed out,
revoked), it is refused exactly as before.

The derivation tests need no database. The rotation tests run against real
PostgreSQL (``integration``), because the window and the row lock are the
database's to enforce and a mocked cursor proves neither.
"""

from __future__ import annotations

import os
import threading
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from robothor.auth import accounts, tokens


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


# ── Derivation (no database) ─────────────────────────────────────────


def test_the_grace_window_is_thirty_seconds():
    assert tokens.REFRESH_REUSE_GRACE_SECONDS == 30


def test_a_successor_is_derived_from_the_token_it_replaces():
    raw, h = tokens.successor_refresh_token("refresh-token-a")
    again_raw, again_h = tokens.successor_refresh_token("refresh-token-a")
    assert (raw, h) == (again_raw, again_h)
    assert h == tokens.hash_refresh_token(raw)
    assert raw != "refresh-token-a"
    # The same shape as a freshly minted token: 64 URL-safe characters.
    fresh, _ = tokens.new_refresh_token()
    assert len(raw) == len(fresh) == 64
    assert all(c.isalnum() or c in "-_" for c in raw)


def test_different_tokens_have_different_successors():
    assert (
        tokens.successor_refresh_token("refresh-token-a")[0]
        != tokens.successor_refresh_token("refresh-token-b")[0]
    )


def test_a_successor_depends_on_the_signing_key(monkeypatch):
    first = tokens.successor_refresh_token("refresh-token-a")[0]
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "another-signing-key-at-least-32-bytes-long")
    tokens.reset_signing_key_cache()
    assert tokens.successor_refresh_token("refresh-token-a")[0] != first


def test_an_access_token_minted_twice_for_one_rotation_is_identical():
    jti = tokens.successor_jti("refresh-token-a")
    assert jti == tokens.successor_jti("refresh-token-a")
    assert jti != tokens.successor_jti("refresh-token-b")
    one = tokens.issue_access_token("uid-1", "default", "member", issued_at=1_700_000_000, jti=jti)
    two = tokens.issue_access_token("uid-1", "default", "member", issued_at=1_700_000_000, jti=jti)
    assert one == two


# ── Rotation against real PostgreSQL ─────────────────────────────────

PG_DSN = os.environ.get(
    "ROBOTHOR_TEST_DB_DSN",
    "dbname=robothor_test user=robothor host=/var/run/postgresql",
)


def _connect():
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        return psycopg2.connect(PG_DSN)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"no test database reachable at ROBOTHOR_TEST_DB_DSN: {exc}")


@contextmanager
def _fresh_connection():
    """One new connection per call, like a pod with its own pool."""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def live_accounts():
    with patch("robothor.auth.accounts.get_connection", _fresh_connection):
        yield


@pytest.fixture
def alice(live_accounts):
    """A throwaway active account in the default tenant, removed afterwards."""
    from robothor.constants import DEFAULT_TENANT

    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (DEFAULT_TENANT, "Default"),
    )
    cur.execute(
        "INSERT INTO user_accounts (tenant_id, email, display_name, role, status) "
        "VALUES (%s, %s, %s, 'member', 'active') RETURNING id",
        (DEFAULT_TENANT, f"alice-{uuid.uuid4().hex[:8]}@example.com", "Alice"),
    )
    user_id = str(cur.fetchone()[0])
    conn.commit()
    try:
        yield user_id
    finally:
        cur.execute("DELETE FROM user_accounts WHERE id = %s", (user_id,))
        conn.commit()
        conn.close()


def _session(user_id: str) -> str:
    raw, h = tokens.new_refresh_token()
    accounts.create_session(user_id, h, ttl_seconds=3600)
    return raw


def _rotate(raw: str) -> dict | None:
    _, successor_hash = tokens.successor_refresh_token(raw)
    return accounts.rotate_session(
        tokens.hash_refresh_token(raw),
        successor_hash,
        ttl_seconds=3600,
        grace_seconds=tokens.REFRESH_REUSE_GRACE_SECONDS,
    )


def _age_rotation(raw: str, seconds: int) -> None:
    """Move the rotation ``seconds`` into the past — the database's fake clock."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE user_sessions SET rotated_at = rotated_at - make_interval(secs => %s) "
        "WHERE refresh_token_hash = %s",
        (seconds, tokens.hash_refresh_token(raw)),
    )
    conn.commit()
    conn.close()


def _live_sessions(user_id: str) -> int:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM user_sessions WHERE user_id = %s AND revoked_at IS NULL",
        (user_id,),
    )
    n = int(cur.fetchone()[0])
    conn.close()
    return n


@pytest.mark.integration
def test_a_normal_refresh_rotates_to_the_derived_successor(alice):
    raw = _session(alice)
    first = _rotate(raw)
    assert first is not None
    assert str(first["user_id"]) == alice
    assert first["rotated_at"] is not None
    successor_raw, successor_hash = tokens.successor_refresh_token(raw)
    live = accounts.get_active_session(successor_hash)
    assert live is not None and str(live["user_id"]) == alice
    # The old token is spent; exactly one live session remains.
    assert accounts.get_active_session(tokens.hash_refresh_token(raw)) is None
    assert _live_sessions(alice) == 1
    # The successor is an ordinary session: it rotates in turn.
    assert _rotate(successor_raw) is not None


@pytest.mark.integration
def test_the_same_token_twice_within_the_window_answers_the_same_rotation(alice):
    raw = _session(alice)
    first = _rotate(raw)
    second = _rotate(raw)
    assert first is not None and second is not None
    assert second["user_id"] == first["user_id"]
    assert second["rotated_at"] == first["rotated_at"]
    # No second session was minted for the replay.
    assert _live_sessions(alice) == 1


@pytest.mark.integration
def test_the_same_token_after_the_window_is_refused(alice):
    raw = _session(alice)
    assert _rotate(raw) is not None
    _age_rotation(raw, tokens.REFRESH_REUSE_GRACE_SECONDS + 1)
    assert _rotate(raw) is None


@pytest.mark.integration
def test_a_signed_out_token_is_refused_inside_the_window(alice):
    raw = _session(alice)
    assert accounts.revoke_session(tokens.hash_refresh_token(raw)) is True
    assert _rotate(raw) is None


@pytest.mark.integration
def test_a_replay_is_refused_once_the_successor_has_signed_out(alice):
    raw = _session(alice)
    assert _rotate(raw) is not None
    _, successor_hash = tokens.successor_refresh_token(raw)
    assert accounts.revoke_session(successor_hash) is True
    assert _rotate(raw) is None


@pytest.mark.integration
def test_a_token_cannot_claim_another_sessions_successor(alice):
    """Rotation A's successor is bound to A's token; B's token cannot reach it."""
    raw_a = _session(alice)
    raw_b = _session(alice)
    assert _rotate(raw_a) is not None
    assert accounts.revoke_session(tokens.hash_refresh_token(raw_b)) is True
    _, successor_a = tokens.successor_refresh_token(raw_a)
    claimed = accounts.rotate_session(
        tokens.hash_refresh_token(raw_b),
        successor_a,
        ttl_seconds=3600,
        grace_seconds=tokens.REFRESH_REUSE_GRACE_SECONDS,
    )
    assert claimed is None


@pytest.mark.integration
def test_two_pods_refreshing_at_once_both_get_the_rotation(alice):
    raw = _session(alice)
    barrier = threading.Barrier(2)
    results: list[dict | None] = []
    errors: list[BaseException] = []

    def pod() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(_rotate(raw))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=pod) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert len(results) == 2 and all(r is not None for r in results), results
    assert results[0]["rotated_at"] == results[1]["rotated_at"]
    assert _live_sessions(alice) == 1


@pytest.mark.integration
def test_a_disabled_account_gets_no_successor(alice):
    raw = _session(alice)
    conn = _connect()
    cur = conn.cursor()
    cur.execute("UPDATE user_accounts SET status = 'disabled' WHERE id = %s", (alice,))
    conn.commit()
    conn.close()
    # Refused outright: the route must not hand out a successor it never stored.
    assert _rotate(raw) is None
    _, successor_hash = tokens.successor_refresh_token(raw)
    assert accounts.get_active_session(successor_hash) is None
    assert _live_sessions(alice) == 0
    # And the token is spent all the same; re-enabling the account does not revive it.
    conn = _connect()
    cur = conn.cursor()
    cur.execute("UPDATE user_accounts SET status = 'active' WHERE id = %s", (alice,))
    conn.commit()
    conn.close()
    assert _rotate(raw) is None


@pytest.mark.integration
def test_the_window_runs_from_the_first_spend(alice):
    raw = _session(alice)
    first = _rotate(raw)
    _age_rotation(raw, 20)
    replay = _rotate(raw)
    assert replay is not None and replay["user_id"] == first["user_id"]
    # A replay does not restart the window: 31 s after the first spend, refused.
    _age_rotation(raw, 11)
    assert _rotate(raw) is None
