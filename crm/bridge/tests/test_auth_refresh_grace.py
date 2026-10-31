"""``POST /api/auth/refresh`` when two dashboard replicas refresh at once.

Each replica de-duplicates its own concurrent refreshes, but nothing ties a
browser to one replica, so two of them can present the same refresh token a
few milliseconds apart. Refresh tokens are single-use; before the grace
window, the second one got a 401 and the operator's next request failed.

The rotation store is faked here with a clock the test moves, so the route's
contract is pinned in the unit lane: within the window the second caller gets
the SAME pair as the first, after it a 401, and a token that was signed out
(rather than rotated) is refused at once. The SQL that enforces the same rules
across real connections is in ``robothor/auth/tests/test_refresh_rotation.py``.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REFRESH = "/api/auth/refresh"

ALICE = {
    "id": "uid-alice",
    "email": "alice@example.com",
    "display_name": "Alice",
    "role": "member",
    "tenant_id": "default",
    "status": "active",
}
BOB = {**ALICE, "id": "uid-bob", "email": "bob@example.com", "display_name": "Bob"}


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    monkeypatch.delenv("GENUS_AUTH_ENFORCE", raising=False)
    from robothor.auth import tokens

    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


class FakeClock:
    def __init__(self) -> None:
        # Real time, so the access tokens minted from it still verify.
        self.now = datetime.now(UTC)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeSessions:
    """``user_sessions`` by refresh-token hash, with the rotation columns.

    ``db_ahead`` is how far the database's clock runs ahead of the pod that
    checks the token: ``rotated_at`` is the database's ``NOW()``.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.db_ahead = timedelta(0)
        self.rows: dict[str, dict] = {}
        self.calls: list[dict] = []

    def add(self, user_id: str, raw: str) -> None:
        from robothor.auth import tokens

        self.rows[tokens.hash_refresh_token(raw)] = {
            "user_id": user_id,
            "revoked_at": None,
            "rotated_at": None,
            "rotated_to": None,
        }

    def sign_out(self, raw: str) -> None:
        from robothor.auth import tokens

        self.rows[tokens.hash_refresh_token(raw)]["revoked_at"] = self.clock.now

    def live(self) -> int:
        return sum(1 for row in self.rows.values() if row["revoked_at"] is None)

    def rotate_session(self, h, successor_hash, *, ttl_seconds, grace_seconds, **_):
        self.calls.append({"successor_hash": successor_hash, "grace_seconds": grace_seconds})
        row = self.rows.get(h)
        if row is None:
            return None
        now = self.clock.now + self.db_ahead
        if row["revoked_at"] is None:
            row["revoked_at"] = row["rotated_at"] = now
            row["rotated_to"] = successor_hash
            self.rows[successor_hash] = {
                "user_id": row["user_id"],
                "revoked_at": None,
                "rotated_at": None,
                "rotated_to": None,
            }
            return {"user_id": row["user_id"], "rotated_at": now, "replayed": False}
        successor = self.rows.get(row["rotated_to"] or "")
        if (
            row["rotated_at"] is not None
            and now - row["rotated_at"] < timedelta(seconds=grace_seconds)
            and row["rotated_to"] == successor_hash
            and successor is not None
            and successor["revoked_at"] is None
            and successor["user_id"] == row["user_id"]
        ):
            return {"user_id": row["user_id"], "rotated_at": row["rotated_at"], "replayed": True}
        return None


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sessions(clock):
    store = FakeSessions(clock)
    accounts_by_id = {ALICE["id"]: ALICE, BOB["id"]: BOB}
    with (
        patch("routers.auth.accounts.rotate_session", store.rotate_session),
        patch("routers.auth.accounts.get_account_by_id", accounts_by_id.get),
        patch("routers.auth.accounts.create_session") as create_session,
    ):
        store.create_session = create_session
        yield store


async def _refresh(client, raw: str):
    return await client.post(REFRESH, json={"refresh_token": raw})


