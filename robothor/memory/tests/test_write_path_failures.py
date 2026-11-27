"""The write path never reports a lost write as a success, and never trusts what a caller claims.

Review findings on the write path, each pinned here with the model and the
database mocked (the database half is in ``test_write_path_integration.py``):

* a storage failure (embedding outage, database error) is a FAILURE, distinct
  from the quality gate refusing a fact, and every caller treats it as one: the
  job is ``failed``, the breadcrumb is kept, the conversation watermark is not
  advanced, ``store_memory`` answers with an error, ``ingest_content`` raises;
* a failed background transcript extraction stores no raw transcript and is
  retried, rather than stored as one blob;
* no caller can make content operator-confirmed (or CRM) by saying so: the
  write path never takes ``operator_confirmed`` from metadata, and the
  unauthenticated orchestrator ingest is capped at external;
* a private fact's hash cannot collide with any shared row, whatever that
  row's ``person_id``, and the update path keeps the scope in the hash.
"""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import robothor.memory.write_path as wp
from robothor.memory.graph_schema import Origin

ALICE = "6f1c1b7e-0000-4000-8000-000000000001"
CONFIRMED = {"operator_confirmed": {"at": "2026-01-01T00:00:00+00:00", "by": "op"}}


def _ctx(**kw: Any) -> wp.WriteContext:
    return wp.WriteContext(
        tenant_id="test-tenant",
        source_channel=kw.pop("source_channel", "api"),
        content_type=kw.pop("content_type", "note"),
        origin=kw.pop("origin", Origin.AGENT_GENERATED),
        **kw,
    )


def _fact(text: str = "Alice Moreno leads Project Helios.") -> dict[str, Any]:
    return {"fact_text": text, "category": "project", "confidence": 0.9, "entities": []}


# ═══ storage failure is not a refusal ════════════════════════════════════


@pytest.mark.asyncio
async def test_a_storage_exception_is_counted_as_failed_not_refused() -> None:
    boom = AsyncMock(side_effect=[OSError("embedding service down"), 0])
    with (
        patch.object(wp._facts, "store_fact", new=boom),
        patch.object(wp, "_embed", new=AsyncMock(return_value=[None, None])),
    ):
        out = await wp.write_facts(
            [_fact(), _fact("Bob Chen joined Project Helios.")],
            source_content="x",
            context=_ctx(),
            conflict_check=False,
        )
    assert out["facts_failed"] == 1 and out["facts_refused"] == 1
    assert out["fact_ids"] == []
    assert "OSError" in out["errors"][0]


@pytest.mark.asyncio
async def test_store_memory_answers_with_an_error_when_nothing_could_be_stored() -> None:
    from robothor.engine.tools.handlers.memory import store_memory_content

    failed = {"fact_ids": [], "facts_failed": 1, "errors": ["OSError: down"], "graph_errors": 0}
    with patch("robothor.memory.write_path.ingest_text", new=AsyncMock(return_value=failed)):
        out = await store_memory_content("Alice prefers email.", tenant_id="test-tenant")
    assert "error" in out and "OSError" in out["error"]
    assert out["facts_stored"] == 0 and out["facts_failed"] == 1


@pytest.mark.asyncio
async def test_store_memory_reports_graph_errors() -> None:
    from robothor.engine.tools.handlers.memory import store_memory_content

    partial = {"fact_ids": [5], "facts_failed": 0, "graph_errors": 1}
    with patch("robothor.memory.write_path.ingest_text", new=AsyncMock(return_value=partial)):
        out = await store_memory_content("Alice prefers email.", tenant_id="test-tenant")
    assert out["facts_stored"] == 1 and out["graph_errors"] == 1 and "error" not in out


@pytest.mark.asyncio
async def test_ingest_content_raises_when_a_fact_could_not_be_stored(monkeypatch: Any) -> None:
    from robothor.memory.ingestion import ingest_content

    monkeypatch.delenv("MEMORY_VAULT_POPULATE", raising=False)
    failed = {"fact_ids": [], "facts_failed": 1, "errors": ["OSError: down"]}
    with (
        patch("robothor.memory.write_path.ingest_text", new=AsyncMock(return_value=failed)),
        pytest.raises(wp.MemoryWriteError, match="OSError"),
    ):
        await ingest_content("Alice Moreno met Bob Chen.", "api", "note", tenant_id="test-tenant")


