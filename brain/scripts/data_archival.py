#!/usr/bin/env python3
"""
Data Archival Cron for Robothor.

Runs weekly on Sunday at 4:00 AM.
Pure Python — no AI, no LLM calls.

Actions:
1. Archive email-log.json entries with processedAt > 30 days
2. Prune resolved escalations > 7 days from worker-handoff.json
3. Compact rag-quality-log.json runs > 30 days
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

MEMORY_DIR = Path("/home/philip/robothor/brain/memory")
ARCHIVE_DIR = MEMORY_DIR / "archive"


def archive_old_emails() -> dict:
    """Archive email-log entries older than 30 days."""
    result = {"archived": 0, "remaining": 0}

    email_log_path = MEMORY_DIR / "email-log.json"
    if not email_log_path.exists():
        return result

    email_log = json.loads(email_log_path.read_text())
    entries = email_log.get("entries", {})
    cutoff = datetime.now() - timedelta(days=30)

    to_archive = {}
    to_keep = {}

    for entry_id, entry in entries.items():
        processed_at = entry.get("processedAt", "")
        if not processed_at:
            to_keep[entry_id] = entry
            continue

        try:
            entry_time = datetime.fromisoformat(processed_at.replace("Z", "+00:00"))
            if entry_time.tzinfo:
                entry_time = entry_time.replace(tzinfo=None)
            if entry_time < cutoff:
                to_archive[entry_id] = entry
            else:
                to_keep[entry_id] = entry
        except (ValueError, TypeError):
            to_keep[entry_id] = entry

    if not to_archive:
        result["remaining"] = len(to_keep)
        return result

    # Write archive file (grouped by month)
    ARCHIVE_DIR.mkdir(exist_ok=True)
    month = datetime.now().strftime("%Y-%m")
    archive_path = ARCHIVE_DIR / f"email-log-archive-{month}.json"

    existing_archive = {"entries": {}}
    if archive_path.exists():
        try:
            existing_archive = json.loads(archive_path.read_text())
        except (json.JSONDecodeError, ValueError):
            pass

    existing_archive.setdefault("entries", {}).update(to_archive)
    archive_path.write_text(json.dumps(existing_archive, indent=2, default=str))

    # Update original log
    email_log["entries"] = to_keep
    email_log_path.write_text(json.dumps(email_log, indent=2, default=str))

    result["archived"] = len(to_archive)
    result["remaining"] = len(to_keep)
    logger.info("Archived %d email entries (keeping %d)", len(to_archive), len(to_keep))

    return result


def prune_resolved_escalations() -> dict:
    """Remove resolved escalations older than 7 days from worker-handoff.json."""
    result = {"pruned": 0, "remaining": 0}

    handoff_path = MEMORY_DIR / "worker-handoff.json"
    if not handoff_path.exists():
        return result

    handoff = json.loads(handoff_path.read_text())
    escalations = handoff.get("escalations", [])
    cutoff = datetime.now() - timedelta(days=7)

    kept = []
    pruned = 0

    for esc in escalations:
        resolved_at = esc.get("resolvedAt")
        if resolved_at:
            try:
                resolved_time = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
                if resolved_time.tzinfo:
                    resolved_time = resolved_time.replace(tzinfo=None)
                if resolved_time < cutoff:
                    pruned += 1
                    continue
            except (ValueError, TypeError):
                pass
        kept.append(esc)

    if pruned > 0:
        handoff["escalations"] = kept
        handoff_path.write_text(json.dumps(handoff, indent=2, default=str))

    result["pruned"] = pruned
    result["remaining"] = len(kept)
    logger.info("Pruned %d resolved escalations (keeping %d)", pruned, len(kept))

    return result


def compact_quality_log() -> dict:
    """Remove quality log runs older than 30 days."""
    result = {"removed": 0, "remaining": 0}

    quality_log_path = MEMORY_DIR / "rag-quality-log.json"
    if not quality_log_path.exists():
        return result

    quality_log = json.loads(quality_log_path.read_text())
    runs = quality_log.get("runs", [])
    cutoff = datetime.now() - timedelta(days=30)

    kept = []
    removed = 0

    for run in runs:
        timestamp = run.get("timestamp", "")
        if timestamp:
            try:
                run_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if run_time.tzinfo:
                    run_time = run_time.replace(tzinfo=None)
                if run_time < cutoff:
                    removed += 1
                    continue
            except (ValueError, TypeError):
                pass
        kept.append(run)

    if removed > 0:
        quality_log["runs"] = kept
        quality_log_path.write_text(json.dumps(quality_log, indent=2, default=str))

    result["removed"] = removed
    result["remaining"] = len(kept)
    logger.info("Compacted quality log: %d removed, %d remaining", removed, len(kept))

    return result


WORKER_HANDOFF_PATH = MEMORY_DIR / "worker-handoff.json"
MAX_HANDOFF_ITEMS = 50


def truncate_handoff_items() -> dict:
    """Keep only the last MAX_HANDOFF_ITEMS items and escalations in worker-handoff.json."""
    result = {"items_before": 0, "items_after": 0, "escalations_before": 0, "escalations_after": 0}

    if not WORKER_HANDOFF_PATH.exists():
        return result

    try:
        handoff = json.loads(WORKER_HANDOFF_PATH.read_text())
    except (json.JSONDecodeError, ValueError):
        return result

    items = handoff.get("items", [])
    escalations = handoff.get("escalations", [])
    result["items_before"] = len(items)
    result["escalations_before"] = len(escalations)

    changed = False
    if len(items) > MAX_HANDOFF_ITEMS:
        handoff["items"] = items[-MAX_HANDOFF_ITEMS:]
        changed = True
    if len(escalations) > MAX_HANDOFF_ITEMS:
        handoff["escalations"] = escalations[-MAX_HANDOFF_ITEMS:]
        changed = True

    if changed:
        WORKER_HANDOFF_PATH.write_text(json.dumps(handoff, indent=2, default=str))

    result["items_after"] = len(handoff.get("items", []))
    result["escalations_after"] = len(handoff.get("escalations", []))
    logger.info(
        "Handoff truncation: items %d→%d, escalations %d→%d",
        result["items_before"],
        result["items_after"],
        result["escalations_before"],
        result["escalations_after"],
    )
    return result


def main():
    start_time = datetime.now()
    logger.info("=== Data Archival Started: %s ===", start_time)

    # 1. Archive old emails
    logger.info("Step 1: Archiving old emails...")
    email_result = archive_old_emails()

    # 2. Prune resolved escalations
    logger.info("Step 2: Pruning resolved escalations...")
    esc_result = prune_resolved_escalations()

    # 3. Compact quality log
    logger.info("Step 3: Compacting quality log...")
    quality_result = compact_quality_log()

    # 4. Truncate worker handoff
    logger.info("Step 4: Truncating worker handoff...")
    handoff_result = truncate_handoff_items()

    duration = (datetime.now() - start_time).total_seconds()
    logger.info("=== Archival Complete (%.1fs) ===", duration)

    print(f"Data Archival — {start_time.date()}")
    print(f"  Emails archived: {email_result['archived']} (keeping {email_result['remaining']})")
    print(f"  Escalations pruned: {esc_result['pruned']} (keeping {esc_result['remaining']})")
    print(
        f"  Quality log compacted: {quality_result['removed']} (keeping {quality_result['remaining']})"
    )
    print(
        f"  Handoff truncated: items {handoff_result['items_before']}→{handoff_result['items_after']}, escalations {handoff_result['escalations_before']}→{handoff_result['escalations_after']}"
    )
    print(f"  Duration: {duration:.1f}s")


if __name__ == "__main__":
    main()
