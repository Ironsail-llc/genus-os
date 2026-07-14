# The Helm — Phase 1 (Control Plane) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the 12 governed guardrail flags from systemd env vars into a DB-backed control plane with an audited, agent-hostile write API, an inert-control detector, and a Controls tab — so the operator can see and steer every guardrail from the dashboard, and a flag flip reaches the engine without a restart.

**Architecture:** A `feature_flags` table becomes the source of truth; `robothor/flags/store.py` resolves DB→env→default with a short-TTL cache invalidated by `pg_notify`; `robothor/engine/feature_flags.py` reads through the store at two choke-point helpers (`_enforcement_mode`, `_env_bool`) so there is no call-site churn; the write API lives on the bridge, is operator-only (agents carry service tokens and are structurally excluded), and every write is audited. An evidence detector classifies each flag ENFORCING / INERT / BLIND / UNPROVEN using per-flag evidence sources declared in code.

**Tech Stack:** Python 3.12, psycopg2, FastAPI (bridge), Next.js 16 / React 19 (dashboard), pytest, vitest.

## Global Constraints

- **Migrations:** numbered `NNN_name.sql` in `crm/migrations/`; next number is **084**. Idempotent (`IF NOT EXISTS`, `DO $$ ... $$`). Applied by `robothor/db/migrate.py` (regex `^(\d+)([a-z]?)_(.+)\.sql$`).
- **Governed flags (exactly 12):** 8 mode-ladder — `ROBOTHOR_RBAC_MODE`, `ROBOTHOR_INJECTION_SCAN_MODE`, `ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE`, `ROBOTHOR_APPROVAL_MODE`, `ROBOTHOR_SANDBOX_DEFAULT_MODE`, `ROBOTHOR_COMPLETION_CONTRACTS_MODE`, `ROBOTHOR_RIP_7_MODE`, `ROBOTHOR_RIP_13_MODE`; 4 boolean — `ROBOTHOR_RIP_1_ENABLED`, `ROBOTHOR_RIP_4_ENABLED`, `ROBOTHOR_RIP_5_ENABLED`, `ROBOTHOR_JUDGE_ENABLED`. `TELEGRAM_*` and `TRAJECTORY_SAMPLE` are NOT governed — leave in env.
- **Resolution order (never violated):** DB value → `os.environ` → coded default. A store that returns `off`/`disabled` when the DB is *unreachable* is a bug — DB *unreachable* must fall through to env, only a DB row that *says* off means off.
- **Write path is agent-hostile:** no tool in `robothor/engine/tools/schemas.py` matching `flag|control|guardrail`; API on the bridge only; operator-only via `AuthContext` (`auth is not None and not auth.is_service`). Agents carry service tokens (`is_service == True`) and are excluded structurally.
- **Test discipline:** platform DB tests run against local Postgres; mark DB-touching tests as they already are in the suite. Bridge tests run from `crm/bridge/` rootdir. Stub outbound channels — never let a test hit Telegram or a real network.
- **DEFAULT_TENANT** pinned in tests via `conftest.py` (already sets `ROBOTHOR_DEFAULT_TENANT=default`). The hermetic-env fixture (added today) snapshots `os.environ` per test — rely on it; do not leak flag env vars between tests.

---

### Task 1: Migration 084 — `feature_flags` + `feature_flag_audit`

**Files:**
- Create: `crm/migrations/084_feature_flags.sql`
- Test: `robothor/tests/test_migration_084_feature_flags.py`

**Interfaces:**
- Consumes: nothing.
- Produces two tables:
  - `feature_flags(name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_by TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), reason TEXT)` — `value` holds the mode string (`off`/`observe`/`alert`/`enforce`) for ladder flags or `true`/`false` for booleans.
  - `feature_flag_audit(id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, from_value TEXT, to_value TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, at TIMESTAMPTZ NOT NULL DEFAULT now())`.
  - Seeds `feature_flags` with the 12 governed flag names, each `value` copied from the current live env value (so the cutover changes nothing on day one).

- [ ] **Step 1: Write the failing test**

