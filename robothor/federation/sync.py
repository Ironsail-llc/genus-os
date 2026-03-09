"""Event journal and sync protocol — three-channel sync with hybrid logical clocks."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from robothor.federation.models import (
    ENTITY_CONFLICT_MAP,
    HLC,
    TASK_STATUS_ORDER,
    ConflictStrategy,
    SyncChannel,
    SyncEvent,
)

logger = logging.getLogger(__name__)


class EventJournal:
    """Append-only event journal for federation sync.

    Events are written locally and published to NATS when a connection is active.
    Three independent channels with separate cursors allow priority-based sync.
    """

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self._clock = HLC(instance_id=instance_id)

    @property
    def clock(self) -> HLC:
        return self._clock

    def append(
        self,
        connection_id: str,
        channel: SyncChannel,
        event_type: str,
        payload: dict[str, Any],
    ) -> SyncEvent:
        """Append an event to the journal and persist it."""
        self._clock = self._clock.tick()

        event = SyncEvent(
            connection_id=connection_id,
            channel=channel,
            event_type=event_type,
            payload=payload,
            hlc_timestamp=self._clock.to_string(),
            created_at=datetime.now(UTC).isoformat(),
        )

        # Persist to database
        _persist_event(event)
        return event

    def get_unsynced(
        self,
        connection_id: str,
        channel: SyncChannel | None = None,
        limit: int = 100,
    ) -> list[SyncEvent]:
        """Get unsynced events for a connection, optionally filtered by channel."""
        return _load_unsynced_events(connection_id, channel, limit)

    def mark_synced(self, event_ids: list[int]) -> int:
        """Mark events as synced. Returns count of events marked."""
        if not event_ids:
            return 0
        return _mark_events_synced(event_ids)

    def receive(
        self,
        connection_id: str,
        remote_event: SyncEvent,
        remote_hlc: HLC,
    ) -> SyncEvent | None:
        """Process a received event from a remote peer.

        Merges the HLC, applies conflict resolution, and persists if accepted.
        Returns the event if accepted, None if rejected by conflict resolution.
        """
        # Merge clocks
        self._clock = self._clock.merge(remote_hlc)

        # Apply conflict resolution
        entity_type = _extract_entity_type(remote_event.event_type)
        strategy = ENTITY_CONFLICT_MAP.get(entity_type, ConflictStrategy.APPEND_ONLY)

        if not _resolve_conflict(strategy, remote_event):
            logger.debug(
                "Event rejected by conflict resolution: %s (strategy=%s)",
                remote_event.event_type,
                strategy,
            )
            return None

        # Persist the received event (already synced)
        remote_event.synced_at = datetime.now(UTC).isoformat()
        _persist_event(remote_event)
        return remote_event

    def get_sync_watermark(self, connection_id: str, channel: SyncChannel) -> str:
        """Get the HLC timestamp of the last synced event for a connection+channel."""
        return _get_watermark(connection_id, channel)


def _extract_entity_type(event_type: str) -> str:
    """Extract entity type from event type string (e.g. 'task.created' → 'task')."""
    return event_type.split(".")[0] if "." in event_type else event_type


def _resolve_conflict(strategy: ConflictStrategy, event: SyncEvent) -> bool:
    """Apply conflict resolution strategy. Returns True if event should be accepted."""
    if strategy == ConflictStrategy.NO_CONFLICT:
        return True

    if strategy == ConflictStrategy.APPEND_ONLY:
        return True

    if strategy == ConflictStrategy.ADDITIVE_MERGE:
        # Memory facts: always accept additions, accept deactivation (monotonic)
        action = event.payload.get("action", "")
        return action in ("create", "deactivate", "add")

    if strategy == ConflictStrategy.MONOTONIC_LATTICE:
        # Tasks: only accept if new status >= current status
        new_status = event.payload.get("status", "")
        current_status = event.payload.get("current_status", "")
        new_order = TASK_STATUS_ORDER.get(new_status, -1)
        current_order = TASK_STATUS_ORDER.get(current_status, -1)
        return new_order >= current_order

    if strategy == ConflictStrategy.AUTHORITY:
        # Config: exporting instance is authoritative — always accept
        return True

    return True  # default: accept


# ── Database persistence ──────────────────────────────────────────────


def _persist_event(event: SyncEvent) -> None:
    """Write an event to the federation_events table."""
    try:
        from robothor.db.connection import get_connection

        with get_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                INSERT INTO federation_events
                    (connection_id, channel, event_type, payload, hlc_timestamp, synced_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    event.connection_id,
                    event.channel.value,
                    event.event_type,
                    json.dumps(event.payload),
                    event.hlc_timestamp,
                    event.synced_at,
                ),
            )
            row = cur.fetchone()
            if row:
                event.id = row[0]
            db.commit()
    except Exception as e:
        logger.warning("Failed to persist sync event: %s", e)


def _load_unsynced_events(
    connection_id: str,
    channel: SyncChannel | None,
    limit: int,
) -> list[SyncEvent]:
    """Load unsynced events from the database."""
    try:
        from robothor.db.connection import get_connection

        with get_connection() as db:
            cur = db.cursor()
            if channel:
                cur.execute(
                    """
                    SELECT id, connection_id, channel, event_type, payload,
                           hlc_timestamp, synced_at, created_at
                    FROM federation_events
                    WHERE connection_id = %s AND channel = %s AND synced_at IS NULL
                    ORDER BY id
                    LIMIT %s
                    """,
                    (connection_id, channel.value, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, connection_id, channel, event_type, payload,
                           hlc_timestamp, synced_at, created_at
                    FROM federation_events
                    WHERE connection_id = %s AND synced_at IS NULL
                    ORDER BY id
                    LIMIT %s
                    """,
                    (connection_id, limit),
                )
            rows = cur.fetchall()

        return [
            SyncEvent(
                id=row[0],
                connection_id=row[1],
                channel=SyncChannel(row[2]),
                event_type=row[3],
                payload=json.loads(row[4]) if isinstance(row[4], str) else row[4],
                hlc_timestamp=row[5],
                synced_at=str(row[6]) if row[6] else None,
                created_at=str(row[7]) if row[7] else "",
            )
            for row in rows
        ]
    except Exception as e:
        logger.warning("Failed to load unsynced events: %s", e)
        return []


def _mark_events_synced(event_ids: list[int]) -> int:
    """Mark events as synced in the database."""
    try:
        from robothor.db.connection import get_connection

        now = datetime.now(UTC).isoformat()
        with get_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                UPDATE federation_events
                SET synced_at = %s
                WHERE id = ANY(%s) AND synced_at IS NULL
                """,
                (now, event_ids),
            )
            count: int = cur.rowcount
            db.commit()
        return count
    except Exception as e:
        logger.warning("Failed to mark events synced: %s", e)
        return 0


def _get_watermark(connection_id: str, channel: SyncChannel) -> str:
    """Get the HLC of the last synced event."""
    try:
        from robothor.db.connection import get_connection

        with get_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT hlc_timestamp FROM federation_events
                WHERE connection_id = %s AND channel = %s AND synced_at IS NOT NULL
                ORDER BY id DESC LIMIT 1
                """,
                (connection_id, channel.value),
            )
            row = cur.fetchone()
        return row[0] if row else ""
    except Exception as e:
        logger.warning("Failed to get watermark: %s", e)
        return ""
