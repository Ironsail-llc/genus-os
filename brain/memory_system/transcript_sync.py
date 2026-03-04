#!/usr/bin/env python3
"""
Sync Moltbot session transcripts to PostgreSQL audit system.

Reads JSONL session files and logs user/assistant messages to audit_log
with vector embeddings for semantic search.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Import from memory_service
sys.path.insert(0, str(Path(__file__).parent))
from memory_service import log_event, store_memory

SESSIONS_DIR = Path.home() / ".moltbot" / "agents" / "main" / "sessions"
STATE_FILE = Path(__file__).parent / "transcript_sync_state.json"


def load_state() -> dict:
    """Load sync state."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"synced_entries": {}, "last_sync": None}


def save_state(state: dict):
    """Save sync state."""
    STATE_FILE.write_text(json.dumps(state, indent=2))


def extract_text_content(content: Any) -> str | None:
    """Extract text from message content (handles string or array format)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text")
    return None


def parse_session_file(filepath: Path) -> list[dict]:
    """Parse a session JSONL file and extract messages."""
    messages = []

    try:
        with open(filepath) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Only process message entries
                if entry.get("type") != "message":
                    continue

                msg = entry.get("message", {})
                role = msg.get("role")

                # Skip tool results and system messages
                if role not in ("user", "assistant"):
                    continue

                content = extract_text_content(msg.get("content"))
                if not content:
                    continue

                # Skip very short messages and commands
                if len(content) < 10 or content.startswith("/"):
                    continue

                # Create unique ID for this entry
                entry_id = entry.get("id", f"{filepath.stem}:{line_num}")
                timestamp = entry.get("timestamp") or msg.get("timestamp")

                messages.append(
                    {
                        "id": entry_id,
                        "session_id": filepath.stem,
                        "role": role,
                        "content": content,
                        "timestamp": timestamp,
                        "line": line_num,
                        "model": msg.get("model"),
                        "provider": msg.get("provider"),
                    }
                )

    except Exception as e:
        print(f"Error parsing {filepath}: {e}", file=sys.stderr)

    return messages


def sync_transcript(
    filepath: Path, state: dict, vectorize: bool = True, verbose: bool = False
) -> int:
    """Sync a single session transcript to PostgreSQL."""
    session_id = filepath.stem
    synced_entries = state.get("synced_entries", {})
    session_synced = set(synced_entries.get(session_id, []))

    messages = parse_session_file(filepath)
    new_count = 0

    for msg in messages:
        entry_id = msg["id"]

        # Skip already synced
        if entry_id in session_synced:
            continue

        content = msg["content"]
        role = msg["role"]

        # Log to audit
        try:
            audit_result = log_event(
                event_type="conversation",
                action=content[:500],  # Truncate for audit action field
                category=role,
                details={
                    "entry_id": entry_id,
                    "session_id": session_id,
                    "full_content": content,
                    "model": msg.get("model"),
                    "provider": msg.get("provider"),
                    "original_timestamp": msg.get("timestamp"),
                },
                source_channel="moltbot",
                session_key=f"agent:main:{session_id}",
            )

            # Optionally vectorize for semantic search
            if vectorize and len(content) > 50:
                store_memory(
                    content=content,
                    content_type=f"transcript_{role}",
                    metadata={
                        "session_id": session_id,
                        "role": role,
                        "audit_id": audit_result.get("id"),
                        "original_timestamp": msg.get("timestamp"),
                    },
                    ttl_hours=None,  # Permanent
                )

            session_synced.add(entry_id)
            new_count += 1

            if verbose:
                print(f"  [{role}] {content[:60]}...")

        except Exception as e:
            print(f"Error syncing {entry_id}: {e}", file=sys.stderr)

    # Update state
    if new_count > 0:
        synced_list = list(session_synced)
        synced_entries[session_id] = synced_list[-1000:] if len(synced_list) > 1000 else synced_list
        state["synced_entries"] = synced_entries

    return new_count


def sync_all(vectorize: bool = True, verbose: bool = False, limit: int = 10) -> dict:
    """Sync all recent session transcripts."""
    state = load_state()

    if not SESSIONS_DIR.exists():
        print(f"Sessions directory not found: {SESSIONS_DIR}")
        return {"error": "Sessions directory not found"}

    # Get session files sorted by modification time (newest first)
    session_files = sorted(
        SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:limit]

    total_new = 0
    synced_sessions = []

    for filepath in session_files:
        if verbose:
            print(f"\nSyncing {filepath.name}...")

        new_count = sync_transcript(filepath, state, vectorize, verbose)
        total_new += new_count

        if new_count > 0:
            synced_sessions.append({"session": filepath.stem, "new_entries": new_count})

    # Update and save state
    state["last_sync"] = datetime.now().isoformat()
    save_state(state)

    result = {
        "total_new": total_new,
        "sessions_processed": len(session_files),
        "sessions_with_new": synced_sessions,
        "last_sync": state["last_sync"],
    }

    print(f"\nSynced {total_new} new transcript entries from {len(session_files)} sessions")
    return result


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Sync Moltbot transcripts to PostgreSQL")
    parser.add_argument("--no-vectorize", action="store_true", help="Skip vectorization")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--limit", type=int, default=10, help="Max sessions to process")
    parser.add_argument("--stats", action="store_true", help="Show sync stats")
    args = parser.parse_args()

    if args.stats:
        state = load_state()
        total_synced = sum(len(v) for v in state.get("synced_entries", {}).values())
        print(f"Total synced entries: {total_synced}")
        print(f"Sessions tracked: {len(state.get('synced_entries', {}))}")
        print(f"Last sync: {state.get('last_sync', 'Never')}")
        return

    result = sync_all(vectorize=not args.no_vectorize, verbose=args.verbose, limit=args.limit)

    if args.verbose:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
