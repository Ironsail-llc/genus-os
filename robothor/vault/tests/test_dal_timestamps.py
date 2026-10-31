"""When a secret was last written, read without decrypting it.

The provider listing's ``updated_at`` column is the only thing that tells an
operator whether the key on screen is the one they saved this morning or one
from a year ago, and its caller used to swallow every exception into a blank
cell — so a query that had been broken since it was written would have looked
exactly like a vault that was never initialised.

A fake connection rather than a live database: the unit suite has no Postgres,
and what is worth pinning here is the SQL's shape and the result mapping, both
of which a stub proves and neither of which a live row would prove better.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from robothor.vault.dal import get_secrets_updated_at

WRITTEN = datetime(2026, 9, 11, 14, 30, tzinfo=UTC)


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConn:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.cur = FakeCursor(rows)
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cur

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_conn(monkeypatch):
    holder: dict[str, FakeConn] = {}

    def _install(rows: list[tuple[Any, ...]]) -> FakeConn:
        conn = FakeConn(rows)
        holder["conn"] = conn
        monkeypatch.setattr("robothor.vault.dal._get_conn", lambda: conn)
        return conn

    return _install


def test_a_row_comes_back_keyed_by_its_vault_key(fake_conn) -> None:
    fake_conn([("providers/openrouter/api_key", WRITTEN)])
    assert get_secrets_updated_at(["providers/openrouter/api_key"]) == {
        "providers/openrouter/api_key": WRITTEN
    }


def test_it_asks_for_every_key_in_one_statement(fake_conn) -> None:
    """One connection for the whole listing, not one per slot."""
    conn = fake_conn([])
    keys = ["providers/openrouter/api_key", "providers/anthropic/api_key"]
    get_secrets_updated_at(keys)

    assert len(conn.cur.executed) == 1
    sql, params = conn.cur.executed[0]
    assert "= ANY(" in sql
    assert params[1] == keys


def test_a_key_with_no_row_is_simply_absent(fake_conn) -> None:
    fake_conn([("providers/openrouter/api_key", WRITTEN)])
    result = get_secrets_updated_at(["providers/openrouter/api_key", "providers/openai/api_key"])
    assert "providers/openai/api_key" not in result


def test_a_null_timestamp_is_dropped_rather_than_reported_as_none(fake_conn) -> None:
    """A None would render as "never written", which is a different claim."""
    fake_conn([("providers/openrouter/api_key", None)])
    assert get_secrets_updated_at(["providers/openrouter/api_key"]) == {}


def test_no_keys_means_no_connection(monkeypatch) -> None:
    def _boom():
        raise AssertionError("an empty request must not open a connection")

    monkeypatch.setattr("robothor.vault.dal._get_conn", _boom)
    assert get_secrets_updated_at([]) == {}


def test_the_connection_is_closed_even_when_the_query_raises(fake_conn) -> None:
    conn = fake_conn([])

    def _explode(sql: str, params: tuple) -> None:
        raise RuntimeError("connection reset")

    conn.cur.execute = _explode  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        get_secrets_updated_at(["providers/openrouter/api_key"])
    assert conn.closed, "a leaked connection per failed listing exhausts the pool"


def test_it_never_selects_the_encrypted_value(fake_conn) -> None:
    """No master key is needed to answer, so none is asked for — a status page
    has no business holding plaintext to say "set three days ago"."""
    conn = fake_conn([])
    get_secrets_updated_at(["providers/openrouter/api_key"])
    sql, _ = conn.cur.executed[0]
    assert "encrypted_value" not in sql


def test_vault_set_forwards_metadata_to_the_dal(monkeypatch):
    import robothor.vault as vault

    seen: list[dict] = []
    monkeypatch.setattr(vault, "get_master_key", lambda: b"k" * 32)
    monkeypatch.setattr(vault, "set_secret", lambda key, value, mk, **kw: seen.append(kw))
    vault.set("providers/example/api_key", "v" * 20, metadata={"stored_by": "user-alice"})
    assert seen[0]["metadata"] == {"stored_by": "user-alice"}


def test_get_secret_metadata_reads_the_audit_fields_without_decrypting(fake_conn) -> None:
    from robothor.vault.dal import get_secret_metadata

    conn = fake_conn([({"stored_by": "user-alice", "via": "chat:webchat"},)])
    assert get_secret_metadata("providers/example/api_key") == {
        "stored_by": "user-alice",
        "via": "chat:webchat",
    }
    sql, _ = conn.cur.executed[0]
    assert "encrypted_value" not in sql


def test_get_secret_metadata_of_an_absent_row_is_none(fake_conn) -> None:
    from robothor.vault.dal import get_secret_metadata

    fake_conn([])
    assert get_secret_metadata("providers/example/api_key") is None