@pytest.mark.asyncio
async def test_a_single_refresh_rotates_as_before(test_client, sessions):
    from robothor.auth import tokens

    sessions.add(ALICE["id"], "alice-refresh-token")
    r = await _refresh(test_client, "alice-refresh-token")
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"]
    assert body["refresh_token"] and body["refresh_token"] != "alice-refresh-token"
    assert body["user"]["id"] == ALICE["id"]
    claims = tokens.decode_token(body["access_token"])
    assert claims["sub"] == ALICE["id"]
    # The new refresh token is the one whose hash the store recorded.
    assert sessions.calls[0]["successor_hash"] == tokens.hash_refresh_token(body["refresh_token"])
    assert sessions.calls[0]["grace_seconds"] == tokens.REFRESH_REUSE_GRACE_SECONDS
    # Rotation mints its successor inside the store, never as a second session.
    sessions.create_session.assert_not_called()
    # The successor refreshes in turn.
    assert (await _refresh(test_client, body["refresh_token"])).status_code == 200


@pytest.mark.asyncio
async def test_the_same_token_within_the_window_returns_the_same_pair(test_client, sessions, clock):
    sessions.add(ALICE["id"], "alice-refresh-token")
    first = await _refresh(test_client, "alice-refresh-token")
    clock.advance(1)
    second = await _refresh(test_client, "alice-refresh-token")
    assert first.status_code == 200
    assert second.status_code == 200, second.json()
    assert second.json() == first.json()
    assert sessions.live() == 1


@pytest.mark.asyncio
async def test_the_same_token_after_the_window_is_refused(test_client, sessions, clock):
    from robothor.auth import tokens

    sessions.add(ALICE["id"], "alice-refresh-token")
    assert (await _refresh(test_client, "alice-refresh-token")).status_code == 200
    clock.advance(tokens.REFRESH_REUSE_GRACE_SECONDS + 1)
    late = await _refresh(test_client, "alice-refresh-token")
    assert late.status_code == 401
    assert late.json() == {"error": "invalid or expired refresh token"}


@pytest.mark.asyncio
async def test_a_signed_out_token_is_refused_even_inside_the_window(test_client, sessions):
    sessions.add(BOB["id"], "bob-refresh-token")
    sessions.sign_out("bob-refresh-token")
    r = await _refresh(test_client, "bob-refresh-token")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_another_sessions_replay_gets_its_own_pair_never_this_one(test_client, sessions):
    sessions.add(ALICE["id"], "alice-refresh-token")
    sessions.add(BOB["id"], "bob-refresh-token")
    alice = await _refresh(test_client, "alice-refresh-token")
    bob = await _refresh(test_client, "bob-refresh-token")
    bob_again = await _refresh(test_client, "bob-refresh-token")
    assert bob_again.json() == bob.json()
    assert bob.json()["refresh_token"] != alice.json()["refresh_token"]
    assert bob.json()["user"]["id"] == BOB["id"]


@pytest.mark.asyncio
async def test_an_unknown_token_is_refused(test_client, sessions):
    r = await _refresh(test_client, "never-issued")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_the_window_runs_from_the_first_spend_not_the_last_replay(
    test_client, sessions, clock
):
    sessions.add(ALICE["id"], "alice-refresh-token")
    first = await _refresh(test_client, "alice-refresh-token")
    clock.advance(20)
    replay = await _refresh(test_client, "alice-refresh-token")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    clock.advance(11)  # 31 s after the first spend, 11 s after the replay
    assert (await _refresh(test_client, "alice-refresh-token")).status_code == 401


@pytest.mark.asyncio
async def test_a_database_clock_ahead_of_the_bridge_still_yields_a_valid_token(
    test_client, sessions
):
    """``iat`` comes from the database clock and is checked on the pod's.

    A database a second ahead used to mint a token that PyJWT rejected as
    "not yet valid" the moment it was issued. (A second rather than half a
    second: ``iat`` is whole seconds, and only a full second ahead makes the
    old behaviour fail on every run instead of half of them.)
    """
    from robothor.auth import tokens

    sessions.db_ahead = timedelta(seconds=1)
    sessions.add(ALICE["id"], "alice-refresh-token")
    r = await _refresh(test_client, "alice-refresh-token")
    assert r.status_code == 200
    claims = tokens.decode_token(r.json()["access_token"])
    assert claims["sub"] == ALICE["id"]
    # Backdated by the skew allowance; the lifetime still ends TTL after rotation.
    assert claims["exp"] - claims["iat"] == (
        tokens.ACCESS_TTL_SECONDS + tokens.ROTATION_CLOCK_SKEW_SECONDS
    )
