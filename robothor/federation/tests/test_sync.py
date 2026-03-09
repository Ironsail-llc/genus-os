"""Tests for EventJournal — append/receive, HLC merge, conflict strategies."""

from __future__ import annotations

from unittest.mock import patch

from robothor.federation.models import (
    HLC,
    ConflictStrategy,
    SyncChannel,
    SyncEvent,
)
from robothor.federation.sync import (
    EventJournal,
    _extract_entity_type,
    _resolve_conflict,
)

# ── Conflict Resolution ───────────────────────────────────────────────


class TestResolveConflict:
    def _event(self, **payload_kw) -> SyncEvent:
        return SyncEvent(payload=payload_kw)

    def test_no_conflict_always_accepts(self):
        assert _resolve_conflict(ConflictStrategy.NO_CONFLICT, self._event()) is True

    def test_append_only_always_accepts(self):
        assert _resolve_conflict(ConflictStrategy.APPEND_ONLY, self._event()) is True

    def test_authority_always_accepts(self):
        assert _resolve_conflict(ConflictStrategy.AUTHORITY, self._event()) is True

    def test_additive_merge_accepts_create(self):
        assert (
            _resolve_conflict(ConflictStrategy.ADDITIVE_MERGE, self._event(action="create")) is True
        )

    def test_additive_merge_accepts_deactivate(self):
        assert (
            _resolve_conflict(ConflictStrategy.ADDITIVE_MERGE, self._event(action="deactivate"))
            is True
        )

    def test_additive_merge_accepts_add(self):
        assert _resolve_conflict(ConflictStrategy.ADDITIVE_MERGE, self._event(action="add")) is True

    def test_additive_merge_rejects_update(self):
        assert (
            _resolve_conflict(ConflictStrategy.ADDITIVE_MERGE, self._event(action="update"))
            is False
        )

    def test_additive_merge_rejects_delete(self):
        assert (
            _resolve_conflict(ConflictStrategy.ADDITIVE_MERGE, self._event(action="delete"))
            is False
        )

    def test_additive_merge_rejects_empty_action(self):
        assert _resolve_conflict(ConflictStrategy.ADDITIVE_MERGE, self._event()) is False

    def test_monotonic_lattice_accepts_forward(self):
        event = self._event(status="done", current_status="in_progress")
        assert _resolve_conflict(ConflictStrategy.MONOTONIC_LATTICE, event) is True

    def test_monotonic_lattice_accepts_same(self):
        event = self._event(status="open", current_status="open")
        assert _resolve_conflict(ConflictStrategy.MONOTONIC_LATTICE, event) is True

    def test_monotonic_lattice_rejects_backward(self):
        event = self._event(status="open", current_status="done")
        assert _resolve_conflict(ConflictStrategy.MONOTONIC_LATTICE, event) is False

    def test_monotonic_lattice_unknown_status(self):
        """Unknown statuses get order -1, so reject if current is known."""
        event = self._event(status="unknown", current_status="open")
        assert _resolve_conflict(ConflictStrategy.MONOTONIC_LATTICE, event) is False

    def test_monotonic_full_lattice_order(self):
        """Verify the full chain: open → in_progress → done → archived."""
        statuses = ["open", "in_progress", "done", "archived"]
        for i in range(len(statuses)):
            for j in range(i, len(statuses)):
                event = self._event(status=statuses[j], current_status=statuses[i])
                assert _resolve_conflict(ConflictStrategy.MONOTONIC_LATTICE, event) is True

    def test_default_strategy_accepts(self):
        """Unknown strategy falls through to default accept."""
        # We can't create an unknown strategy enum value, but the code path exists
        # Test by calling with a valid strategy that always accepts
        assert _resolve_conflict(ConflictStrategy.NO_CONFLICT, self._event()) is True


# ── Entity Type Extraction ─────────────────────────────────────────────


class TestExtractEntityType:
    def test_dotted(self):
        assert _extract_entity_type("task.created") == "task"

    def test_multi_dot(self):
        assert _extract_entity_type("memory_fact.updated.merged") == "memory_fact"

    def test_no_dot(self):
        assert _extract_entity_type("agent_run") == "agent_run"


# ── EventJournal ───────────────────────────────────────────────────────


