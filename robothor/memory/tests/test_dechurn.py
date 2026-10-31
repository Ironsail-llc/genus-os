"""Tests for the WS-8 reversible de-churn clustering (pure logic)."""

from __future__ import annotations

from robothor.memory.dechurn import cluster_near_dup_losers


def test_keeps_newest_drops_older_near_dup() -> None:
    facts = [
        {
            "id": 1,
            "fact_text": "An OpenRouter login was detected from Inwood",
            "entities": ["OpenRouter"],
        },
        {
            "id": 2,
            "fact_text": "An OpenRouter login was detected from Inwood",
            "entities": ["OpenRouter"],
        },
    ]
    assert cluster_near_dup_losers(facts) == [1]  # older id dropped, newest kept


def test_distinct_facts_are_not_collapsed() -> None:
    facts = [
        {"id": 1, "fact_text": "Alice manages the Helios project", "entities": ["Alice", "Helios"]},
        {"id": 2, "fact_text": "Bob is the security lead at FakeVendorCo", "entities": ["Bob"]},
    ]
    assert cluster_near_dup_losers(facts) == []


def test_requires_shared_entity() -> None:
    # Identical text but no shared entity → not compared, not collapsed.
    facts = [
        {"id": 1, "fact_text": "the migration completed successfully", "entities": ["ProjA"]},
        {"id": 2, "fact_text": "the migration completed successfully", "entities": ["ProjB"]},
    ]
    assert cluster_near_dup_losers(facts) == []


def test_transitive_cluster_keeps_only_newest() -> None:
    facts = [
        {"id": 1, "fact_text": "the X alert was reviewed and closed today", "entities": ["X"]},
        {"id": 2, "fact_text": "the X alert was reviewed and closed today", "entities": ["X"]},
        {"id": 3, "fact_text": "the X alert was reviewed and closed today", "entities": ["X"]},
    ]
    assert cluster_near_dup_losers(facts) == [1, 2]  # keep id 3 (newest)


def test_threshold_respected() -> None:
    facts = [
        {"id": 1, "fact_text": "Alice decided to use Postgres for storage", "entities": ["Alice"]},
        {"id": 2, "fact_text": "Alice decided to use SQLite for caching", "entities": ["Alice"]},
    ]
    # Different decisions, low overlap → not collapsed even though same entity.
    assert cluster_near_dup_losers(facts, jaccard=0.9) == []


def test_active_fact_load_skips_operator_confirmed_facts() -> None:
    # A confirmed fact is neither a loser nor a winner: dechurn never sees it.
    from unittest.mock import MagicMock

    from robothor.memory.confirmed import NOT_OPERATOR_CONFIRMED_SQL
    from robothor.memory.dechurn import _load_active_facts

    cur = MagicMock()
    cur2 = cur.connection.cursor.return_value
    cur2.fetchall.return_value = []
    assert _load_active_facts(cur, "test-tenant") == []
    sql = cur2.execute.call_args.args[0]
    assert "is_active = TRUE" in sql
    assert NOT_OPERATOR_CONFIRMED_SQL in sql
