#!/usr/bin/env python3
"""Tests for escalation_manager.py — dedup, prune, and query functions."""

from datetime import UTC, datetime, timedelta

from escalation_manager import deduplicate_escalations, get_existing_source_ids, prune_resolved


def _make_escalation(
    source="email", source_id="t1", created_at=None, resolved_at=None, surfaced_at=None
):
    return {
        "id": f"{source}-{source_id}",
        "source": source,
        "sourceId": source_id,
        "reason": "test",
        "summary": "test escalation",
        "urgency": "medium",
        "handled": False,
        "createdAt": created_at or datetime.now(UTC).isoformat(),
        "surfacedAt": surfaced_at,
        "resolvedAt": resolved_at,
    }


class TestDeduplicateEscalations:
    def test_no_duplicates_unchanged(self):
        handoff = {
            "escalations": [
                _make_escalation("email", "t1"),
                _make_escalation("calendar", "e1"),
            ]
        }
        deduplicate_escalations(handoff)
        assert len(handoff["escalations"]) == 2

    def test_removes_duplicate_keeps_earliest(self):
        early = _make_escalation("email", "t1", created_at="2026-02-17T10:00:00+00:00")
        late = _make_escalation("email", "t1", created_at="2026-02-17T12:00:00+00:00")
        handoff = {"escalations": [late, early]}
        deduplicate_escalations(handoff)
        assert len(handoff["escalations"]) == 1
        assert handoff["escalations"][0]["createdAt"] == "2026-02-17T10:00:00+00:00"

    def test_empty_escalations(self):
        handoff = {"escalations": []}
        deduplicate_escalations(handoff)
        assert handoff["escalations"] == []

    def test_no_escalations_key(self):
        handoff = {}
        deduplicate_escalations(handoff)
        # Should not raise

    def test_keeps_entries_with_none_source_id(self):
        handoff = {
            "escalations": [
                {"source": None, "sourceId": None, "createdAt": "2026-01-01"},
                {"source": None, "sourceId": None, "createdAt": "2026-01-02"},
            ]
        }
        deduplicate_escalations(handoff)
        assert len(handoff["escalations"]) == 2

    def test_different_sources_same_source_id(self):
        handoff = {
            "escalations": [
                _make_escalation("email", "id1"),
                _make_escalation("calendar", "id1"),
            ]
        }
        deduplicate_escalations(handoff)
        assert len(handoff["escalations"]) == 2


class TestPruneResolved:
    def test_prunes_old_resolved(self):
        old_resolved = _make_escalation(
            "email",
            "t1",
            resolved_at=(datetime.now(UTC) - timedelta(hours=25)).isoformat(),
        )
        handoff = {"escalations": [old_resolved]}
        prune_resolved(handoff)
        assert len(handoff["escalations"]) == 0

    def test_keeps_recently_resolved(self):
        recent_resolved = _make_escalation(
            "email",
            "t1",
            resolved_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        )
        handoff = {"escalations": [recent_resolved]}
        prune_resolved(handoff)
        assert len(handoff["escalations"]) == 1

    def test_keeps_unresolved(self):
        unresolved = _make_escalation("email", "t1", resolved_at=None)
        handoff = {"escalations": [unresolved]}
        prune_resolved(handoff)
        assert len(handoff["escalations"]) == 1

    def test_custom_max_age(self):
        resolved_5h_ago = _make_escalation(
            "email",
            "t1",
            resolved_at=(datetime.now(UTC) - timedelta(hours=5)).isoformat(),
        )
        handoff = {"escalations": [resolved_5h_ago]}
        prune_resolved(handoff, max_age_hours=4)
        assert len(handoff["escalations"]) == 0

    def test_empty_escalations(self):
        handoff = {"escalations": []}
        prune_resolved(handoff)
        assert handoff["escalations"] == []

    def test_mixed_resolved_and_unresolved(self):
        old_resolved = _make_escalation(
            "email",
            "t1",
            resolved_at=(datetime.now(UTC) - timedelta(hours=48)).isoformat(),
        )
        unresolved = _make_escalation("email", "t2", resolved_at=None)
        recent_resolved = _make_escalation(
            "email",
            "t3",
            resolved_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        )
        handoff = {"escalations": [old_resolved, unresolved, recent_resolved]}
        prune_resolved(handoff)
        assert len(handoff["escalations"]) == 2
        ids = {e["sourceId"] for e in handoff["escalations"]}
        assert ids == {"t2", "t3"}


class TestGetExistingSourceIds:
    def test_returns_active_source_ids(self):
        handoff = {
            "escalations": [
                _make_escalation("email", "t1", resolved_at=None),
                _make_escalation("email", "t2", resolved_at="2026-02-17T10:00:00+00:00"),
                _make_escalation("calendar", "e1", resolved_at=None),
            ]
        }
        result = get_existing_source_ids(handoff)
        assert result == {"t1", "e1"}

    def test_empty_escalations(self):
        result = get_existing_source_ids({"escalations": []})
        assert result == set()

    def test_no_escalations_key(self):
        result = get_existing_source_ids({})
        assert result == set()

    def test_skips_null_source_ids(self):
        handoff = {
            "escalations": [
                {"sourceId": None, "resolvedAt": None},
                _make_escalation("email", "t1", resolved_at=None),
            ]
        }
        result = get_existing_source_ids(handoff)
        assert result == {"t1"}
