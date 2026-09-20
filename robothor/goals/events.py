"""Replay existing Redis streams into the durable goal inbox.

Cursor and inbox entries commit together. Lost/trimmed stream history is covered
by each watch's fallback review. Unscoped events never cross tenant boundaries.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from psycopg2.extras import Json

from robothor.goals.store import transaction

logger = logging.getLogger(__name__)


def capture(tenant: str) -> None:
    from robothor.events.bus import VALID_STREAMS, _get_redis, _stream_key

    client = _get_redis()
    if client is None:
        return
    with transaction() as cur:
        cur.execute(
            "SELECT cursors,updated_at FROM goal_pursuit_settings WHERE tenant_id=%s AND enabled FOR UPDATE",
            (tenant,),
        )
        row = cur.fetchone()
        if not row:
            return
        cursors = row["cursors"] or {}
        # Only the event types some goal is actually waiting on are stored.
        # This used to copy all eight streams into PostgreSQL every sixty
        # seconds whether or not anything could ever match, and nothing
        # deleted a row afterwards. Cursors still advance across every stream,
        # so a watch registered later starts from now rather than replaying a
        # backlog — which is the existing contract: a wait cannot be satisfied
        # by an event older than its registration.
        cur.execute(
            """SELECT DISTINCT data->'wait'->>'event_type' AS event_type FROM pursuit_goals
                       WHERE tenant_id=%s AND status='waiting'
                         AND data->'wait'->>'event_type' IS NOT NULL""",
            (tenant,),
        )
        awaited = {r["event_type"] for r in cur.fetchall()}
        for stream in sorted(VALID_STREAMS):
            cursor = cursors.get(stream, f"{int(row['updated_at'].timestamp() * 1000)}-0")
            entries = client.xrange(_stream_key(stream), min="(" + cursor, max="+", count=1000)
            for event_id, raw in entries:
                event_id = event_id.decode() if isinstance(event_id, bytes) else event_id
                envelope = {
                    (k.decode() if isinstance(k, bytes) else k): (
                        v.decode() if isinstance(v, bytes) else v
                    )
                    for k, v in raw.items()
                }
                if envelope.get("tenant_id") == tenant and envelope.get("type", "") in awaited:
                    try:
                        payload = json.loads(envelope.get("payload", "{}"))
                        if not isinstance(payload, dict):
                            raise ValueError("event payload is not an object")
                        timestamp = datetime.fromisoformat(envelope["timestamp"])
                        if timestamp.tzinfo is None:
                            raise ValueError("event timestamp has no timezone")
                        cur.execute(
                            """INSERT INTO pursuit_goal_events(tenant_id,id,event_type,payload,created_at)
                                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (
                                tenant,
                                f"{stream}:{event_id}",
                                envelope.get("type", ""),
                                Json(payload),
                                timestamp,
                            ),
                        )
                    except (ValueError, KeyError):
                        logger.warning("Skipping malformed goal event %s:%s", stream, event_id)
                cursors[stream] = event_id
        cur.execute(
            "UPDATE goal_pursuit_settings SET cursors=%s WHERE tenant_id=%s",
            (Json(cursors), tenant),
        )