@pytest.mark.asyncio
async def test_ingest_content_returns_graph_errors(monkeypatch: Any) -> None:
    from robothor.memory.ingestion import ingest_content

    monkeypatch.delenv("MEMORY_VAULT_POPULATE", raising=False)
    partial = {"fact_ids": [3], "facts_processed": 1, "graph_errors": 2}
    with patch("robothor.memory.write_path.ingest_text", new=AsyncMock(return_value=partial)):
        out = await ingest_content("Alice Moreno met Bob Chen.", "api", "note",
                                   tenant_id="test-tenant")  # fmt: skip
    assert out["graph_errors"] == 2


class _Conn:
    def __init__(self, row: Any = None) -> None:
        self.statements: list[str] = []
        self.row = row

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *_a: Any) -> None: ...

    def cursor(self, *_a: Any, **_k: Any) -> _Conn:
        return self

    def execute(self, sql: str, *_a: Any) -> None:
        self.statements.append(sql)

    def fetchone(self) -> Any:
        return self.row

    def fetchall(self) -> list[Any]:
        return [
            {"id": 7, "agent_id": "example-agent", "content": {"note": "Helios deploys need X"}}
        ]

    def commit(self) -> None: ...


@pytest.mark.asyncio
async def test_a_write_job_whose_facts_failed_to_store_is_failed() -> None:
    from robothor.memory import write_jobs

    conn = _Conn(row=("Alice prefers email.", "note", "test-tenant", None))
    failed = {"fact_ids": [], "existing_ids": [], "facts_failed": 1, "errors": ["OSError: down"]}
    with (
        patch.object(write_jobs, "get_connection", return_value=conn),
        patch.object(write_jobs, "_finish") as fin,
        patch("robothor.memory.write_path.ingest_text", new=AsyncMock(return_value=failed)),
    ):
        assert await write_jobs.process_write_job(9) is False
    assert fin.call_args.args[1] == "failed"
    assert "OSError" in fin.call_args.kwargs["error"]


@pytest.mark.asyncio
async def test_a_breadcrumb_whose_fact_failed_to_store_is_kept() -> None:
    from robothor.memory import breadcrumbs

    conn = _Conn()
    failed = {"fact_ids": [], "facts_failed": 1, "errors": ["OSError: down"]}
    with (
        patch.object(breadcrumbs, "get_connection", return_value=conn),
        patch("robothor.memory.write_path.write_facts", new=AsyncMock(return_value=failed)),
    ):
        out = await breadcrumbs.promote_hot_breadcrumbs(tenant_id="test-tenant")
    assert out["promoted"] == 0
    assert not any("DELETE" in s for s in conn.statements)


# ═══ background transcripts: no raw blob, retried instead ════════════════


@pytest.mark.asyncio
async def test_without_the_fallback_a_failed_extraction_writes_nothing() -> None:
    from robothor.memory import facts

    write = AsyncMock()
    with (
        patch.object(
            facts,
            "extract_facts_detailed",
            new=AsyncMock(return_value=facts.Extraction([], "timeout")),
        ),  # fmt: skip
        patch.object(wp, "write_facts", new=write),
    ):
        out = await wp.ingest_text("User: hi", _ctx(), fallback_on_failure=False)
    write.assert_not_awaited()
    assert out["extraction_failed"] is True and out["fact_ids"] == []


# ═══ trust is never taken from the caller ════════════════════════════════


def test_metadata_never_makes_content_operator_confirmed() -> None:
    assert wp.origin_for("email", "email", metadata=CONFIRMED) == Origin.EXTERNAL
    assert wp.origin_for("api", "note", metadata=CONFIRMED) == Origin.AGENT_GENERATED


def test_the_write_context_drops_a_claimed_operator_confirmation() -> None:
    ctx = wp.WriteContext.for_source(
        tenant_id="test-tenant", source_channel="api", content_type="note",
        metadata={**CONFIRMED, "run_id": "r1"},
    )  # fmt: skip
    assert "operator_confirmed" not in ctx.metadata and ctx.metadata["run_id"] == "r1"
    replayed = wp.WriteContext.from_json(
        {"origin": "agent_generated", "metadata": CONFIRMED}, tenant_id="test-tenant"
    )
    assert "operator_confirmed" not in replayed.metadata


