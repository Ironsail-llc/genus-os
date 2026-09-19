"""Complete revision manifests make mature retention measurable without guessing."""

import hashlib
import json
from datetime import UTC, datetime

import pytest

from robothor.sales.tests.test_business import business as business
from robothor.sales.tests.test_business import order, practice, prospect


def fingerprint(members):
    return hashlib.sha256(json.dumps(sorted(members), separators=(",", ":")).encode()).hexdigest()


def page(
    sales, business, members, *, coverage=None, after=None, next_cursor=None, scan="scan-coverage"
):
    payload = {
        "source": "orders_app",
        "account_id": "account-1",
        "kind": "order",
        "practice_id": "practice-1",
        "after": after,
        "seen_cursors": [after] if after else [],
        "scan_id": scan,
    }
    if after is None:
        sales.ops.enqueue("sales.business", f"{scan}:{after}", payload)
    job = sales.ops.claim("sales.business")
    business.commit_page(
        job,
        {
            **{k: v for k, v in payload.items() if k not in ("scan_id", "seen_cursors")},
            "observed_at": "2026-09-15T00:00:00Z",
            "next_cursor": next_cursor,
            "items": members,
            "coverage": coverage,
        },
    )


def fixture(sales, business):
    p = prospect(sales)
    identity = practice(business)
    business.bind(p, identity, "v1", "operator:test", "Reviewed complete business identity")
    order(business, at=datetime(2026, 9, 14, tzinfo=UTC))
    row = business.records(kind="order")["items"][0]
    return p, {"external_id": row["external_id"], "revision": row["revision"], "data": row["data"]}


def manifest(items, **changes):
    return dict(
        fingerprint=fingerprint([[i["external_id"], i["revision"]] for i in items]),
        total=len(items),
        through="2026-09-15T00:00:00Z",
        **changes,
    )


def test_complete_manifest_unlocks_only_windows_mature_at_observation(sales, business):
    p, item = fixture(sales, business)
    page(sales, business, [item], coverage=manifest([item]))
    metrics = business.retention(p, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert metrics["coverage_complete"] is True
    assert metrics["coverage_through"] == "2026-09-15T00:00:00+00:00"
    assert metrics["repeat_within_30_days"] is False
    assert metrics["active_days_31_60"] is False
    assert metrics["active_days_61_90"] is None


@pytest.mark.parametrize("failure", ["missing", "changed", "unknown", "correction"])
def test_incomplete_or_changed_history_never_becomes_negative_retention(sales, business, failure):
    p, item = fixture(sales, business)
    proof = manifest([item])
    if failure == "missing":
        proof["total"] = 2
    if failure == "changed":
        proof["fingerprint"] = "a" * 64
    if failure == "unknown":
        item["data"]["fulfillment"] = "unknown"
        item["data"]["fulfilled_at"] = None
        item["revision"] = "v2"
        proof = manifest([item])
    page(sales, business, [item], coverage=proof)
    if failure == "correction":
        order(business, state="excluded", revision="v2")
    metrics = business.retention(p)
    assert metrics["coverage_complete"] is False
    assert metrics["repeat_within_30_days"] is None


def test_empty_complete_snapshot_excludes_stale_observations_without_deleting_evidence(
    sales, business
):
    p, item = fixture(sales, business)
    page(sales, business, [], coverage=manifest([]))
    metrics = business.retention(p)
    assert metrics["coverage_complete"] is True
    assert metrics["completed_orders"] == 0
    assert len(business.records(kind="order")["items"]) == 1


@pytest.mark.parametrize("changed", [False, True])
def test_multi_page_restart_requires_the_same_complete_manifest(sales, business, changed):
    from copy import deepcopy

    from robothor.sales.business import BusinessObservations

    p, first = fixture(sales, business)
    second = deepcopy(first)
    second["external_id"] = "order-2"
    second["data"]["fulfilled_at"] = "2026-07-20T00:00:00Z"
    proof = manifest([first, second])
    page(sales, business, [first], coverage=proof, next_cursor="next-1")
    assert business.retention(p)["coverage_complete"] is False
    if changed:
        proof["fingerprint"] = "a" * 64
    restarted = BusinessObservations(sales)
    page(sales, restarted, [second], coverage=proof, after="next-1")
    result = restarted.retention(p)
    assert result["coverage_complete"] is not changed
    assert result["repeat_within_30_days"] is (None if changed else True)


def test_new_observation_after_complete_scan_withdraws_coverage(sales, business):
    p, item = fixture(sales, business)
    page(sales, business, [item], coverage=manifest([item]))
    order(business, identity="new-order")
    assert business.retention(p)["coverage_complete"] is False
