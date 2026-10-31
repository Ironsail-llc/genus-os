"""Tests for robothor.memory.conflicts — classification schema, prompts and resolution."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import robothor.memory.conflicts as conflicts
from robothor.memory.conflicts import (
    CLASSIFICATION_SCHEMA,
    build_classification_prompt,
)


class TestClassificationSchema:
    def test_schema_has_classification(self):
        assert "classification" in CLASSIFICATION_SCHEMA["properties"]
        enum = CLASSIFICATION_SCHEMA["properties"]["classification"]["enum"]
        assert set(enum) == {"new", "duplicate", "update", "contradiction"}

    def test_schema_has_reasoning(self):
        assert "reasoning" in CLASSIFICATION_SCHEMA["properties"]

    def test_required_fields(self):
        assert set(CLASSIFICATION_SCHEMA["required"]) == {"classification", "reasoning"}


class TestBuildClassificationPrompt:
    def test_includes_both_facts(self):
        prompt = build_classification_prompt("New fact text", "Old fact text")
        assert "New fact text" in prompt
        assert "Old fact text" in prompt

    def test_includes_classification_options(self):
        prompt = build_classification_prompt("a", "b")
        assert "duplicate" in prompt
        assert "update" in prompt
        assert "contradiction" in prompt
        assert "new" in prompt


_CONFIRMED = {"operator_confirmed": {"at": "2026-01-01T00:00:00+00:00", "by": "operator"}}


def _match(metadata: Any) -> dict[str, Any]:
    return {
        "id": 7,
        "fact_text": "Alice leads the Helios project",
        "category": "personal",
        "similarity": 0.8,
        "metadata": metadata,
    }


class _Resolved:
    """What one resolve_and_store call did: its result, the supersede writer's
    calls, every statement on the bitemporal connection, and the decisions row."""

    store: AsyncMock
    classify: AsyncMock
    lookup: MagicMock

    def __init__(self, out: dict, supersede: MagicMock, cur: MagicMock, record: MagicMock):
        self.out = out
        self.supersede = supersede
        self.cur = cur
        self.record = record

    def statements(self) -> list[tuple[str, tuple]]:
        return [(c.args[0], tuple(c.args[1])) for c in self.cur.execute.call_args_list]


async def _resolve(
    match: dict[str, Any],
    kind: str,
    confidence: float,
    *,
    superseded: bool = True,
    prior_overruled: int | None = None,
    reread: tuple | None = None,
) -> _Resolved:
    import robothor.memory.bitemporal as bt

    supersede = MagicMock(return_value=superseded)
    # The conflicts module's own connection: the lookup for an earlier
    # overruled copy of the same extraction.
    lookup = MagicMock()
    # ``reread`` is the target's row as read back after a refused supersede.
    lookup.fetchone.return_value = (prior_overruled,) if prior_overruled else reread
    lookup_conn = MagicMock()
    lookup_conn.cursor.return_value = lookup
    lookup_ctx = MagicMock()
    lookup_ctx.__enter__.return_value = lookup_conn
    lookup_ctx.__exit__.return_value = False
    store = AsyncMock(return_value=99)
    classify = AsyncMock(return_value={"classification": kind, "reasoning": "r"})
    record = MagicMock()
    cur = MagicMock()
    cur.rowcount = 1
    conn = MagicMock()
    conn.cursor.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    with (
        patch.object(conflicts, "find_similar_facts", new=AsyncMock(return_value=[match])),
        patch.object(conflicts, "classify_relationship", new=classify),
        patch.object(conflicts, "store_fact", new=store),
        patch.object(conflicts, "supersede_with_validity", new=supersede),
        patch.object(conflicts, "record_conflict_decision", new=record),
        patch.object(conflicts, "get_connection", return_value=lookup_ctx),
        patch.object(bt, "get_connection", return_value=ctx),
    ):
        out = await conflicts.resolve_and_store(
            {
                "fact_text": "Bob leads the Helios project",
                "category": "personal",
                "entities": ["Bob"],
                "confidence": confidence,
            },
            "source",
            "conversation",
            tenant_id="test-tenant",
        )
    resolved = _Resolved(out, supersede, cur, record)
    resolved.store = store
    resolved.classify = classify
    resolved.lookup = lookup
    return resolved


def _assert_kept_confirmed(r: _Resolved) -> None:
    """The new row (99) is bounded to a zero-width interval and points at the
    confirmed fact (7); no statement touches the confirmed row itself."""
    assert r.out["action"] == "kept_confirmed"
    assert r.out["new_id"] == 99
    assert r.out["existing_id"] == 7
    statements = r.statements()
    assert len(statements) == 1
    sql, params = statements[0]
    assert "SET is_active = FALSE" in sql
    assert "superseded_by = %s" in sql
    assert "valid_from = COALESCE(valid_from, NOW())" in sql
    assert "valid_to = COALESCE(valid_to, NOW())" in sql
    assert "WHERE id = %s AND tenant_id = %s" in sql
    # 7 only as superseded_by and in the guard that keeps the write off it.
    assert params == (7, 99, "test-tenant", 7)
    assert "id <> %s" in sql
    assert r.record.call_count == 1
    assert r.record.call_args.kwargs["action"] == "kept_confirmed"
    assert r.record.call_args.kwargs["new_fact_id"] == 99


class TestOperatorConfirmedSurvivesConflicts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["contradiction", "update"])
    @pytest.mark.parametrize("confidence", [0.5, 1.0])
    async def test_a_confirmed_fact_is_never_superseded(self, kind: str, confidence: float):
        r = await _resolve(_match(_CONFIRMED), kind, confidence)
        # The new extraction is stored, then recorded as superseded BY the
        # confirmed fact: the pipeline's view is kept, the operator's stays
        # active and its validity is not rewritten.
        r.supersede.assert_not_called()
        _assert_kept_confirmed(r)

    @pytest.mark.asyncio
    async def test_metadata_as_a_json_string_is_still_confirmed(self):
        import json

        r = await _resolve(_match(json.dumps(_CONFIRMED)), "update", 1.0)
        r.supersede.assert_not_called()
        _assert_kept_confirmed(r)

    @pytest.mark.asyncio
    async def test_an_unconfirmed_fact_is_still_superseded(self):
        r = await _resolve(_match({}), "update", 1.0)
        assert r.out["action"] == "superseded"
        assert r.supersede.call_args.args[:2] == (7, 99)
        assert r.record.call_args.kwargs["action"] == "superseded"

    @pytest.mark.asyncio
    async def test_a_refused_supersede_is_reported_as_kept_confirmed(self):
        # Confirmed between the similarity search and the write: the guard in
        # supersede_with_validity refuses, so both facts must not stay active
        # under a "superseded" report.
        r = await _resolve(_match({}), "contradiction", 1.0, superseded=False, reread=(_CONFIRMED,))
        assert r.supersede.call_args.args[:2] == (7, 99)
        _assert_kept_confirmed(r)

    @pytest.mark.asyncio
    async def test_a_vanished_target_leaves_the_new_fact_active(self):
        # Refused because the target is gone, not because it is confirmed: the
        # new fact is the only current statement and must not be deactivated.
        r = await _resolve(_match({}), "update", 1.0, superseded=False, reread=None)
        assert r.out["action"] == "supersede_skipped"
        assert r.out["new_id"] == 99
        assert r.statements() == []  # keep_overruled never ran
        assert r.record.call_args.kwargs["action"] == "supersede_skipped"
        assert r.record.call_args.kwargs["new_fact_id"] == 99

    @pytest.mark.asyncio
    async def test_a_repeated_overruled_extraction_inserts_no_row(self):
        # The first pass left row 55 inactive, superseded by the confirmed
        # fact 7. Re-ingesting the same source must not mint another dead row,
        # nor pay for another classification.
        r = await _resolve(_match(_CONFIRMED), "contradiction", 1.0, prior_overruled=55)
        r.store.assert_not_awaited()
        r.classify.assert_not_awaited()
        r.supersede.assert_not_called()
        assert r.out["action"] == "kept_confirmed"
        assert r.out["new_id"] == 55
        assert r.out["existing_id"] == 7
        assert r.record.call_args.kwargs["action"] == "kept_confirmed"
        assert r.record.call_args.kwargs["new_fact_id"] == 55
        assert r.record.call_args.kwargs["existing_fact_id"] == 7
        # The lookup is tenant-scoped, by content hash, among rows overruled by 7.
        sql, params = r.lookup.execute.call_args_list[0].args
        assert "content_hash = %s" in sql
        assert "superseded_by = %s" in sql
        assert "tenant_id = %s" in sql
        assert "test-tenant" in params and 7 in params

    @pytest.mark.asyncio
    async def test_the_first_overruled_extraction_is_still_stored(self):
        r = await _resolve(_match(_CONFIRMED), "contradiction", 1.0, prior_overruled=None)
        r.store.assert_awaited_once()
        _assert_kept_confirmed(r)

    @pytest.mark.asyncio
    async def test_similar_facts_carry_their_metadata(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value = cur
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        ctx.__exit__.return_value = False
        with (
            patch.object(
                conflicts.llm_client, "get_embedding_async", new=AsyncMock(return_value=[0.0])
            ),
            patch.object(conflicts, "get_connection", return_value=ctx),
            patch.object(conflicts, "apply_hnsw_session"),
        ):
            await conflicts.find_similar_facts("anything", tenant_id="test-tenant")
        select = cur.execute.call_args.args[0]
        assert "metadata" in select.split("FROM")[0]
