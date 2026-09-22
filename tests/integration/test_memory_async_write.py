"""Deferred memory writes are durable, and a lost one is discoverable.

store_memory extracts facts with an LLM on the request path: ~23s with the
model warm, a 63.5s production p50 because the 32B is almost never resident,
and 15 of 121 calls over 30 days killed at the 120s tool wall.

Moving extraction off the request path fixes that but risks something worse — a
write the agent was told had succeeded, silently lost. task_registry is the
right executor (chat.py and telegram.py already defer memory writes through it)
but daemon.py drains it with a 10-second budget against a ~60-second job under
Restart=always, so a deploy would destroy in-flight work with no trace.

The job row is the durability. These tests assert the property that matters:
after any interruption, an incomplete write is still on record and can be
retried. A test that only checked "the fact eventually appears" would pass just
as happily against a fire-and-forget implementation that loses work on restart.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

_CONSTANT_VECTOR = [0.1] * 1024


@pytest.fixture
def _stub_llm(monkeypatch):
    """Stub the LLM boundary only — external service, not the control."""
    from robothor.llm import ollama as llm_client
    from robothor.memory import facts as facts_mod

    async def _fake_extract(_content, *_a, **_kw):
        return [
            {
                "fact_text": "the depot runs fortnightly",
                "category": "project",
                "entities": [],
                "confidence": 0.9,
            }
        ]

    async def _fake_embedding(_t):
        return list(_CONSTANT_VECTOR)

    async def _fake_batch(texts):
        return [list(_CONSTANT_VECTOR) for _ in texts]

    monkeypatch.setattr(facts_mod, "extract_facts", _fake_extract)
    monkeypatch.setattr(llm_client, "get_embedding_async", _fake_embedding)
    monkeypatch.setattr(llm_client, "get_embeddings_batch_async", _fake_batch)


@pytest.fixture
def job_tenant(db_cursor, test_prefix):
    tenant = f"{test_prefix}-jobs"
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s,%s) ON CONFLICT (id) DO NOTHING",
        (tenant, tenant),
    )
    return tenant


@pytest.mark.asyncio
async def test_enqueue_records_the_promise_before_any_work(
    job_tenant, db_cursor, mock_get_connection
):
    """The row must exist the moment the caller is told 'queued'.

    If it were written after extraction, a crash during extraction would leave
    no evidence a write was ever promised — which is the whole failure this
    table prevents.
    """
    from robothor.memory.write_jobs import enqueue_write

    job_id = await enqueue_write("depot scheduling note", tenant_id=job_tenant)
    assert job_id

    db_cursor.execute(
        "SELECT status, attempts, content, fact_ids FROM memory_write_jobs WHERE id = %s",
        (job_id,),
    )
    row = db_cursor.fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["content"] == "depot scheduling note"
    assert row["fact_ids"] is None


@pytest.mark.asyncio
async def test_processing_stores_facts_and_marks_done(
    job_tenant, db_cursor, mock_get_connection, _stub_llm
):
    from robothor.memory.write_jobs import enqueue_write, process_write_job

    job_id = await enqueue_write("the depot runs fortnightly", tenant_id=job_tenant)
    await process_write_job(job_id)

    db_cursor.execute(
        "SELECT status, attempts, fact_ids FROM memory_write_jobs WHERE id = %s", (job_id,)
    )
    row = db_cursor.fetchone()
    assert row["status"] == "done"
    assert row["attempts"] == 1
    assert row["fact_ids"], "a completed job must record what it stored"

    db_cursor.execute(
        "SELECT tenant_id FROM memory_facts WHERE id = ANY(%s)", (row["fact_ids"],)
    )
    assert {r["tenant_id"] for r in db_cursor.fetchall()} == {job_tenant}


@pytest.mark.asyncio
async def test_failure_is_recorded_not_swallowed(
    job_tenant, db_cursor, mock_get_connection, monkeypatch
):
    from robothor.memory import facts as facts_mod
    from robothor.memory.write_jobs import enqueue_write, process_write_job

    async def _explode(*_a, **_kw):
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(facts_mod, "extract_facts", _explode)

    job_id = await enqueue_write("something", tenant_id=job_tenant)
    await process_write_job(job_id)

    db_cursor.execute(
        "SELECT status, attempts, error FROM memory_write_jobs WHERE id = %s", (job_id,)
    )
    row = db_cursor.fetchone()
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert "ollama unreachable" in (row["error"] or "")


@pytest.mark.asyncio
async def test_sweeper_finds_work_abandoned_by_a_restart(
    job_tenant, db_cursor, mock_get_connection, _stub_llm
):
    """A job left 'running' by a killed process must be recoverable.

    This is the deploy case: task_registry's 10-second drain cannot finish a
    ~60-second extraction, so the process dies mid-job. Without the sweeper the
    agent has been told the write succeeded and nothing will ever complete it.
    """
    from robothor.memory.write_jobs import enqueue_write, sweep_stale_jobs

    job_id = await enqueue_write("the depot runs fortnightly", tenant_id=job_tenant)
    # Simulate the interrupted state: claimed, then the process vanished.
    db_cursor.execute(
        "UPDATE memory_write_jobs SET status='running', attempts=1, "
        "updated_at = NOW() - interval '30 minutes' WHERE id = %s",
        (job_id,),
    )

    swept = await sweep_stale_jobs(stale_after_minutes=10, max_attempts=3)
    assert job_id in swept, f"stale job not reclaimed: {swept}"

    db_cursor.execute("SELECT status FROM memory_write_jobs WHERE id = %s", (job_id,))
    assert db_cursor.fetchone()["status"] == "done"


@pytest.mark.asyncio
async def test_sweeper_leaves_fresh_running_jobs_alone(
    job_tenant, db_cursor, mock_get_connection, _stub_llm
):
    """Negative control: a job that is merely slow must not be double-processed."""
    from robothor.memory.write_jobs import enqueue_write, sweep_stale_jobs

    job_id = await enqueue_write("x", tenant_id=job_tenant)
    db_cursor.execute(
        "UPDATE memory_write_jobs SET status='running', updated_at=NOW() WHERE id=%s", (job_id,)
    )
    assert job_id not in await sweep_stale_jobs(stale_after_minutes=10, max_attempts=3)


@pytest.mark.asyncio
async def test_poison_payload_stops_after_max_attempts(
    job_tenant, db_cursor, mock_get_connection, monkeypatch
):
    """A job that always fails must not be retried forever against a local LLM."""
    from robothor.memory import facts as facts_mod
    from robothor.memory.write_jobs import enqueue_write, sweep_stale_jobs

    async def _explode(*_a, **_kw):
        raise RuntimeError("always fails")

    monkeypatch.setattr(facts_mod, "extract_facts", _explode)

    job_id = await enqueue_write("poison", tenant_id=job_tenant)
    db_cursor.execute(
        "UPDATE memory_write_jobs SET status='running', attempts=3, "
        "updated_at = NOW() - interval '1 hour' WHERE id = %s",
        (job_id,),
    )
    assert job_id not in await sweep_stale_jobs(stale_after_minutes=10, max_attempts=3)
