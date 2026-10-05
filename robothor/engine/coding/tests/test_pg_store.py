"""PgJobStore against the real test database (rolled back after the test)."""

from __future__ import annotations

from pathlib import Path

import pytest

from robothor.engine.coding.jobs import Acceptance, CodingJob, JobStatus, PgJobStore

pytestmark = pytest.mark.integration

MIGRATION = Path(__file__).resolve().parents[4] / "crm" / "migrations" / "144_coding_jobs.sql"


@pytest.fixture
def store(db_conn, mock_get_connection):
    with db_conn.cursor() as cur:
        cur.execute(MIGRATION.read_text())
    return PgJobStore()


async def test_roundtrip_and_unfinished_listing(store, test_prefix):
    import uuid

    tenant = f"{test_prefix}-tenant"
    job = CodingJob(
        id=str(uuid.uuid4()),
        tenant_id=tenant,
        agent_id="main",
        task="make the failing test pass",
        repo_path="/tmp/repo",
        mode="code",
        acceptance=Acceptance(verify_command="pytest -q", require_commit=True),
        branch="genus/cc-abc",
        max_budget_usd=2.5,
    )
    await store.insert(job)

    job.status = JobStatus.RUNNING
    job.session_id = "5f0c7a52-0000-4000-8000-0000000000aa"
    job.rounds = 1
    job.cost_usd = 0.1234
    job.events_tail = ["tool_use: Bash: pytest -q"]
    job.result = {"verify": {"passed": False, "exit_code": 1}}
    await store.save(job)

    loaded = await store.get(job.id, tenant)
    assert loaded is not None
    assert loaded.status == JobStatus.RUNNING
    assert loaded.session_id == job.session_id
    assert loaded.acceptance.verify_command == "pytest -q"
    assert loaded.cost_usd == pytest.approx(0.1234)
    assert loaded.max_budget_usd == pytest.approx(2.5)
    assert loaded.events_tail == ["tool_use: Bash: pytest -q"]
    assert loaded.result["verify"]["exit_code"] == 1

    assert await store.get(job.id, "other-tenant") is None
    assert await store.get("not-a-uuid", tenant) is None
    unfinished = await store.list_unfinished(tenant)
    assert [j.id for j in unfinished] == [job.id]

    job.status = JobStatus.DONE
    await store.save(job)
    assert await store.list_unfinished(tenant) == []