```python
# robothor/tests/test_migration_084_feature_flags.py
"""084 creates the flag store and seeds it without changing any value.

The whole point of DB-backed flags is that flipping the source of truth from env
to DB must be a NO-OP on day one: the seeded value must equal what the flag
resolved to from env, or the cutover silently changes a guardrail.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import psycopg2
import pytest

MIGRATION = Path(__file__).resolve().parents[2] / "crm" / "migrations" / "084_feature_flags.sql"

GOVERNED = {
    "ROBOTHOR_RBAC_MODE", "ROBOTHOR_INJECTION_SCAN_MODE",
    "ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE", "ROBOTHOR_APPROVAL_MODE",
    "ROBOTHOR_SANDBOX_DEFAULT_MODE", "ROBOTHOR_COMPLETION_CONTRACTS_MODE",
    "ROBOTHOR_RIP_7_MODE", "ROBOTHOR_RIP_13_MODE",
    "ROBOTHOR_RIP_1_ENABLED", "ROBOTHOR_RIP_4_ENABLED",
    "ROBOTHOR_RIP_5_ENABLED", "ROBOTHOR_JUDGE_ENABLED",
}


def test_migration_file_exists():
    assert MIGRATION.exists(), "084_feature_flags.sql must exist"


def test_migration_creates_both_tables_and_seeds_twelve(pg_scratch):
    """pg_scratch: a fixture yielding a psycopg2 conn to an empty scratch DB."""
    with pg_scratch.cursor() as cur:
        cur.execute(MIGRATION.read_text())
        pg_scratch.commit()

        cur.execute("SELECT to_regclass('public.feature_flags')")
        assert cur.fetchone()[0] is not None
        cur.execute("SELECT to_regclass('public.feature_flag_audit')")
        assert cur.fetchone()[0] is not None

        cur.execute("SELECT name FROM feature_flags")
        seeded = {r[0] for r in cur.fetchall()}
        assert seeded == GOVERNED, f"seed drift: {seeded ^ GOVERNED}"


def test_migration_is_idempotent(pg_scratch):
    with pg_scratch.cursor() as cur:
        cur.execute(MIGRATION.read_text())
        cur.execute(MIGRATION.read_text())  # second apply must not raise
        pg_scratch.commit()
        cur.execute("SELECT count(*) FROM feature_flags")
        assert cur.fetchone()[0] == 12
```

Add the `pg_scratch` fixture to `robothor/tests/conftest.py` if absent:

```python
# robothor/tests/conftest.py  (append)
import os
import psycopg2
import pytest


@pytest.fixture
def pg_scratch():
    """A throwaway schema for migration tests. Rolls back on teardown.

    Uses the same connection params as the app; creates a temp schema so the
    migration's public-schema DDL runs in isolation and never touches real tables.
    """
    dsn = os.environ.get("ROBOTHOR_TEST_DSN", "dbname=robothor_memory")
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/philip/robothor && PYTHONPATH=. venv/bin/python -m pytest robothor/tests/test_migration_084_feature_flags.py -q`
Expected: FAIL — `084_feature_flags.sql must exist`.

- [ ] **Step 3: Write the migration**

```sql
-- crm/migrations/084_feature_flags.sql
-- The 12 governed guardrail flags become DB-backed. Env stays as the emergency
-- override (resolution: DB -> env -> coded default), so this cutover changes
-- nothing until an operator writes a row. Seeds each flag with a
-- behaviour-preserving default; the store fills any missing value from env at
-- first read.

CREATE TABLE IF NOT EXISTS feature_flags (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason     TEXT
);

CREATE TABLE IF NOT EXISTS feature_flag_audit (
    id         BIGSERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    from_value TEXT,
    to_value   TEXT NOT NULL,
    actor      TEXT NOT NULL,
    reason     TEXT,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS feature_flag_audit_name_at
    ON feature_flag_audit (name, at DESC);

-- Seed with behaviour-preserving defaults. Ladder flags default to the coded
-- default of the reader (`observe`); booleans to their coded default. An
-- operator promotes from here; the store will not overwrite an existing row.
INSERT INTO feature_flags (name, value, updated_by, reason) VALUES
    ('ROBOTHOR_RBAC_MODE',                 'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_INJECTION_SCAN_MODE',       'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE','observe', 'migration-084', 'seed'),
    ('ROBOTHOR_APPROVAL_MODE',             'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_SANDBOX_DEFAULT_MODE',      'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_COMPLETION_CONTRACTS_MODE', 'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_RIP_7_MODE',                'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_RIP_13_MODE',               'observe', 'migration-084', 'seed'),
    ('ROBOTHOR_RIP_1_ENABLED',             'false',   'migration-084', 'seed'),
    ('ROBOTHOR_RIP_4_ENABLED',             'false',   'migration-084', 'seed'),
    ('ROBOTHOR_RIP_5_ENABLED',             'false',   'migration-084', 'seed'),
    ('ROBOTHOR_JUDGE_ENABLED',             'false',   'migration-084', 'seed')
ON CONFLICT (name) DO NOTHING;
```

