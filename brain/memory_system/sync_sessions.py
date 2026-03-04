#!/usr/bin/env python3
"""
Sync Moltbot session history to PostgreSQL audit/memory system.

Reads recent messages and logs any that haven't been captured yet.
Run periodically via cron.
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Import from memory_service
sys.path.insert(0, str(Path(__file__).parent))
from memory_service import log_event

MOLTBOT_MEMORY = Path.home() / ".moltbot" / "memory" / "main.sqlite"
STATE_FILE = Path(__file__).parent / "sync_state.json"


def load_state():
    """Load last synced message ID."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_sync": None, "synced_ids": []}


def save_state(state):
    """Save sync state."""
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_recent_chunks(limit=100):
    """Get recent chunks from Moltbot's SQLite."""
    if not MOLTBOT_MEMORY.exists():
        print(f"Moltbot memory not found: {MOLTBOT_MEMORY}")
        return []

    conn = sqlite3.connect(MOLTBOT_MEMORY)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, path, source, text, start_line, end_line, updated_at
        FROM chunks
        ORDER BY updated_at DESC
        LIMIT ?
    """,
        (limit,),
    )

    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def sync():
    """Sync recent session content to PostgreSQL."""
    state = load_state()
    synced_ids = set(state.get("synced_ids", []))

    chunks = get_recent_chunks(200)
    new_count = 0

    for chunk in chunks:
        chunk_id = chunk["id"]
        if chunk_id in synced_ids:
            continue

        # Determine content type from path
        path = chunk.get("path", "")
        content_type = "memory"
        if "email" in path.lower():
            content_type = "email"
        elif "task" in path.lower():
            content_type = "task"
        elif "calendar" in path.lower():
            content_type = "calendar"

        # Log to audit
        try:
            log_event(
                event_type="memory_sync",
                action=chunk["text"][:500],
                category=content_type,
                details={
                    "chunk_id": chunk_id,
                    "path": path,
                    "source": chunk.get("source"),
                    "full_text": chunk["text"],
                },
                source_channel="moltbot",
            )
            synced_ids.add(chunk_id)
            new_count += 1
        except Exception as e:
            print(f"Error syncing {chunk_id}: {e}")

    # Update state
    state["last_sync"] = datetime.now().isoformat()
    state["synced_ids"] = list(synced_ids)[-500:] if len(synced_ids) > 500 else list(synced_ids)
    save_state(state)

    print(f"Synced {new_count} new chunks. Total tracked: {len(synced_ids)}")
    return new_count


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        chunks = get_recent_chunks(20)
        for c in chunks:
            print(f"[{c['updated_at']}] {c['path']}: {c['text'][:80]}...")
    else:
        sync()


if __name__ == "__main__":
    main()