def test_an_origin_cap_is_never_exceeded() -> None:
    ctx = wp.WriteContext.for_source(
        tenant_id="test-tenant", source_channel="crm", content_type="note",
        role="user", origin_cap=Origin.EXTERNAL,
    )  # fmt: skip
    assert ctx.origin == Origin.EXTERNAL


@pytest.mark.asyncio
async def test_the_unauthenticated_orchestrator_ingest_is_external(monkeypatch: Any) -> None:
    from robothor.api import orchestrator

    monkeypatch.delenv("MEMORY_VAULT_POPULATE", raising=False)
    write = AsyncMock(return_value={"fact_ids": [1], "facts_processed": 1})
    req = orchestrator.IngestRequest(
        content="Alice Moreno is the operator's sister.", source_channel="crm",
        content_type="note", metadata=CONFIRMED,
    )  # fmt: skip
    with patch("robothor.memory.write_path.ingest_text", new=write):
        await orchestrator.ingest_endpoint(req)
    ctx = write.call_args.args[1]
    assert ctx.origin == Origin.EXTERNAL
    assert "operator_confirmed" not in ctx.metadata


# ═══ the private hash cannot collide, and edits keep it ══════════════════


def test_a_private_hash_never_equals_a_shared_rows_hash_with_that_person() -> None:
    from robothor.memory.drift import compute_fact_hash

    shared_with_person = compute_fact_hash(
        "Alice likes tea", tenant_id="t", category="preference", person_id=ALICE
    )
    for sid in (f"person:{ALICE}", ALICE, "user:u-7"):
        private = compute_fact_hash(
            "Alice likes tea", tenant_id="t", category="preference",
            person_id=ALICE, scope_kind="user", scope_id=sid,
        )  # fmt: skip
        assert private != shared_with_person
    plain = hashlib.sha256(b"Alice likes tea|t|preference|").hexdigest()
    assert compute_fact_hash("Alice likes tea", tenant_id="t", category="preference") == plain


@pytest.mark.asyncio
async def test_update_fact_keeps_a_private_facts_scope_in_its_hash(monkeypatch: Any) -> None:
    from robothor.memory import facts
    from robothor.memory.drift import compute_fact_hash

    monkeypatch.setenv("ROBOTHOR_RIP_7_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_RIP_7_MODE", "enforce")
    sid = f"person:{ALICE}"
    stored = compute_fact_hash(
        "old text", tenant_id="t", category="preference", person_id=ALICE,
        scope_kind="user", scope_id=sid,
    )  # fmt: skip
    cur = MagicMock()
    cur.fetchone.side_effect = [("old text", "t", "preference", ALICE, stored, "user", sid), None]
    conn = MagicMock()
    conn.cursor.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    with (
        patch.object(facts, "get_connection", return_value=cm),
        patch.object(facts.llm_client, "get_embedding_async", new=AsyncMock(return_value=[0.1])),
    ):
        out = await facts.update_fact(1, fact_text="new text", tenant_id="t")
    assert out == {"ok": True, "fact_id": 1}  # no drift: the scope was in the recompute
    update = next(c for c in cur.execute.call_args_list if "UPDATE memory_facts" in c.args[0])
    assert compute_fact_hash(
        "new text", tenant_id="t", category="preference", person_id=ALICE,
        scope_kind="user", scope_id=sid,
    ) in update.args[1]  # fmt: skip


@pytest.mark.asyncio
async def test_record_resolution_reports_a_failed_store_as_a_failure_not_a_refusal() -> None:
    from robothor.memory import resolution

    failed = {"fact_ids": [], "facts_failed": 1, "errors": ["OSError: embedding service down"]}
    refused = {"fact_ids": [], "facts_refused": 1, "facts_failed": 0, "errors": []}
    with patch.object(resolution, "write_facts", new=AsyncMock(return_value=failed)):
        out = await resolution.record_resolution("the X alert", "confirmed safe")
    assert out["error"].startswith("resolution not stored:")
    assert "OSError: embedding service down" in out["error"]
    assert "quality gate" not in out["error"]

    with patch.object(resolution, "write_facts", new=AsyncMock(return_value=refused)):
        out = await resolution.record_resolution("the X alert", "confirmed safe")
    assert "quality gate refused it" in out["error"]