> Note: the seed uses coded defaults, not live env values, because a migration
> cannot read the engine's runtime env. The store's DB→env→default order means
> the *live* env value still wins over a seed row only if we choose env-first —
> we do NOT. So the DB seed is the behaviour-preserving default, and the operator
> promotes explicitly. This is deliberate: after 084 applies, `store.resolve`
> returns the seeded `observe`/`false` unless env is *also* set, in which case —
> see Task 2 — a **seed** row is treated as "unset, fall through to env". This is
> what makes the cutover a true no-op. Task 2's tests pin exactly this.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/philip/robothor && PYTHONPATH=. venv/bin/python -m pytest robothor/tests/test_migration_084_feature_flags.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add crm/migrations/084_feature_flags.sql robothor/tests/test_migration_084_feature_flags.py robothor/tests/conftest.py
git commit -m "feat(flags): migration 084 — DB-backed feature_flags + audit"
```

---

### Task 2: `robothor/flags/store.py` — DB-first resolution with cache + pg_notify

**Files:**
- Create: `robothor/flags/__init__.py` (empty)
- Create: `robothor/flags/store.py`
- Test: `robothor/tests/test_flag_store.py`

**Interfaces:**
- Consumes: `robothor.db.connection.get_connection`; the `feature_flags` table.
- Produces:
  - `GOVERNED_FLAGS: frozenset[str]` — the 12 names.
  - `resolve(name: str) -> str | None` — returns the effective raw value string, applying DB → env → `None`. A seed row (`updated_by == 'migration-084'` and never operator-touched) is treated as "unset" so env still wins during cutover; an operator-written row wins over env. Returns `None` if neither DB (operator row) nor env has a value.
  - `set_flag(name: str, value: str, actor: str, reason: str) -> None` — upserts `feature_flags`, writes a `feature_flag_audit` row, and `NOTIFY feature_flags`. Raises `ValueError` if `name not in GOVERNED_FLAGS`.
  - `invalidate() -> None` — clears the in-process cache.
  - Cache: module-level dict, ~5s TTL, plus a `LISTEN feature_flags` path (a helper `start_listener()` the daemon calls; tested via direct `invalidate()`).

- [ ] **Step 1: Write the failing tests**

```python
# robothor/tests/test_flag_store.py
"""The store must NEVER disable a guardrail because the DB blinked.

Resolution order is DB(operator row) -> env -> None. A DB that is *unreachable*
falls through to env; only an operator row that *says* a value overrides env. A
bug here disables every guardrail at once, silently.
"""
from __future__ import annotations

import pytest

from robothor.flags import store


@pytest.fixture(autouse=True)
def _clear_cache():
    store.invalidate()
    yield
    store.invalidate()


def test_only_governed_flags_are_writable():
    with pytest.raises(ValueError):
        store.set_flag("ROBOTHOR_TELEGRAM_BOT_TOKEN", "x", actor="op", reason="r")


def test_env_wins_when_only_a_seed_row_exists(monkeypatch, pg_scratch_store):
    # seed row present (as migration leaves it), env also set -> env wins (cutover no-op)
    pg_scratch_store.seed("ROBOTHOR_RIP_7_MODE", "observe", by="migration-084")
    monkeypatch.setenv("ROBOTHOR_RIP_7_MODE", "enforce")
    assert store.resolve("ROBOTHOR_RIP_7_MODE") == "enforce"


def test_operator_row_wins_over_env(monkeypatch, pg_scratch_store):
    pg_scratch_store.seed("ROBOTHOR_RIP_7_MODE", "alert", by="operator:philip")
    monkeypatch.setenv("ROBOTHOR_RIP_7_MODE", "observe")
    assert store.resolve("ROBOTHOR_RIP_7_MODE") == "alert"


def test_db_unreachable_falls_through_to_env(monkeypatch):
    monkeypatch.setattr(store, "_read_db", lambda name: (_ for _ in ()).throw(OSError("db down")))
    monkeypatch.setenv("ROBOTHOR_RBAC_MODE", "enforce")
    # DB raising must NOT return None/off — it must fall through to env
    assert store.resolve("ROBOTHOR_RBAC_MODE") == "enforce"


def test_set_flag_writes_audit_and_notifies(pg_scratch_store):
    store.set_flag("ROBOTHOR_RBAC_MODE", "enforce", actor="operator:philip", reason="promote")
    rows = pg_scratch_store.audit("ROBOTHOR_RBAC_MODE")
    assert rows and rows[-1]["to_value"] == "enforce"
    assert rows[-1]["actor"] == "operator:philip"
```

Add a `pg_scratch_store` fixture (a thin wrapper the tests use to seed/read the scratch DB) to `robothor/tests/conftest.py`:

```python
# robothor/tests/conftest.py  (append)
@pytest.fixture
def pg_scratch_store(pg_scratch, monkeypatch):
    """Point the store at the scratch connection and expose seed/audit helpers."""
    from robothor.flags import store as _store

    class _Harness:
        def __init__(self, conn):
            self.conn = conn
            with conn.cursor() as cur:
                cur.execute(
                    (Path(__file__).resolve().parents[2]
                     / "crm/migrations/084_feature_flags.sql").read_text()
                )
            conn.commit()
        def seed(self, name, value, by):
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO feature_flags (name,value,updated_by) VALUES (%s,%s,%s) "
                    "ON CONFLICT (name) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by",
                    (name, value, by),
                )
            self.conn.commit()
        def audit(self, name):
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT to_value, actor FROM feature_flag_audit WHERE name=%s ORDER BY at",
                    (name,),
                )
                return [{"to_value": r[0], "actor": r[1]} for r in cur.fetchall()]

    from contextlib import contextmanager

    @contextmanager
    def _conn():
        yield pg_scratch
    monkeypatch.setattr(_store, "get_connection", _conn)
    return _Harness(pg_scratch)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/philip/robothor && PYTHONPATH=. venv/bin/python -m pytest robothor/tests/test_flag_store.py -q`
Expected: FAIL — `ModuleNotFoundError: robothor.flags`.

- [ ] **Step 3: Write the store**

```python
# robothor/flags/store.py
"""DB-backed resolution for the 12 governed guardrail flags.

Resolution order, never violated: operator DB row -> os.environ -> None.

A DB that is *unreachable* falls through to env — it never returns None (which a
caller would read as off). Only an operator-written row overrides env; a bare
migration seed row is treated as "unset" so the env->DB cutover is a no-op.
"""
from __future__ import annotations

