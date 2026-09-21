"""Immutable paid fragments survive retries, but never bypass work fencing."""

from pathlib import Path
from uuid import uuid4

import pytest

from robothor.operations.store import Conflict, Operations
from robothor.operations.tests.test_store import ops as ops


@pytest.fixture
def fragments(ops):
    from robothor.operations.fragments import Fragments

    with ops.transaction() as cur:
        cur.execute(
            (Path(__file__).parents[3] / "crm/migrations/133_operation_fragments.sql").read_text()
        )
    return Fragments(ops)


def test_fragments_are_immutable_tenant_bound_and_survive_reclaim(ops, fragments):
    ops.enqueue("research", "one", {})
    job = ops.claim("research")
    fragments.put(job, "topic", "a" * 64, {"run": "one"})
    fragments.put(job, "topic", "a" * 64, {"run": "one"})
    with pytest.raises(Conflict):
        fragments.put(job, "topic", "a" * 64, {"run": "two"})
    with pytest.raises(Conflict):
        fragments.read(job, "b" * 64)
    ops.defer(job["id"], job["lease_token"], "retry", delay_seconds=1)
    with ops.transaction() as cur:
        cur.execute("UPDATE operation_jobs SET available_at=now() WHERE id=%s", (job["id"],))
    replacement = ops.claim("research")
    assert fragments.read(replacement, "a" * 64) == {"topic": {"run": "one"}}
    with pytest.raises(Conflict):
        fragments.put(job, "late", "a" * 64, {})
    with pytest.raises(Conflict):
        type(fragments)(Operations("other-tenant")).read(replacement, "a" * 64)


@pytest.mark.parametrize("change", ["lease", "deadline", "token"])
def test_no_expired_or_replaced_fragment_reads_or_writes(ops, fragments, change):
    ops.enqueue("research", "one", {})
    job = ops.claim("research")
    if change == "token":
        job["lease_token"] = str(uuid4())
    else:
        column = "lease_until" if change == "lease" else "deadline"
        with ops.transaction() as cur:
            cur.execute(
                f"UPDATE operation_jobs SET {column}=now()-interval '1 second' WHERE id=%s",
                (job["id"],),
            )
    with pytest.raises(Conflict):
        fragments.put(job, "topic", "a" * 64, {})
    with pytest.raises(Conflict):
        fragments.read(job, "a" * 64)


def test_lock_wait_cannot_admit_work_after_lease_expiration(ops, fragments):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    ops.enqueue("research", "lock-race", {})
    job = ops.claim("research")
    entered = Event()

    def late_write():
        entered.set()
        fragments.put(job, "late", "a" * 64, {})

    with ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET lease_until=clock_timestamp()+interval '150 milliseconds' WHERE id=%s",
            (job["id"],),
        )
    with ThreadPoolExecutor(max_workers=1) as pool:
        with ops.transaction() as cur:
            cur.execute("SELECT id FROM operation_jobs WHERE id=%s FOR UPDATE", (job["id"],))
            future = pool.submit(late_write)
            assert entered.wait(1)
            time.sleep(0.25)
        with pytest.raises(Conflict):
            future.result(timeout=2)


def test_corrupted_fragment_content_is_not_reused(ops, fragments):
    ops.enqueue("research", "corruption", {})
    job = ops.claim("research")
    fragments.put(job, "topic", "a" * 64, {"run": "one"})
    with ops.transaction() as cur:
        cur.execute("UPDATE operation_fragments SET payload='{}' WHERE job_id=%s", (job["id"],))
    with pytest.raises(Conflict):
        fragments.read(job, "a" * 64)
