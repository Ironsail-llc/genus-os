"""The lockout counter must survive concurrent failures, against real Postgres.

A mock cannot see this bug. ``_record_failure`` used to read the current count
through a CTE:

    WITH next AS (SELECT failed_login_count + 1 AS n FROM user_accounts WHERE id = ...)
    UPDATE user_accounts SET failed_login_count = (SELECT n FROM next) ...

A subquery is evaluated against the statement's own snapshot. When the UPDATE
then blocks on a concurrent writer's row lock, PostgreSQL re-runs the *scan*
under EvalPlanQual against the newly committed row — but the already-computed
CTE output is not recomputed. So N overlapping failures all write the same
value, and a password-spraying attacker can hold the counter at 1 forever
while the lockout never fires. A plain column reference
(``failed_login_count + 1``) IS re-evaluated, which is the fix.

Skipped when no test database is reachable.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

pytestmark = pytest.mark.integration

# Same convention as tests/conftest_integration.py and crm/bridge/tests/test_audit.py
# — a *_test database, never a production name.
PG_DSN = os.environ.get(
    "ROBOTHOR_TEST_DB_DSN",
    "dbname=robothor_test user=robothor host=/var/run/postgresql",
)

WRITERS = 8


def _connect():
    try:
        return psycopg2.connect(PG_DSN)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"no test database reachable at ROBOTHOR_TEST_DB_DSN: {exc}")


@pytest.fixture
def counter_table():
    """A throwaway table with the same columns the real statement touches.

    Deliberately NOT user_accounts: this test hammers a row from eight threads
    and must not be able to touch a real account even in the test database.
    """
    name = f"__lockout_race_{uuid.uuid4().hex[:8]}"
    conn = _connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE {name} (
            id                 UUID PRIMARY KEY,
            failed_login_count INTEGER NOT NULL DEFAULT 0,
            locked_until       TIMESTAMPTZ,
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    row_id = str(uuid.uuid4())
    cur.execute(f"INSERT INTO {name} (id) VALUES (%s)", (row_id,))
    try:
        yield name, row_id
    finally:
        cur.execute(f"DROP TABLE IF EXISTS {name}")
        conn.close()


def _run_concurrently(sql: str, row_id: str, writers: int = WRITERS) -> None:
    """Fire *writers* copies of ``sql`` from separate connections at once."""
    from robothor.auth.local_login import LOCKOUT_SECONDS, LOCKOUT_THRESHOLD

    barrier = threading.Barrier(writers)
    errors: list[BaseException] = []

    def one() -> None:
        conn = psycopg2.connect(PG_DSN)
        try:
            cur = conn.cursor()
            barrier.wait(timeout=10)
            cur.execute(sql, (LOCKOUT_THRESHOLD, LOCKOUT_SECONDS, row_id))
            cur.fetchone()
            conn.commit()
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=one) for _ in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors


def test_the_shipped_statement_counts_every_concurrent_failure(counter_table) -> None:
    from robothor.auth.local_login import FAILURE_UPDATE_SQL

    table, row_id = counter_table
    _run_concurrently(FAILURE_UPDATE_SQL.format(table=table), row_id)

    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"SELECT failed_login_count, locked_until FROM {table} WHERE id = %s", (row_id,))
    count, locked_until = cur.fetchone()
    conn.close()

    assert count == WRITERS, (
        f"{WRITERS} concurrent failures produced a counter of {count}. A lost update here "
        "means the lockout never fires against a parallel password spray."
    )


def test_the_cte_form_that_shipped_first_loses_updates(counter_table) -> None:
    """The bug this pins, reproduced. If Postgres ever stops losing updates
    here the guard is unnecessary — but it does, and this says so out loud."""
    table, row_id = counter_table
    cte_sql = f"""
        WITH next AS (
            SELECT CASE
                       WHEN locked_until IS NOT NULL AND locked_until <= NOW() THEN 1
                       ELSE failed_login_count + 1
                   END AS n
            FROM {table} WHERE id = %s
        )
        UPDATE {table} SET
            failed_login_count = (SELECT n FROM next),
            locked_until = CASE
                WHEN (SELECT n FROM next) >= %s THEN NOW() + make_interval(secs => %s)
                ELSE NULL
            END,
            updated_at = NOW()
        WHERE id = %s
        RETURNING failed_login_count
    """

    from robothor.auth.local_login import LOCKOUT_SECONDS, LOCKOUT_THRESHOLD

    barrier = threading.Barrier(WRITERS)

    def one() -> None:
        conn = psycopg2.connect(PG_DSN)
        try:
            cur = conn.cursor()
            barrier.wait(timeout=10)
            cur.execute(cte_sql, (row_id, LOCKOUT_THRESHOLD, LOCKOUT_SECONDS, row_id))
            cur.fetchone()
            conn.commit()
        finally:
            conn.close()

    threads = [threading.Thread(target=one) for _ in range(WRITERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"SELECT failed_login_count FROM {table} WHERE id = %s", (row_id,))
    (count,) = cur.fetchone()
    conn.close()
    assert count < WRITERS, (
        "the CTE form did NOT lose an update in this run; the race is timing-dependent, "
        "but the shipped statement must not depend on winning it"
    )


def test_the_lock_engages_at_the_threshold_under_concurrency(counter_table) -> None:
    from robothor.auth.local_login import FAILURE_UPDATE_SQL, LOCKOUT_THRESHOLD

    table, row_id = counter_table
    _run_concurrently(FAILURE_UPDATE_SQL.format(table=table), row_id, writers=LOCKOUT_THRESHOLD)

    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"SELECT failed_login_count, locked_until FROM {table} WHERE id = %s", (row_id,))
    count, locked_until = cur.fetchone()
    conn.close()
    assert count == LOCKOUT_THRESHOLD
    assert locked_until is not None, "the threshold was reached but no lock was set"