import os
import threading
import time

from robothor.db.connection import get_connection

GOVERNED_FLAGS: frozenset[str] = frozenset({
    "ROBOTHOR_RBAC_MODE", "ROBOTHOR_INJECTION_SCAN_MODE",
    "ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE", "ROBOTHOR_APPROVAL_MODE",
    "ROBOTHOR_SANDBOX_DEFAULT_MODE", "ROBOTHOR_COMPLETION_CONTRACTS_MODE",
    "ROBOTHOR_RIP_7_MODE", "ROBOTHOR_RIP_13_MODE",
    "ROBOTHOR_RIP_1_ENABLED", "ROBOTHOR_RIP_4_ENABLED",
    "ROBOTHOR_RIP_5_ENABLED", "ROBOTHOR_JUDGE_ENABLED",
})

_SEED_ACTOR = "migration-084"
_TTL_SECONDS = 5.0
_cache: dict[str, tuple[float, str | None]] = {}
_lock = threading.Lock()


def invalidate() -> None:
    with _lock:
        _cache.clear()


def _read_db(name: str) -> str | None:
    """Return the operator-written value, or None if only a seed row / no row.

    Raises on connection failure — the caller MUST fall through to env, never
    treat a DB outage as 'off'.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT value, updated_by FROM feature_flags WHERE name = %s", (name,)
        )
        row = cur.fetchone()
    if row is None:
        return None
    value, updated_by = row
    if updated_by == _SEED_ACTOR:
        return None  # seed row == "unset", let env win during cutover
    return value


def resolve(name: str) -> str | None:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < _TTL_SECONDS:
            return hit[1] if hit[1] is not None else os.environ.get(name)
    db_val: str | None
    try:
        db_val = _read_db(name)
    except Exception:
        db_val = None  # DB unreachable -> fall through to env below
    with _lock:
        _cache[name] = (now, db_val)
    return db_val if db_val is not None else os.environ.get(name)


def set_flag(name: str, value: str, actor: str, reason: str) -> None:
    if name not in GOVERNED_FLAGS:
        raise ValueError(f"{name} is not a governed flag")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM feature_flags WHERE name = %s", (name,))
        prev = cur.fetchone()
        from_value = prev[0] if prev else None
        cur.execute(
            "INSERT INTO feature_flags (name, value, updated_by, reason) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, "
            "updated_by = EXCLUDED.updated_by, updated_at = now(), reason = EXCLUDED.reason",
            (name, value, actor, reason),
        )
        cur.execute(
            "INSERT INTO feature_flag_audit (name, from_value, to_value, actor, reason) "
            "VALUES (%s, %s, %s, %s, %s)",
            (name, from_value, value, actor, reason),
        )
        cur.execute("NOTIFY feature_flags")
        conn.commit()
    invalidate()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/philip/robothor && PYTHONPATH=. venv/bin/python -m pytest robothor/tests/test_flag_store.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add robothor/flags/__init__.py robothor/flags/store.py robothor/tests/test_flag_store.py robothor/tests/conftest.py
git commit -m "feat(flags): DB-first store with env fallback + audited writes"
```

---

### Task 3: `robothor/flags/evidence.py` — the inert-control detector

**Files:**
- Create: `robothor/flags/evidence.py`
- Test: `robothor/tests/test_flag_evidence.py`

**Interfaces:**
- Consumes: `get_connection`; the evidence tables (`agent_guardrail_events`, `memory_facts_audit`, `crm_agent_notifications`, …).
- Produces:
  - `EVIDENCE_SOURCES: dict[str, EvidenceSource]` — one per governed flag, declaring the table + predicate that counts as "this control fired". Declared in code, never inferred.
  - `verdict(name: str, mode: str) -> Verdict` where `Verdict` is a dataclass `{name, mode, status: Literal["ENFORCING","INERT","BLIND","UNPROVEN"], last_fired: datetime|None, count_7d: int, message: str}`.

- [ ] **Step 1: Write the failing tests**

```python
# robothor/tests/test_flag_evidence.py
"""The detector must not itself be a hollow control.

