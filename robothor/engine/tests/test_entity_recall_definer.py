"""Entity recall under row-level security, in a private disposable cluster.

As the engine's RLS-bound role, the tenant_isolation policy (migration 081)
keeps the planner from using idx_facts_entities_lower: neither
``lower_entities()`` nor ``&&`` is LEAKPROOF, so the entity match ran as a
filter over the tenant's whole fact set, once per candidate. Migration 166's
``entity_facts_for_candidates`` runs the lookup as its owner, which bypasses
the policy, and enforces the tenant rule itself. These tests pin both halves:
it is never wider than the policy, and it does use the index.

No shared database or credentials: the cluster lives under pytest's basetemp
and is stopped at the end. Skipped when the PostgreSQL binaries are absent.
"""

from __future__ import annotations

import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor, execute_values

from robothor.engine import warmup

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = (
    "081_tenant_rls_backstop.sql",
    "082_tenant_rls_app_role.sql",
    "109_facts_entities_lower_gin.sql",
    "166_entity_recall_definer.sql",
)
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
PERSON = "11111111-1111-1111-1111-111111111111"
OTHER_PERSON = "22222222-2222-2222-2222-222222222222"
#: Enough rows per tenant that a per-tenant scan is the slow plan it was.
BULK_ROWS = 20000


def _binaries() -> Path | None:
    for candidate in (
        Path("/usr/lib/postgresql/18/bin"),
        Path("/usr/lib/postgresql/16/bin"),
        Path("/opt/homebrew/opt/postgresql@16/bin"),
    ):
        if (candidate / "initdb").exists():
            return candidate
    found = shutil.which("initdb")
    return Path(found).parent if found else None


def _seed(cur: Any) -> None:
    cur.execute(
        """CREATE TABLE memory_facts (
            id serial PRIMARY KEY,
            tenant_id text NOT NULL,
            fact_text text NOT NULL,
            category varchar(50) NOT NULL,
            entities text[] DEFAULT '{}',
            importance_score float DEFAULT 0.5,
            person_id uuid,
            is_active boolean DEFAULT TRUE,
            created_at timestamptz DEFAULT now()
        )"""
    )
    cur.execute("CREATE INDEX idx_facts_tenant ON memory_facts (tenant_id)")
    bulk = [
        (tenant, f"{tenant} filler {i}", "c", [f"filler{i % 2000}"], 0.5, None, True)
        for tenant in (TENANT_A, TENANT_B)
        for i in range(BULK_ROWS)
    ]
    named = [
        (TENANT_A, "Bob owns the billing service.", "person", ["Bob"], 0.9, None, True),
        (TENANT_A, "Bob is on leave next week.", "person", ["BOB"], 0.6, None, True),
        (TENANT_A, "Bob's old desk was on floor two.", "person", ["bob"], 0.2, None, False),
        (TENANT_A, "Alice reviewed the Example plan.", "project", ["Alice", "Example"], 0.7,
         PERSON, True),
        (TENANT_A, "Carol's salary review is due.", "person", ["Example"], 0.8,
         OTHER_PERSON, True),
        (TENANT_A, "The Example plan ships in March.", "project", ["Example"], 0.4, None, True),
        (TENANT_B, "B: Bob is tenant B's contractor.", "person", ["Bob"], 1.0, None, True),
        (TENANT_B, "B: the Example plan is cancelled.", "project", ["example"], 1.0, None, True),
    ]  # fmt: skip
    execute_values(
        cur,
        "INSERT INTO memory_facts "
        "(tenant_id, fact_text, category, entities, importance_score, person_id, is_active) "
        "VALUES %s",
        bulk + named,
    )
    for name in MIGRATIONS:
        path = ROOT / "crm" / "migrations" / name
        if path.exists():  # an absent migration fails the tests that call it
            cur.execute(path.read_text())
    cur.execute("ANALYZE memory_facts")


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    binary = _binaries()
    if binary is None:
        pytest.skip("PostgreSQL binaries required for disposable-cluster integration tests")
    root = tmp_path_factory.mktemp("recallpg")
    data, socket = root / "data", root / "s"
    socket.mkdir()

    def command(name: str, *args: Any) -> None:
        subprocess.run(
            [str(binary / name), *map(str, args)], check=True, capture_output=True, text=True
        )

    command("initdb", "-D", data, "-U", "recalltest", "--auth=trust", "--no-locale", "-E", "UTF8")
    command("pg_ctl", "-D", data, "-l", root / "pg.log", "-o", f"-F -h '' -k {socket}", "-w",
            "start")  # fmt: skip
    try:
        command("createdb", "-h", socket, "-U", "recalltest", "recall")
        dsn = f"dbname=recall user=recalltest host={socket}"
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            _seed(cur)
        yield dsn
    finally:
        command("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")


