"""Tests for federation data models — HLC ordering, conflict resolution, defaults."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.federation.models import (
    CHILD_DEFAULT_EXPORTS,
    ENTITY_CONFLICT_MAP,
    HLC,
    PARENT_DEFAULT_EXPORTS,
    TASK_STATUS_ORDER,
    ConflictStrategy,
    Connection,
    ConnectionState,
    Instance,
    InviteToken,
    Relationship,
    SyncChannel,
    SyncEvent,
    default_exports_for,
)

# ── HLC Tests ──────────────────────────────────────────────────────────


class TestHLC:
    def test_tick_advances_wall_ms(self):
        """Tick uses real wall time when it's ahead of current wall_ms."""
        hlc = HLC(wall_ms=100, counter=5, instance_id="a")
        with patch("robothor.federation.models._now_ms", return_value=200):
            ticked = hlc.tick()
        assert ticked.wall_ms == 200
        assert ticked.counter == 0
        assert ticked.instance_id == "a"

    def test_tick_increments_counter_when_clock_stalls(self):
        """Tick increments counter when wall time hasn't advanced."""
        hlc = HLC(wall_ms=200, counter=3, instance_id="a")
        with patch("robothor.federation.models._now_ms", return_value=200):
            ticked = hlc.tick()
        assert ticked.wall_ms == 200
        assert ticked.counter == 4

    def test_tick_increments_counter_when_wall_behind(self):
        """If now_ms < wall_ms (clock skew), counter increments."""
        hlc = HLC(wall_ms=300, counter=2, instance_id="a")
        with patch("robothor.federation.models._now_ms", return_value=100):
            ticked = hlc.tick()
        assert ticked.wall_ms == 300
        assert ticked.counter == 3

    def test_merge_uses_max_wall_time(self):
        """Merge picks the maximum wall time from local, remote, and now."""
        local = HLC(wall_ms=100, counter=2, instance_id="a")
        remote = HLC(wall_ms=200, counter=5, instance_id="b")
        with patch("robothor.federation.models._now_ms", return_value=150):
            merged = local.merge(remote)
        assert merged.wall_ms == 200
        # Remote has the max wall, so counter = remote.counter + 1
        assert merged.counter == 6
        assert merged.instance_id == "a"

    def test_merge_now_ahead_resets_counter(self):
        """If now_ms is strictly ahead of both, counter resets to 0."""
        local = HLC(wall_ms=100, counter=2, instance_id="a")
        remote = HLC(wall_ms=150, counter=3, instance_id="b")
        with patch("robothor.federation.models._now_ms", return_value=300):
            merged = local.merge(remote)
        assert merged.wall_ms == 300
        assert merged.counter == 0

    def test_merge_all_equal_wall(self):
        """When all three wall times are equal, take max(counter) + 1."""
        local = HLC(wall_ms=200, counter=3, instance_id="a")
        remote = HLC(wall_ms=200, counter=7, instance_id="b")
        with patch("robothor.federation.models._now_ms", return_value=200):
            merged = local.merge(remote)
        assert merged.wall_ms == 200
        assert merged.counter == 8  # max(3, 7) + 1

    def test_merge_local_wins_wall(self):
        """When local wall_ms is max, counter = local.counter + 1."""
        local = HLC(wall_ms=300, counter=5, instance_id="a")
        remote = HLC(wall_ms=100, counter=2, instance_id="b")
        with patch("robothor.federation.models._now_ms", return_value=200):
            merged = local.merge(remote)
        assert merged.wall_ms == 300
        assert merged.counter == 6

    def test_to_string_and_from_string_roundtrip(self):
        hlc = HLC(wall_ms=1234567890, counter=42, instance_id="test-node")
        s = hlc.to_string()
        assert s == "1234567890:42:test-node"
        parsed = HLC.from_string(s)
        assert parsed.wall_ms == hlc.wall_ms
        assert parsed.counter == hlc.counter
        assert parsed.instance_id == hlc.instance_id

    def test_from_string_with_colons_in_instance_id(self):
        """Instance IDs may contain colons (UUIDs don't, but be robust)."""
        s = "100:5:node:with:colons"
        parsed = HLC.from_string(s)
        assert parsed.wall_ms == 100
        assert parsed.counter == 5
        assert parsed.instance_id == "node:with:colons"

    def test_from_string_invalid(self):
        with pytest.raises(ValueError, match="Invalid HLC string"):
            HLC.from_string("not-a-valid-hlc")

    def test_lt_by_wall(self):
        a = HLC(wall_ms=100, counter=0, instance_id="a")
        b = HLC(wall_ms=200, counter=0, instance_id="a")
        assert a < b
        assert not b < a

    def test_lt_by_counter(self):
        a = HLC(wall_ms=100, counter=1, instance_id="a")
        b = HLC(wall_ms=100, counter=2, instance_id="a")
        assert a < b

    def test_lt_by_instance_id(self):
        """Tiebreak by instance ID (lexicographic)."""
        a = HLC(wall_ms=100, counter=1, instance_id="alpha")
        b = HLC(wall_ms=100, counter=1, instance_id="beta")
        assert a < b

    def test_sorting_multiple_hlcs(self):
        clocks = [
            HLC(wall_ms=200, counter=0, instance_id="c"),
            HLC(wall_ms=100, counter=0, instance_id="a"),
            HLC(wall_ms=100, counter=1, instance_id="a"),
            HLC(wall_ms=100, counter=1, instance_id="b"),
        ]
        sorted_clocks = sorted(clocks)
        assert [c.to_string() for c in sorted_clocks] == [
            "100:0:a",
            "100:1:a",
            "100:1:b",
            "200:0:c",
        ]


