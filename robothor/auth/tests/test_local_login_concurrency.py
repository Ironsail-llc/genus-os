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


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Reproduces the lost update the CTE form suffers. Timing-dependent by "
        "nature, and a database that happens to serialise these eight "
        "transactions would not lose one — that is a pass for the product, so "
        "it must not be a failure for the suite. It is here to show the race is "
        "real, not to gate on it; the shipped statement is gated above."
    ),
)
def test_the_cte_form_that_shipped_first_loses_updates(counter_table) -> None:
    """The bug this pins, reproduced."""
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


# ── TOTP single-use, under concurrency ───────────────────────────────


@pytest.fixture
def mfa_table():
    """A throwaway table with the columns the TOTP watermark touches."""
    name = f"__mfa_race_{uuid.uuid4().hex[:8]}"
    conn = _connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE {name} (
            id                 UUID PRIMARY KEY,
            mfa_last_used_step BIGINT,
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


def test_only_one_of_two_simultaneous_uses_of_a_code_wins(mfa_table) -> None:
    """The watermark used to be check-then-write across two connections: both
    requests read "no step used", both verified the code, both signed in. A
    one-time password that works twice at once is not one-time."""
    from robothor.auth.local_login import BURN_STEP_SQL

    table, row_id = mfa_table
    step = 59_640_000
    barrier = threading.Barrier(2)
    wins: list[int] = []

    def one() -> None:
        conn = psycopg2.connect(PG_DSN)
        try:
            cur = conn.cursor()
            barrier.wait(timeout=10)
            cur.execute(BURN_STEP_SQL.format(table=table), (step, row_id, step))
            wins.append(cur.rowcount)
            conn.commit()
        finally:
            conn.close()

    threads = [threading.Thread(target=one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(wins) == [0, 1], (
        f"both concurrent uses of one code were accepted (rowcounts {wins}); "
        "the burn must be conditional in the same statement"
    )


def test_the_same_step_is_refused_the_second_time(mfa_table) -> None:
    from robothor.auth.local_login import BURN_STEP_SQL

    table, row_id = mfa_table
    conn = _connect()
    conn.autocommit = True
    cur = conn.cursor()
    sql = BURN_STEP_SQL.format(table=table)
    cur.execute(sql, (100, row_id, 100))
    assert cur.rowcount == 1
    cur.execute(sql, (100, row_id, 100))
    assert cur.rowcount == 0, "a replay of the same step was accepted"
    cur.execute(sql, (99, row_id, 99))
    assert cur.rowcount == 0, "an older step walked the watermark backwards"
    cur.execute(sql, (101, row_id, 101))
    assert cur.rowcount == 1, "a fresh step was refused"
    conn.close()


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


# ── Python and PostgreSQL must agree on what "the same address" means ──


def test_python_and_postgres_fold_case_identically(mfa_table) -> None:
    """`canonical_email` is `lower()`, and migration 114 lower-cases with SQL
    `lower()`. If the two ever disagreed, a row written by one would be
    invisible to the other — and the owner account is the one whose lockout has
    no recovery path. `Straße@x.de` is the case that separates `lower()` from
    Python's `casefold()`, which would have produced `strasse@x.de`: a
    different mailbox."""
    from robothor.auth.accounts import canonical_email

    conn = _connect()
    cur = conn.cursor()
    for address in ("Straße@X.DE", "ALICE@Example.COM", "Ünïcode@Exämple.test"):
        cur.execute("SELECT lower(%s)", (address,))
        (postgres,) = cur.fetchone()
        assert canonical_email(address) == postgres, (
            f"Python and PostgreSQL disagree on {address!r}: "
            f"{canonical_email(address)!r} vs {postgres!r}"
        )
    # And the trap: casefold would have folded these together.
    cur.execute("SELECT lower(%s) = lower(%s)", ("Straße@x.de", "Strasse@x.de"))
    (same,) = cur.fetchone()
    assert same is False
    assert canonical_email("Straße@x.de") != canonical_email("Strasse@x.de")
    conn.close()


def test_citext_agrees_with_canonical_email(mfa_table) -> None:
    """The stored column is CITEXT, so its own comparison must also match."""
    from robothor.auth.accounts import canonical_email

    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT %s::citext = %s::citext", ("Straße@x.de", canonical_email("Straße@X.DE")))
    (matched,) = cur.fetchone()
    assert matched is True
    cur.execute("SELECT %s::citext = %s::citext", ("Straße@x.de", "Strasse@x.de"))
    (collided,) = cur.fetchone()
    assert collided is False, "CITEXT must not conflate ß with ss either"
    conn.close()