@contextmanager
def _as_app(dsn: str, tenant: str | None):
    """A connection as the RLS-bound role, scoped to ``tenant`` (None: unscoped)."""
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET ROLE robothor_app")
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant or "",))
        yield conn
    finally:
        conn.close()


def _call(
    conn: Any,
    tenant: str,
    names: list[str],
    *,
    restricted: bool = False,
    person: str | None = None,
) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fact_text FROM entity_facts_for_candidates(%s, %s::text[], %s, %s::uuid)",
            (tenant, names, restricted, person),
        )
        return [r[0] for r in cur.fetchall()]


class TestNeverWiderThanThePolicy:
    def test_each_tenant_gets_only_its_own_facts(self, cluster: str) -> None:
        for scoped in (True, False):
            with _as_app(cluster, TENANT_A if scoped else None) as conn:
                a = _call(conn, TENANT_A, ["Bob", "Example"])
            with _as_app(cluster, TENANT_B if scoped else None) as conn:
                b = _call(conn, TENANT_B, ["Bob", "Example"])
            assert a and all(not t.startswith("B:") for t in a), (scoped, a)
            assert b and all(t.startswith("B:") for t in b), (scoped, b)

    def test_a_scoped_session_cannot_ask_for_another_tenant(self, cluster: str) -> None:
        with _as_app(cluster, TENANT_A) as conn:
            assert _call(conn, TENANT_B, ["Bob", "Example"]) == []

    def test_inactive_facts_stay_out(self, cluster: str) -> None:
        with _as_app(cluster, TENANT_A) as conn:
            assert "Bob's old desk was on floor two." not in _call(conn, TENANT_A, ["bob"])

    OWN = "Alice reviewed the Example plan."
    OTHERS = "Carol's salary review is due."
    NOBODYS = "The Example plan ships in March."

    def test_a_restricted_person_sees_own_and_unowned_facts_only(self, cluster: str) -> None:
        with _as_app(cluster, TENANT_A) as conn:
            facts = _call(conn, TENANT_A, ["Example"], restricted=True, person=PERSON)
        assert self.OWN in facts
        assert self.NOBODYS in facts
        assert self.OTHERS not in facts

    def test_restricted_with_no_linked_person_sees_only_unowned_facts(self, cluster: str) -> None:
        """An unverified or unrecognised caller is restricted with no person:
        it gets facts about no one, never any person's."""
        with _as_app(cluster, TENANT_A) as conn:
            facts = _call(conn, TENANT_A, ["Example"], restricted=True, person=None)
        assert facts == [self.NOBODYS], facts

    def test_unrestricted_sees_every_persons_facts(self, cluster: str) -> None:
        with _as_app(cluster, TENANT_A) as conn:
            facts = _call(conn, TENANT_A, ["Example"])
        assert {self.OWN, self.OTHERS, self.NOBODYS} <= set(facts), facts

    def test_the_app_role_cannot_call_it_without_the_grant(self, cluster: str) -> None:
        """Execute is granted to the app role, not to PUBLIC."""
        with psycopg2.connect(cluster) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT has_function_privilege('robothor_app', "
                "'entity_facts_for_candidates(text, text[], boolean, uuid)', 'EXECUTE'), "
                "has_function_privilege('public', "
                "'entity_facts_for_candidates(text, text[], boolean, uuid)', 'EXECUTE')"
            )
            app, public = cur.fetchone()
        assert app is True
        assert public is False


