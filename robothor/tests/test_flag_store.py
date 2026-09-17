"""The store must NEVER disable a guardrail because the DB blinked.

Resolution order is DB(operator row) -> env -> None. A DB that is *unreachable*
falls through to env; only an operator row that *says* a value overrides env. A
bug here disables every guardrail at once, silently.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from robothor.flags import store

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clear_cache():
    store.invalidate()
    yield
    store.invalidate()


@pytest.fixture
def flag_store_db(db_conn, monkeypatch):
    """Apply 084 to the disposable test DB, point store.get_connection at it, and
    expose seed/audit helpers. Relies on db_conn's rollback-on-teardown — no
    .commit(), no production writes."""
    from robothor.flags import store as _store

    with db_conn.cursor() as cur:
        cur.execute(
            (
                Path(__file__).resolve().parents[2] / "crm/migrations/084_feature_flags.sql"
            ).read_text()
        )
    # NB: no db_conn.commit() — the integration fixture rolls back on teardown.

    @contextmanager
    def _conn():
        yield db_conn

    monkeypatch.setattr(_store, "get_connection", _conn)

    class _Harness:
        def seed(self, name, value, by):
            with db_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO feature_flags (name,value,updated_by) VALUES (%s,%s,%s) "
                    "ON CONFLICT (name) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by",
                    (name, value, by),
                )

        def audit(self, name):
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT to_value, actor FROM feature_flag_audit WHERE name=%s ORDER BY at",
                    (name,),
                )
                return [{"to_value": r[0], "actor": r[1]} for r in cur.fetchall()]

    yield _Harness()

    # set_flag() writes through its own committed path, so the rollback this
    # fixture otherwise relies on does not reclaim it. A leaked
    # `updated_by='operator:alice'` row is treated by store._read_db as an
    # authoritative operator override — not a migration seed — so it silently
    # changes what resolve() returns for every later test in the session, and
    # test_feature_flags_modes starts asserting 'enforce' == 'observe'.
    #
    # CI never noticed because its unit lane has no postgres service: the store
    # cannot reach a database, falls through to env, and the tests pass. That
    # makes the outcome depend on the machine the suite runs on, which the
    # repo's own conftest calls "not a suite; a coincidence".
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM feature_flag_audit WHERE actor LIKE 'operator:%%'")
        cur.execute("DELETE FROM feature_flags WHERE updated_by LIKE 'operator:%%'")
    db_conn.commit()
    from robothor.flags import store as _s

    _s.invalidate()


def test_only_governed_flags_are_writable():
    with pytest.raises(ValueError):
        store.set_flag("ROBOTHOR_TELEGRAM_BOT_TOKEN", "x", actor="op", reason="r")


def test_env_wins_when_only_a_seed_row_exists(monkeypatch, flag_store_db):
    # seed row present (as migration leaves it), env also set -> env wins (cutover no-op)
    flag_store_db.seed("ROBOTHOR_RIP_7_MODE", "observe", by="migration-084")
    monkeypatch.setenv("ROBOTHOR_RIP_7_MODE", "enforce")
    assert store.resolve("ROBOTHOR_RIP_7_MODE") == "enforce"


def test_operator_row_wins_over_env(monkeypatch, flag_store_db):
    flag_store_db.seed("ROBOTHOR_RIP_7_MODE", "alert", by="operator:alice")
    monkeypatch.setenv("ROBOTHOR_RIP_7_MODE", "observe")
    assert store.resolve("ROBOTHOR_RIP_7_MODE") == "alert"


def test_db_unreachable_falls_through_to_env(monkeypatch):
    monkeypatch.setattr(store, "_read_db", lambda name: (_ for _ in ()).throw(OSError("db down")))
    monkeypatch.setenv("ROBOTHOR_RBAC_MODE", "enforce")
    # DB raising must NOT return None/off — it must fall through to env
    assert store.resolve("ROBOTHOR_RBAC_MODE") == "enforce"


def test_valid_values_for_a_value_set_flag_is_its_own_value_set():
    """A value-set flag is not a rollout ladder: its values are a setting's
    options. ``ROBOTHOR_CALENDAR_SEND_UPDATES`` names Google's `sendUpdates`
    audience, and the one posture it exists for — `none` — is not a rung.

    Registry-driven, not a name in an `if`: every surface that has to agree
    (the Controls API's 422, the value list the page renders, and
    ``scripts/flag_audit.py``'s effective value) reads the same mapping.
    """
    assert store.VALUE_SET_FLAGS["ROBOTHOR_CALENDAR_SEND_UPDATES"] == (
        "all",
        "externalOnly",
        "none",
    )
    assert store.valid_values_for("ROBOTHOR_CALENDAR_SEND_UPDATES") == (
        "all",
        "externalOnly",
        "none",
    )
    # The ladders are untouched by the registry.
    assert store.valid_values_for("ROBOTHOR_RBAC_MODE") == ("off", "observe", "alert", "enforce")


def test_normalise_keeps_the_case_of_a_value_set_value():
    """``externalOnly`` is camelCase and ``feature_flags.calendar_send_updates``
    compares it case-sensitively. Lower-casing it the way a mode ladder is
    lower-cased made the store hand back ``all`` — a page serving a value the
    operator did not choose, for the flag whose whole point is that a saved
    value is honoured."""
    assert store.normalise("ROBOTHOR_CALENDAR_SEND_UPDATES", "externalOnly") == "externalOnly"
    assert store.normalise("ROBOTHOR_CALENDAR_SEND_UPDATES", "  none  ") == "none"
    # Still clamps what the engine would not honour, to what the engine runs.
    assert store.normalise("ROBOTHOR_CALENDAR_SEND_UPDATES", "externalonly") == "all"
    assert store.normalise("ROBOTHOR_CALENDAR_SEND_UPDATES", None) == "all"


def test_set_flag_writes_audit_and_notifies(flag_store_db):
    store.set_flag("ROBOTHOR_RBAC_MODE", "enforce", actor="operator:alice", reason="promote")
    rows = flag_store_db.audit("ROBOTHOR_RBAC_MODE")
    assert rows and rows[-1]["to_value"] == "enforce"
    assert rows[-1]["actor"] == "operator:alice"
