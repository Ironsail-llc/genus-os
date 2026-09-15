"""The guardrail change log against the real ``feature_flag_audit`` table.

The unit lane fakes the connection, which proves the payload and the gate and
proves nothing about the SQL. Column names drift (``name``/``from_value``/
``to_value``/``at`` are not what the API calls them), so the query is executed
here against the table migration 084 actually created.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

pytestmark = pytest.mark.integration

AUDIT = "/api/controls/audit"


@pytest.fixture
def live_db(monkeypatch, db_conn):
    from routers import flag_audit

    @contextmanager
    def _conn():
        yield db_conn

    monkeypatch.setattr(flag_audit, "get_connection", _conn)


@pytest.fixture
def changes(db_conn):
    """Two changes to one throwaway flag name, removed afterwards."""
    flag = f"ROBOTHOR_B14A_{uuid.uuid4().hex[:8].upper()}"
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO feature_flag_audit (name, from_value, to_value, actor, reason) "
            "VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s) RETURNING id",
            (
                flag,
                None,
                "observe",
                "operator:alice",
                "first",
                flag,
                "observe",
                "enforce",
                "operator:bob",
                "soak clean",
            ),
        )
        ids = sorted(int(row[0]) for row in cur.fetchall())
    db_conn.commit()
    yield flag, ids
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM feature_flag_audit WHERE name = %s", (flag,))
    db_conn.commit()


def test_the_query_runs_and_reads_newest_first(controls_client_as_operator, live_db, changes):
    flag, ids = changes
    body = controls_client_as_operator.get(f"{AUDIT}?flag={flag}").json()
    assert [c["id"] for c in body["changes"]] == sorted(ids, reverse=True)
    assert body["changes"][0]["old_value"] == "observe"
    assert body["changes"][0]["new_value"] == "enforce"
    assert body["changes"][0]["changed_by"] == "operator:bob"
    assert body["changes"][0]["reason"] == "soak clean"
    assert body["changes"][-1]["old_value"] is None
    assert body["changes"][0]["changed_at"].startswith("20")


def test_the_cursor_pages_backwards_through_the_log(controls_client_as_operator, live_db, changes):
    flag, ids = changes
    first = controls_client_as_operator.get(f"{AUDIT}?flag={flag}&limit=1").json()
    assert first["next_cursor"] == str(max(ids))
    second = controls_client_as_operator.get(
        f"{AUDIT}?flag={flag}&limit=1&cursor={first['next_cursor']}"
    ).json()
    assert [c["id"] for c in second["changes"]] == [min(ids)]


def test_an_unknown_flag_is_an_empty_page_not_an_error(controls_client_as_operator, live_db):
    body = controls_client_as_operator.get(f"{AUDIT}?flag=ROBOTHOR_NO_SUCH_FLAG").json()
    assert body == {"changes": [], "next_cursor": None}