class TestEventJournal:
    @patch("robothor.federation.sync._persist_event")
    def test_append_ticks_clock(self, mock_persist):
        journal = EventJournal("node-a")
        initial_wall = journal.clock.wall_ms

        with patch("robothor.federation.models._now_ms", return_value=initial_wall + 1000):
            event = journal.append(
                "conn-1", SyncChannel.CRITICAL, "task.created", {"task_id": "t1"}
            )

        assert event.connection_id == "conn-1"
        assert event.channel == SyncChannel.CRITICAL
        assert event.event_type == "task.created"
        assert event.payload == {"task_id": "t1"}
        assert event.hlc_timestamp
        assert journal.clock.wall_ms >= initial_wall
        mock_persist.assert_called_once_with(event)

    @patch("robothor.federation.sync._persist_event")
    def test_append_multiple_events_advance_clock(self, mock_persist):
        journal = EventJournal("node-a")
        now = 1000000

        with patch("robothor.federation.models._now_ms", return_value=now):
            e1 = journal.append("conn-1", SyncChannel.BULK, "log.entry", {"msg": "a"})
            e2 = journal.append("conn-1", SyncChannel.BULK, "log.entry", {"msg": "b"})

        hlc1 = HLC.from_string(e1.hlc_timestamp)
        hlc2 = HLC.from_string(e2.hlc_timestamp)
        assert hlc1 < hlc2  # Causal ordering preserved

    @patch("robothor.federation.sync._persist_event")
    def test_receive_accepts_valid_event(self, mock_persist):
        journal = EventJournal("node-a")
        remote_hlc = HLC(wall_ms=5000, counter=3, instance_id="node-b")

        remote_event = SyncEvent(
            connection_id="conn-1",
            channel=SyncChannel.CRITICAL,
            event_type="log.entry",  # APPEND_ONLY strategy → always accepted
            payload={"msg": "remote"},
            hlc_timestamp=remote_hlc.to_string(),
        )

        with patch("robothor.federation.models._now_ms", return_value=4000):
            result = journal.receive("conn-1", remote_event, remote_hlc)

        assert result is not None
        assert result.synced_at  # Marked as synced
        mock_persist.assert_called_once()

    @patch("robothor.federation.sync._persist_event")
    def test_receive_merges_hlc(self, mock_persist):
        journal = EventJournal("node-a")
        remote_hlc = HLC(wall_ms=9000, counter=10, instance_id="node-b")

        event = SyncEvent(
            event_type="log.x",
            payload={},
            hlc_timestamp=remote_hlc.to_string(),
        )

        with patch("robothor.federation.models._now_ms", return_value=5000):
            journal.receive("conn-1", event, remote_hlc)

        # After merge, local clock should be at least as advanced as remote
        assert journal.clock.wall_ms >= remote_hlc.wall_ms

    @patch("robothor.federation.sync._persist_event")
    def test_receive_rejects_by_conflict_resolution(self, mock_persist):
        journal = EventJournal("node-a")
        remote_hlc = HLC(wall_ms=5000, counter=0, instance_id="node-b")

        # memory_fact with action="update" → ADDITIVE_MERGE rejects
        event = SyncEvent(
            event_type="memory_fact.updated",
            payload={"action": "update"},
            hlc_timestamp=remote_hlc.to_string(),
        )

        with patch("robothor.federation.models._now_ms", return_value=5000):
            result = journal.receive("conn-1", event, remote_hlc)

        assert result is None
        mock_persist.assert_not_called()

    @patch("robothor.federation.sync._load_unsynced_events", return_value=[])
    def test_get_unsynced(self, mock_load):
        journal = EventJournal("node-a")
        result = journal.get_unsynced("conn-1", SyncChannel.CRITICAL)
        assert result == []
        mock_load.assert_called_once_with("conn-1", SyncChannel.CRITICAL, 100)

    @patch("robothor.federation.sync._mark_events_synced", return_value=3)
    def test_mark_synced(self, mock_mark):
        journal = EventJournal("node-a")
        count = journal.mark_synced([1, 2, 3])
        assert count == 3
        mock_mark.assert_called_once_with([1, 2, 3])

    def test_mark_synced_empty(self):
        journal = EventJournal("node-a")
        count = journal.mark_synced([])
        assert count == 0

    @patch("robothor.federation.sync._get_watermark", return_value="1000:5:node-a")
    def test_get_sync_watermark(self, mock_wm):
        journal = EventJournal("node-a")
        wm = journal.get_sync_watermark("conn-1", SyncChannel.CRITICAL)
        assert wm == "1000:5:node-a"
