"""Operator read-recovery inventory remains tenant scoped and paginated."""

import pytest

from robothor.operations.store import Conflict


def test_read_inventory_excludes_writes_and_returns_only_recovery_metadata(sales):
    failed = sales.ops.enqueue(
        "sales.business",
        "read",
        {
            "source": "example",
            "account_id": "account",
            "kind": "practice",
            "private": "not-for-console",
        },
    )
    sales.ops.enqueue("sales.email", "send", {})
    sales.ops.enqueue("unrelated.task", "other", {})
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET status='failed',error='Read failed' WHERE id=%s", (failed,)
        )
    page = sales.provider_reads()
    assert len(page["items"]) == 1
    row = page["items"][0]
    assert str(row["id"]) == failed
    assert row["scope"] == {"source": "example", "account_id": "account", "kind": "practice"}
    assert "payload" not in row and "lease_token" not in row
    assert "private" not in str(row)
    sales.retry_provider_read(failed, "operator:test", "Repaired source configuration")
    assert sales.provider_reads()["items"] == []
    assert len(sales.provider_reads(state="all")["items"]) == 1


def test_read_inventory_keyset_is_bounded_and_rejects_foreign_cursors(sales):
    from robothor.sales.service import Sales

    for n in range(102):
        sales.ops.enqueue("sales.reconcile", f"page-{n}", {})
    first = sales.provider_reads(state="all")
    assert len(first["items"]) == 100 and first["next_cursor"]
    second = sales.provider_reads(state="all", after=first["next_cursor"])
    assert len(second["items"]) == 2 and second["next_cursor"] is None
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
    with pytest.raises(Conflict):
        Sales("foreign-tenant").provider_reads(state="all", after=first["next_cursor"])
    with pytest.raises(ValueError):
        sales.provider_reads(kind="sales.email")
