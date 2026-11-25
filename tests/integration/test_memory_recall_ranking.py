"""Memory recall ranking against a real Postgres + pgvector.

Two behaviours that only a real database can show:

- **Narrow filters return what was asked for.** HNSW walks its graph and the
  tenant predicate is applied afterwards, so without an iterative scan the
  candidate budget (``hnsw.ef_search``) can be spent entirely on another
  tenant's rows and the search comes back short — or empty — although the
  rows exist. Every pooled connection gets an iterative scan at checkout
  (``robothor.db.connection``); these tests apply the same setting, because
  the integration fixture's raw connection does not.
- **A natural question matches by keyword.** ``plainto_tsquery`` ANDs every
  term, so one word the fact does not contain matched nothing.

The embedding service is stubbed (an external dependency); everything else is
the real SQL. Rows are written inside the per-test transaction and rolled back.
"""

from __future__ import annotations

import random
import uuid

import pytest
from psycopg2.extras import execute_values

pytestmark = pytest.mark.integration

_DIM = 1024


def _vec(axis: int, noise: float, rng: random.Random) -> list[float]:
    v = [rng.gauss(0.0, noise) for _ in range(_DIM)]
    v[axis] += 1.0
    return v


def _tenant(db_cursor, prefix: str, name: str) -> str:
    tenant = f"{prefix}-{name}"
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (tenant, tenant),
    )
    return tenant


def _insert(db_cursor, rows: list[tuple[str, str, list[float]]]) -> None:
    execute_values(
        db_cursor,
        "INSERT INTO memory_facts "
        "(fact_text, category, tenant_id, is_active, importance_score, embedding) VALUES %s",
        [(text, "event", tenant, True, 0.5, str(vec)) for text, tenant, vec in rows],
        template="(%s, %s, %s, %s, %s, %s::vector)",
    )


@pytest.fixture
def _stub_embedding(monkeypatch):
    from robothor.llm import ollama as llm_client

    query_vector = [0.0] * _DIM
    query_vector[0] = 1.0

    async def _fake(_text: str, *a, **k):
        return list(query_vector)

    monkeypatch.setattr(llm_client, "get_embedding_async", _fake)


@pytest.fixture
def _production_scan_mode(db_conn):
    """Give the test connection the session settings every pooled checkout gets.

    ``mock_get_connection`` hands out the raw test connection, which never runs
    ``robothor.db.connection._apply_session_settings`` — so without this the
    test measures a connection production never uses. The SET is inside the
    per-test transaction and is rolled back with it.
    """
    from robothor.db import connection

    connection._apply_ann_scan_mode(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("SHOW hnsw.iterative_scan")
        assert cur.fetchone()[0] == connection.ANN_ITERATIVE_SCAN_MODE


@pytest.fixture
def _hnsw_plan(db_conn):
    """Make the planner take the HNSW index, as it does on a large table.

    On a small shared test table the planner may prefer the tenant btree plus
    a sort, which is exact and hides the post-filter starvation under test.
    Disabling the sort leaves the ordered index scan as the only cheap plan.
    """
    with db_conn.cursor() as cur:
        cur.execute("SET LOCAL enable_sort = off")


@pytest.fixture
def _no_iterative_scan(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("SET LOCAL hnsw.iterative_scan = off")


def _seed_crowd_and_narrow(db_cursor, test_prefix: str) -> str:
    rng = random.Random(868)
    crowd = _tenant(db_cursor, test_prefix, "crowd")
    narrow = _tenant(db_cursor, test_prefix, "narrow")
    # 1,500 rows of another tenant sit right next to the query vector; the
    # narrow tenant's 12 rows are far away. ef_search=100 is spent on the crowd.
    _insert(
        db_cursor,
        [(f"crowd note {uuid.uuid4().hex}", crowd, _vec(0, 0.02, rng)) for _ in range(1500)],
    )
    _insert(
        db_cursor,
        [(f"narrow item {uuid.uuid4().hex}", narrow, _vec(1, 0.02, rng)) for _ in range(12)],
    )
    return narrow


@pytest.mark.asyncio
async def test_narrow_tenant_filter_returns_limit_rows(
    db_cursor, mock_get_connection, _stub_embedding, _hnsw_plan, _production_scan_mode, test_prefix
):
    """Acceptance: given a narrow tenant filter, when k results are requested and
    k exist, then k are returned — with the scan mode every checkout carries."""
    from robothor.memory.facts import search_facts

    narrow = _seed_crowd_and_narrow(db_cursor, test_prefix)

    results = await search_facts(
        "quarterly figures", limit=10, use_reranker=False, tenant_id=narrow
    )

    assert len(results) == 10, f"asked for 10 of 12 existing rows, got {len(results)}"
    assert all(r["fact_text"].startswith("narrow item") for r in results)


@pytest.mark.asyncio
async def test_without_iterative_scan_the_narrow_tenant_comes_back_short(
    db_cursor, mock_get_connection, _stub_embedding, _hnsw_plan, _no_iterative_scan, test_prefix
):
    """Negative control: the fixture really does starve a post-filtered scan, so
    the test above passes because of the iterative scan, not a sparse corpus."""
    from robothor.memory.facts import search_facts

    narrow = _seed_crowd_and_narrow(db_cursor, test_prefix)

    results = await search_facts(
        "quarterly figures", limit=10, use_reranker=False, tenant_id=narrow
    )

    assert len(results) < 10


@pytest.fixture
def _embedding_down(monkeypatch):
    import httpx

    from robothor.llm import ollama as llm_client

    async def _down(_text: str, *a, **k):
        raise httpx.ConnectError("embedding service down")

    monkeypatch.setattr(llm_client, "get_embedding_async", _down)


@pytest.mark.asyncio
async def test_natural_question_matches_by_keyword(
    db_cursor, mock_get_connection, _embedding_down, test_prefix
):
    """'beverage' is not in the fact, so an AND of every term matches nothing;
    the OR fallback still finds the fact that shares the other terms."""
    from robothor.memory.facts import search_facts

    tenant = _tenant(db_cursor, test_prefix, "kw")
    rng = random.Random(1)
    _insert(
        db_cursor,
        [
            ("Alice drinks green tea every morning", tenant, _vec(2, 0.02, rng)),
            ("Bob cycles to the office on Fridays", tenant, _vec(3, 0.02, rng)),
        ],
    )

    results = await search_facts(
        "What beverage does Alice drink each morning?",
        limit=5,
        use_reranker=False,
        tenant_id=tenant,
    )

    assert results, "the OR fallback must find the fact sharing most terms"
    assert results[0]["fact_text"] == "Alice drinks green tea every morning"
    score = results[0]["leg_scores"]["keyword"]
    assert 0.0 < score <= 1.0, "ts_rank_cd with normalisation stays on a 0..1 scale"


@pytest.mark.asyncio
async def test_all_terms_present_still_uses_the_and_form(
    db_cursor, mock_get_connection, _embedding_down, test_prefix
):
    """The OR form is a fallback: when every term matches, only those rows return."""
    from robothor.memory.facts import search_facts

    tenant = _tenant(db_cursor, test_prefix, "kwand")
    rng = random.Random(2)
    _insert(
        db_cursor,
        [
            ("Alice drinks green tea every morning", tenant, _vec(2, 0.02, rng)),
            ("Alice drives to work", tenant, _vec(3, 0.02, rng)),
        ],
    )

    results = await search_facts("Alice tea morning", limit=5, use_reranker=False, tenant_id=tenant)

    assert [r["fact_text"] for r in results] == ["Alice drinks green tea every morning"]