def _plans(conn: Any, sql: str, params: tuple[Any, ...]) -> list[str]:
    """Every plan the statement ran, nested ones included, via auto_explain."""
    with conn.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute("LOAD 'auto_explain'")
        cur.execute("SET auto_explain.log_min_duration = 0")
        cur.execute("SET auto_explain.log_nested_statements = on")
        cur.execute("SET auto_explain.log_level = 'notice'")
        cur.execute("SET ROLE robothor_app")
        del conn.notices[:]
        cur.execute(sql, params)
        cur.fetchall()
    return list(conn.notices)


class TestUsesTheEntitiesIndex:
    """Structural: read from the executed plan, not from a clock."""

    INLINE = (
        "SELECT f.fact_text FROM unnest(%s::text[]) WITH ORDINALITY AS c(name, ord) "
        "CROSS JOIN LATERAL (SELECT fact_text FROM memory_facts WHERE is_active "
        "AND tenant_id = %s AND lower_entities(entities) && ARRAY[lower(c.name)] "
        "LIMIT 3) f"
    )

    def test_the_inline_lookup_cannot_use_it_under_rls(self, cluster: str) -> None:
        """Why the function exists: the policy keeps the index out of reach."""
        with _as_app(cluster, TENANT_A) as conn:
            plans = _plans(conn, self.INLINE, (["Bob", "Example"], TENANT_A))
        assert plans
        assert not any("idx_facts_entities_lower" in p for p in plans), plans

    def test_the_function_uses_it(self, cluster: str) -> None:
        with _as_app(cluster, TENANT_A) as conn:
            plans = _plans(
                conn,
                "SELECT * FROM entity_facts_for_candidates(%s, %s::text[], false, NULL)",
                (TENANT_A, ["Bob", "Example"]),
            )
        assert any("idx_facts_entities_lower" in p for p in plans), plans


#: The lookup the warmup ran before migration 166, as the table owner: the
#: oracle the function's text must match.
REFERENCE = """
    SELECT c.ord AS candidate_ordinal, f.fact_text, f.category, f.importance_score,
           f.person_id
    FROM unnest(%s::text[]) WITH ORDINALITY AS c(name, ord)
    CROSS JOIN LATERAL (
        SELECT fact_text, category, importance_score, person_id, created_at,
               0.55 * COALESCE(importance_score, 0.5) + 0.45 * POWER(0.5,
               EXTRACT(EPOCH FROM (now() - created_at)) / 3600.0 / 168.0) AS rank_score
        FROM memory_facts
        WHERE is_active = TRUE AND tenant_id = %s
          AND lower_entities(entities) && ARRAY[lower(c.name)]
        ORDER BY rank_score DESC, created_at DESC
        LIMIT 3
    ) f
    ORDER BY c.ord, f.rank_score DESC, f.created_at DESC
"""


class TestWarmupTextIsUnchanged:
    def test_entity_context_matches_the_owner_reference(self, cluster: str) -> None:
        message = "Did Bob update the Example plan with Alice?"

        @contextmanager
        def _app_connection(*_a: Any, **_kw: Any):
            with _as_app(cluster, TENANT_A) as conn:
                yield conn

        with (
            patch.dict("os.environ", {"MEMORY_WARMUP_RECALL_V2": "1"}),
            patch("robothor.db.get_connection", _app_connection),
        ):
            text = warmup._build_entity_context(message, tenant_id=TENANT_A)

        candidates = warmup._extract_entity_candidates(message, lowercase_ok=True)[:5]
        with psycopg2.connect(cluster) as owner:
            cur = owner.cursor(cursor_factory=RealDictCursor)

            class _OneShot:
                def execute(self, _sql: str, _params: Any = ()) -> None:
                    cur.execute(REFERENCE, (candidates, TENANT_A))

                def fetchall(self) -> list[Any]:
                    return cur.fetchall()

            @contextmanager
            def _reference_connection(*_a: Any, **_kw: Any):
                class _Conn:
                    def cursor(self, cursor_factory: Any = None) -> _OneShot:
                        return _OneShot()

                yield _Conn()

            with (
                patch.dict("os.environ", {"MEMORY_WARMUP_RECALL_V2": "1"}),
                patch("robothor.db.get_connection", _reference_connection),
            ):
                expected = warmup._build_entity_context(message, tenant_id=TENANT_A)

        assert "Bob owns the billing service." in expected
        assert text == expected