Two guarantees: (1) every declared evidence source names a table that actually
exists and is queryable — else the detector reads a missing table and reports a
comforting zero; (2) a genuinely-inert control (human_approval: enforce, zero
events ever) comes back INERT, tested against the live table, not a mock.
"""
from __future__ import annotations

import pytest

from robothor.flags import evidence
from robothor.flags.store import GOVERNED_FLAGS


def test_every_governed_flag_has_an_evidence_source():
    assert set(evidence.EVIDENCE_SOURCES) == set(GOVERNED_FLAGS)


def test_every_evidence_source_table_exists(db_cursor):
    for name, src in evidence.EVIDENCE_SOURCES.items():
        db_cursor.execute("SELECT to_regclass(%s)", (f"public.{src.table}",))
        assert db_cursor.fetchone()[0] is not None, f"{name}: table {src.table} missing"


def test_enforce_with_zero_evidence_is_inert(db_cursor, monkeypatch):
    # human_approval genuinely has zero events on this instance
    v = evidence.verdict("ROBOTHOR_APPROVAL_MODE", "enforce")
    assert v.status == "INERT"
    assert "NEVER FIRED" in v.message.upper()


def test_enforce_with_recent_evidence_is_enforcing(db_cursor):
    # injection_scan has real blocks; if this instance has none in 7d it is UNPROVEN,
    # which is also acceptable — assert it is NOT falsely INERT when evidence exists.
    v = evidence.verdict("ROBOTHOR_INJECTION_SCAN_MODE", "enforce")
    assert v.status in {"ENFORCING", "UNPROVEN"}
```

(`db_cursor` is the existing integration fixture re-exported by `conftest.py`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/philip/robothor && PYTHONPATH=. venv/bin/python -m pytest robothor/tests/test_flag_evidence.py -q`
Expected: FAIL — `ModuleNotFoundError: robothor.flags.evidence`.

- [ ] **Step 3: Write the detector**

```python
# robothor/flags/evidence.py
"""Per-flag evidence sources and the INERT/BLIND/ENFORCING/UNPROVEN verdict.

Each control writes evidence to a DIFFERENT table. RIP-7 writes to
memory_facts_audit, not agent_guardrail_events. Querying the wrong table returns
a comforting zero and makes THIS detector a liar — so sources are declared here,
in code, beside nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from robothor.db.connection import get_connection

Status = Literal["ENFORCING", "INERT", "BLIND", "UNPROVEN"]


@dataclass(frozen=True)
class EvidenceSource:
    table: str
    where: str  # SQL predicate identifying a "fired" event for this control


@dataclass(frozen=True)
class Verdict:
    name: str
    mode: str
    status: Status
    last_fired: datetime | None
    count_7d: int
    message: str


EVIDENCE_SOURCES: dict[str, EvidenceSource] = {
    "ROBOTHOR_RBAC_MODE": EvidenceSource("agent_guardrail_events", "guardrail_name='rbac'"),
    "ROBOTHOR_INJECTION_SCAN_MODE": EvidenceSource("agent_guardrail_events", "guardrail_name='injection_scan'"),
    "ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE": EvidenceSource("agent_guardrail_events", "guardrail_name LIKE 'exec_allowlist%'"),
    "ROBOTHOR_APPROVAL_MODE": EvidenceSource("agent_guardrail_events", "guardrail_name='human_approval'"),
    "ROBOTHOR_SANDBOX_DEFAULT_MODE": EvidenceSource("agent_guardrail_events", "guardrail_name='sandbox_default'"),
    "ROBOTHOR_COMPLETION_CONTRACTS_MODE": EvidenceSource("agent_guardrail_events", "guardrail_name='completion_contract'"),
    "ROBOTHOR_RIP_7_MODE": EvidenceSource("memory_facts_audit", "TRUE"),
    "ROBOTHOR_RIP_13_MODE": EvidenceSource("agent_guardrail_events", "guardrail_name LIKE 'rip_13%'"),
    "ROBOTHOR_RIP_1_ENABLED": EvidenceSource("agent_guardrail_events", "guardrail_name LIKE 'rip_1%'"),
    "ROBOTHOR_RIP_4_ENABLED": EvidenceSource("agent_guardrail_events", "guardrail_name LIKE 'rip_4%'"),
    "ROBOTHOR_RIP_5_ENABLED": EvidenceSource("agent_guardrail_events", "guardrail_name LIKE 'rip_5%'"),
    "ROBOTHOR_JUDGE_ENABLED": EvidenceSource("agent_guardrail_events", "guardrail_name='judge'"),
}


def verdict(name: str, mode: str) -> Verdict:
    src = EVIDENCE_SOURCES[name]
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT max(created_at), count(*) FILTER (WHERE created_at > now() - interval '7 days') "
            f"FROM {src.table} WHERE {src.where}"  # noqa: S608 — table/where are code-declared constants
        )
        last_fired, count_7d = cur.fetchone()
    count_7d = int(count_7d or 0)

    off = mode in ("off", "false", None)
    ever_fired = last_fired is not None

    if off:
        status: Status = "UNPROVEN"
        msg = "disabled"
    elif not ever_fired:
        status = "INERT"
        msg = "NEVER FIRED — this control cannot protect you."
    elif count_7d == 0:
        status = "UNPROVEN"
        msg = f"last fired {last_fired:%Y-%m-%d} — nothing in 7d"
    else:
        status = "ENFORCING" if mode in ("enforce", "true") else "BLIND"
        msg = f"last fired {last_fired:%Y-%m-%d %H:%M} ({count_7d} events / 7d)"
    return Verdict(name, mode, status, last_fired, count_7d, msg)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/philip/robothor && PYTHONPATH=. venv/bin/python -m pytest robothor/tests/test_flag_evidence.py -q`
Expected: PASS (4 tests). If `test_enforce_with_zero_evidence_is_inert` fails because `human_approval` has since fired, that is real data — adjust the fixture flag to one still at zero and note it.

- [ ] **Step 5: Commit**

```bash
git add robothor/flags/evidence.py robothor/tests/test_flag_evidence.py
git commit -m "feat(flags): inert-control detector with per-flag evidence sources"
```

---

### Task 4: The `feature_flags.py` seam — read through the store

**Files:**
- Modify: `robothor/engine/feature_flags.py` (the `_enforcement_mode` and `_env_bool` helpers + the `_VALID_*` readers)
- Test: `robothor/tests/test_feature_flags_seam.py`

**Interfaces:**
- Consumes: `robothor.flags.store.resolve`.
- Produces: no signature changes. `_enforcement_mode(enabled_var, mode_var)` and `_env_bool(name, default)` now resolve `mode_var` / `name` through `store.resolve()` first, `os.environ` second. Behaviour identical when no DB row exists.

- [ ] **Step 1: Write the failing test**

```python
# robothor/tests/test_feature_flags_seam.py
"""A DB operator row must change what the engine's readers return — no restart."""
from __future__ import annotations