# ── Enum Tests ─────────────────────────────────────────────────────────


class TestEnums:
    def test_connection_state_values(self):
        assert ConnectionState.PENDING.value == "pending"
        assert ConnectionState.ACTIVE.value == "active"
        assert ConnectionState.LIMITED.value == "limited"
        assert ConnectionState.SUSPENDED.value == "suspended"

    def test_relationship_values(self):
        assert Relationship.PARENT.value == "parent"
        assert Relationship.CHILD.value == "child"
        assert Relationship.PEER.value == "peer"

    def test_sync_channel_values(self):
        assert SyncChannel.CRITICAL.value == "critical"
        assert SyncChannel.BULK.value == "bulk"
        assert SyncChannel.MEDIA.value == "media"

    def test_conflict_strategy_values(self):
        assert ConflictStrategy.NO_CONFLICT.value == "no_conflict"
        assert ConflictStrategy.MONOTONIC_LATTICE.value == "monotonic_lattice"
        assert ConflictStrategy.ADDITIVE_MERGE.value == "additive_merge"
        assert ConflictStrategy.AUTHORITY.value == "authority"
        assert ConflictStrategy.APPEND_ONLY.value == "append_only"


# ── Default Exports ────────────────────────────────────────────────────


class TestDefaultExports:
    def test_parent_defaults(self):
        exports = default_exports_for(Relationship.PARENT)
        assert exports == PARENT_DEFAULT_EXPORTS
        # Returns a copy, not the original list
        exports.append("extra")
        assert "extra" not in PARENT_DEFAULT_EXPORTS

    def test_child_defaults(self):
        exports = default_exports_for(Relationship.CHILD)
        assert exports == CHILD_DEFAULT_EXPORTS

    def test_peer_defaults_empty(self):
        exports = default_exports_for(Relationship.PEER)
        assert exports == []


# ── Conflict Maps ──────────────────────────────────────────────────────


class TestConflictMaps:
    def test_entity_conflict_map_coverage(self):
        assert ENTITY_CONFLICT_MAP["agent_run"] == ConflictStrategy.NO_CONFLICT
        assert ENTITY_CONFLICT_MAP["task"] == ConflictStrategy.MONOTONIC_LATTICE
        assert ENTITY_CONFLICT_MAP["memory_fact"] == ConflictStrategy.ADDITIVE_MERGE
        assert ENTITY_CONFLICT_MAP["config"] == ConflictStrategy.AUTHORITY
        assert ENTITY_CONFLICT_MAP["log"] == ConflictStrategy.APPEND_ONLY
        assert ENTITY_CONFLICT_MAP["telemetry"] == ConflictStrategy.APPEND_ONLY

    def test_task_status_order_monotonic(self):
        statuses = sorted(TASK_STATUS_ORDER.keys(), key=lambda s: TASK_STATUS_ORDER[s])
        assert statuses == ["open", "in_progress", "done", "archived"]


# ── Dataclass Defaults ─────────────────────────────────────────────────


class TestDataclassDefaults:
    def test_connection_defaults(self):
        conn = Connection()
        assert conn.relationship == Relationship.PEER
        assert conn.state == ConnectionState.PENDING
        assert conn.exports == []
        assert conn.imports == []
        assert conn.metadata == {}
        assert conn.id  # UUID generated

    def test_instance_defaults(self):
        inst = Instance()
        assert inst.id  # UUID generated
        assert inst.display_name == ""

    def test_sync_event_defaults(self):
        event = SyncEvent()
        assert event.id == 0
        assert event.channel == SyncChannel.CRITICAL
        assert event.payload == {}
        assert event.synced_at is None

    def test_invite_token_defaults(self):
        token = InviteToken()
        assert token.relationship == Relationship.PEER

    def test_connection_ids_unique(self):
        """Each Connection() generates a different UUID."""
        a = Connection()
        b = Connection()
        assert a.id != b.id
