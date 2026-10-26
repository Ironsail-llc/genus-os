"""150: ``pr_reviews.requested_at`` — the review queue's order.

``list_pending`` sorted by ``updated_at``, which moves whenever a row is
touched for any reason, so a request could lose its place to a newer one. The
queue is ordered by when the request was made; rows from before this column
(no time) go last.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION = "150_pr_review_requested_at"
TENANT = "tenant-under-test"


def test_migration_file_exists_and_is_in_the_manifest():
    assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists()
    manifest = (Path(__file__).resolve().parents[1] / "migrations" / "manifest.txt").read_text()
    assert f"crm/{MIGRATION}.sql" in manifest.splitlines()


@pytest.mark.integration
def test_pending_rows_come_back_in_request_order(scratch_db, monkeypatch):
    import psycopg2

    from robothor.pr_review.store import PgStore, PrReviewRow

    _db, dsn = scratch_db(through=MIGRATION)

    @contextlib.contextmanager
    def connect(**_kw):
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr("robothor.db.connection.get_connection", connect)
    store = PgStore()
    t0 = datetime(2026, 10, 13, 11, 0, tzinfo=UTC)

    async def scenario() -> list[int]:
        for number, requested in ((1, t0), (2, t0 + timedelta(minutes=1)), (3, None)):
            row = PrReviewRow(
                tenant_id=TENANT,
                repo="acme/widgets",
                number=number,
                pending_trigger="rereview",
                requested_at=requested,
            )
            await store.save(row)
        first = await store.get(TENANT, "acme/widgets", 1)
        assert first is not None and first.requested_at == t0
        first.title = "touched last"
        await store.save(first)
        return [r.number for r in await store.list_pending(TENANT)]

    assert asyncio.run(scenario()) == [1, 2, 3]


@pytest.mark.integration
def test_the_report_reads_every_row_and_the_replies_given(scratch_db, monkeypatch):
    import psycopg2

    from robothor.pr_review.store import PgStore, PrReviewRow

    _db, dsn = scratch_db(through=MIGRATION)

    @contextlib.contextmanager
    def connect(**_kw):
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr("robothor.db.connection.get_connection", connect)
    store = PgStore()

    async def scenario() -> tuple[list[int], list[str]]:
        for number in (1, 2):
            await store.save(PrReviewRow(tenant_id=TENANT, repo="acme/widgets", number=number))
        await store.record_message(
            TENANT, "spaces/S/messages/r1", "self", "intake reply: No changes?"
        )
        since = datetime.now(UTC) - timedelta(hours=1)
        rows = await store.list_all(TENANT)
        messages = await store.list_messages(TENANT, since)
        return sorted(r.number for r in rows), [m["outcome"] for m in messages]

    assert asyncio.run(scenario()) == ([1, 2], ["intake reply: No changes?"])