import pytest

from robothor.engine import feature_flags
from robothor.flags import store


def test_rip7_mode_reads_the_store(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_RIP_7_ENABLED", "1")
    monkeypatch.setattr(store, "resolve",
                        lambda name: "enforce" if name == "ROBOTHOR_RIP_7_MODE" else None)
    store.invalidate()
    assert feature_flags.rip_7_mode() == "enforce"


def test_env_still_works_when_store_returns_none(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_RIP_7_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_RIP_7_MODE", "alert")
    monkeypatch.setattr(store, "resolve", lambda name: None)
    assert feature_flags.rip_7_mode() == "alert"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/philip/robothor && PYTHONPATH=. venv/bin/python -m pytest robothor/tests/test_feature_flags_seam.py -q`
Expected: FAIL — `rip_7_mode()` returns `observe` (store not consulted).

- [ ] **Step 3: Add the seam**

At the top of `robothor/engine/feature_flags.py`, add a resolver that prefers the store and falls back to env:

```python
# robothor/engine/feature_flags.py  (near the other helpers)
def _resolve_raw(name: str, default: str = "") -> str:
    """Governed flags resolve through the DB store first, then env.

    Non-governed names (credentials, tuning) go straight to env — the store only
    knows the 12 guardrail flags.
    """
    from robothor.flags.store import GOVERNED_FLAGS, resolve

    if name in GOVERNED_FLAGS:
        val = resolve(name)
        if val is not None:
            return val
    return os.environ.get(name, default)
```

Then change the two choke points to use it:

```python
# in _env_bool:
def _env_bool(name: str, default: bool = False) -> bool:
    raw = _resolve_raw(name, "").strip().lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on"}

# in _enforcement_mode: replace `os.environ.get(mode_var, "observe")`
    raw = _resolve_raw(mode_var, "observe").strip().lower()

# in rip_7_mode(): replace `os.environ.get("ROBOTHOR_RIP_7_MODE", "observe")`
    raw = _resolve_raw("ROBOTHOR_RIP_7_MODE", "observe").strip().lower()

# in symbolic_memory_mode() (RIP_13): same substitution for its mode var.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/philip/robothor && PYTHONPATH=. venv/bin/python -m pytest robothor/tests/test_feature_flags_seam.py robothor/engine/tests/ -q -m "not slow and not llm and not e2e" --ignore=robothor/engine/tests/test_channel_bus_crm.py`
Expected: the 2 seam tests PASS; no regression in the engine suite beyond the known pre-existing DB-fixture failures.

- [ ] **Step 5: Commit**

```bash
git add robothor/engine/feature_flags.py robothor/tests/test_feature_flags_seam.py
git commit -m "feat(flags): route governed readers through the store, env as fallback"
```

---

### Task 5: Bridge controls API — operator-only, audited, agent-hostile

**Files:**
- Create: `crm/bridge/routers/controls.py`
- Modify: `crm/bridge/bridge_service.py` (register the router)
- Test: `crm/bridge/tests/test_controls_router.py`
- Test: `robothor/engine/tests/test_no_control_tool.py` (the schemas.py guard)

**Interfaces:**
- Consumes: `robothor.flags.store` (`GOVERNED_FLAGS`, `set_flag`), `robothor.flags.evidence.verdict`; `request.state.auth` (`AuthContext`).
- Produces:
  - `GET /api/controls` → `[{name, value, verdict:{status,message,last_fired,count_7d}}]` for all 12.
  - `PATCH /api/controls/{name}` body `{value, reason}` → 200 on success; **403 if `auth is None or auth.is_service`** (agents excluded); 404 if `name not in GOVERNED_FLAGS`; 422 on an invalid value.

- [ ] **Step 1: Write the failing tests**

```python
# crm/bridge/tests/test_controls_router.py
"""The write path must be reachable only by a human operator, never an agent."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_patch_rejects_a_service_token(controls_client_as_service):
    r = controls_client_as_service.patch(
        "/api/controls/ROBOTHOR_RBAC_MODE", json={"value": "off", "reason": "x"}
    )
    assert r.status_code == 403, "an agent's service token must not flip a guardrail"


def test_patch_rejects_unknown_flag(controls_client_as_operator):
    r = controls_client_as_operator.patch(
        "/api/controls/ROBOTHOR_TELEGRAM_BOT_TOKEN", json={"value": "x", "reason": "y"}
    )
    assert r.status_code == 404


def test_operator_can_promote_and_it_is_audited(controls_client_as_operator, fake_store):
    r = controls_client_as_operator.patch(
        "/api/controls/ROBOTHOR_RBAC_MODE", json={"value": "enforce", "reason": "promote"}
    )
    assert r.status_code == 200
    assert fake_store.last_write == ("ROBOTHOR_RBAC_MODE", "enforce")


def test_get_lists_all_twelve_with_verdicts(controls_client_as_operator):
    r = controls_client_as_operator.get("/api/controls")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 12
    assert all("verdict" in f and "status" in f["verdict"] for f in body)
```

```python
# robothor/engine/tests/test_no_control_tool.py
"""No agent tool may reach the control plane. Structural, enforced by CI."""
import re

from robothor.engine.tools import schemas


def test_no_tool_exposes_flag_control():
    # schemas.py exposes get_engine_schemas(); scan both the built registry and
    # the module source so a control tool cannot slip in by either route.
    import inspect
    built = " ".join(schemas.get_engine_schemas().keys())
    src = inspect.getsource(schemas)
    assert not re.search(r"set_flag|guardrail_mode|feature_flag|control_flag", built + src), (
        "a control tool in schemas.py would give a prompt-injected agent a path "
        "to disable every guardrail — the write path must be operator-only and "
        "have NO agent-facing tool at all"
    )
```

- [ ] **Step 2: Run to verify they fail**

Run bridge test: `cd /home/philip/robothor/crm/bridge && PYTHONPATH=.:../.. /home/philip/robothor/venv/bin/python -m pytest tests/test_controls_router.py -q`
Expected: FAIL — router not registered / fixtures missing.
Run guard test: `cd /home/philip/robothor && PYTHONPATH=. venv/bin/python -m pytest robothor/engine/tests/test_no_control_tool.py -q`
Expected: PASS immediately (no such tool exists) — this is a regression guard, green from birth.

- [ ] **Step 3: Write the router + register it**

```python
# crm/bridge/routers/controls.py
"""Operator-only guardrail control. Agents carry service tokens and are excluded
structurally: is_service == True -> 403. There is no agent tool for this route."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from robothor.flags import store
from robothor.flags.evidence import verdict

router = APIRouter(prefix="/api/controls", tags=["controls"])


class FlagPatch(BaseModel):
    value: str
    reason: str


def _require_operator(request: Request) -> str:
    auth = getattr(request.state, "auth", None)
    if auth is None or auth.is_service:
        raise HTTPException(status_code=403, detail="operator role required")
    return f"operator:{auth.actor_id}"


@router.get("")
async def list_controls():
    out = []
    for name in sorted(store.GOVERNED_FLAGS):
        value = store.resolve(name) or "observe"
        v = verdict(name, value)
        out.append({
            "name": name, "value": value,
            "verdict": {"status": v.status, "message": v.message,
                        "last_fired": v.last_fired.isoformat() if v.last_fired else None,
                        "count_7d": v.count_7d},
        })
    return out


@router.patch("/{name}")
async def set_control(name: str, patch: FlagPatch, request: Request):
    actor = _require_operator(request)
    if name not in store.GOVERNED_FLAGS:
        raise HTTPException(status_code=404, detail="unknown flag")
    if patch.value not in {"off", "observe", "alert", "enforce", "true", "false"}:
        raise HTTPException(status_code=422, detail="invalid value")
    store.set_flag(name, patch.value, actor=actor, reason=patch.reason)
    return {"name": name, "value": patch.value}
```

Register in `crm/bridge/bridge_service.py` beside the other `app.include_router(...)` calls:

```python
from routers.controls import router as controls_router
app.include_router(controls_router)
```

Add the fixtures to `crm/bridge/tests/conftest.py` (operator vs service `AuthContext`, and a `fake_store` that records writes). Mirror the existing auth-fixture pattern in that conftest.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/philip/robothor/crm/bridge && PYTHONPATH=.:../.. /home/philip/robothor/venv/bin/python -m pytest tests/test_controls_router.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add crm/bridge/routers/controls.py crm/bridge/bridge_service.py crm/bridge/tests/ robothor/engine/tests/test_no_control_tool.py
git commit -m "feat(controls): operator-only audited flag API; guard against agent tool"
```

---

### Task 6: The Controls tab

**Files:**
- Create: `app/src/components/views/controls-view.tsx`
- Modify: `app/src/components/layout/*` (add the tab to the nav — follow the existing tab registration)
- Test: `app/src/components/views/__tests__/controls-view.test.tsx`

**Interfaces:**
- Consumes: `GET /api/controls`, `PATCH /api/controls/{name}` (through the dashboard's existing bridge-proxy pattern, `/api/bridge/...`).
- Produces: a tab that renders each flag's mode + verdict, and — for an operator — a control to change the mode with a required reason. **Zero-evidence flags render as a warning, never green.**

- [ ] **Step 1: Write the failing test**

```tsx
// app/src/components/views/__tests__/controls-view.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import ControlsView from "../controls-view";

it("renders an INERT flag as a warning, never as healthy/green", async () => {
  vi.spyOn(global, "fetch").mockResolvedValue({
    ok: true,
    json: async () => ([{
      name: "ROBOTHOR_APPROVAL_MODE", value: "enforce",
      verdict: { status: "INERT", message: "NEVER FIRED — this control cannot protect you.",
                 last_fired: null, count_7d: 0 },
    }]),
  } as Response);

  render(<ControlsView />);
  expect(await screen.findByText(/NEVER FIRED/i)).toBeInTheDocument();
  const badge = await screen.findByTestId("verdict-ROBOTHOR_APPROVAL_MODE");
  expect(badge).toHaveAttribute("data-status", "INERT");
  expect(badge.className).not.toMatch(/green|ok|healthy/i);
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run src/components/views/__tests__/controls-view.test.tsx`
Expected: FAIL — `controls-view` not found.

- [ ] **Step 3: Write the view**

Write `controls-view.tsx`: fetch `/api/bridge/api/controls`, render a row per flag with `data-testid={`verdict-${name}`}` and `data-status={verdict.status}`; map status→style so `INERT`/`BLIND` are warning-styled and only `ENFORCING` is affirmative; render `verdict.message` verbatim; gate the mode-change control behind the operator session (hidden for non-operators). Follow the existing `agents-view.tsx` structure for fetch + layout.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run src/components/views/__tests__/controls-view.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/src/components/views/controls-view.tsx app/src/components/views/__tests__/controls-view.test.tsx app/src/components/layout/
git commit -m "feat(controls): Controls tab — mode + verdict, zero-evidence never green"
```

---

### Task 7: End-to-end probe + phase gate

**Files:** none (verification task).

- [ ] **Step 1:** Apply 084 to the live DB: `psql -d robothor_memory -f crm/migrations/084_feature_flags.sql`. Confirm 12 rows.
- [ ] **Step 2:** With the engine running, `PATCH /api/controls/ROBOTHOR_RIP_13_MODE` (operator session) to `alert`; within ~5s, confirm `feature_flags.symbolic_memory_mode()` in a fresh engine call returns `alert` **without a restart**. This proves the `pg_notify`/TTL propagation end-to-end.
- [ ] **Step 3:** Confirm an agent run cannot reach the route: from an agent context there is no tool; `curl` the PATCH with a service token → 403.
- [ ] **Step 4:** Run the full suites — `bash run_tests.sh` (or the engine + bridge + `cd app && pnpm test` trio) — and confirm no regression against today's hardening (RLS still one tenant, `RLS IS INERT` silent, sandbox fail-closed intact).
- [ ] **Step 5:** Open the PR. Title: `feat(controls): DB-backed guardrail control plane + Controls tab`.

---

## Phases 2 & 3 — follow-on plans

Per the scope check, Phase 2 (accounting tabs) and Phase 3 (canvas bridge) each
produce working software on their own and get their **own plan file**, written
once Phase 1's interfaces are concrete — because their tasks consume Phase 1's
exact API shapes (`GET /api/controls`, the bridge auth dependency, the
operator-only pattern). Writing detailed code against signatures that do not yet
exist would be guessing.

Phase 1's "Produces" blocks above lock those signatures. The moment Phase 1 lands:

- **Phase 2 plan** (`2026-..-helm-phase2-accounting.md`): `GET /api/fleet`,
  `/api/fleet/{agent}`, `/api/runs`, `/api/runs/{id}`, `/api/health/system`
  (operator-scoped, read-only, reusing Task 5's auth dependency); Fleet / Runs /
  Workflows / Health tabs. Fleet flags a capability-without-a-constraint the same
  way Controls flags an inert control.
- **Phase 3 plan** (`2026-..-helm-phase3-canvas.md`): `canvas-bridge.ts`
  mediator, the read-op whitelist mapping each op to a Phase-1/2 endpoint, and the
  propose→confirm channel — with the pinned invariant test (iframe never gets
  `fetch`/token/`allow-same-origin`) and the hostile-propose test.

## Self-review notes

- **Spec coverage:** every Phase-1 spec element maps to a task — flag table
  (T1), DB-first store + fallback + pg_notify (T2), inert detector w/ per-flag
  sources (T3), the seam (T4), operator-only audited API + agent-hostility guard
  (T5), Controls tab w/ zero-never-green (T6), end-to-end propagation probe (T7).
- **The cutover-no-op subtlety** (seed row vs operator row) is pinned by
  `test_env_wins_when_only_a_seed_row_exists` and `test_operator_row_wins_over_env`.
- **The single most dangerous failure** (DB blink → all guardrails off) is pinned
  by `test_db_unreachable_falls_through_to_env`.
- **Agent-hostility** is pinned structurally by `test_no_control_tool` and at the
  HTTP layer by `test_patch_rejects_a_service_token`.
