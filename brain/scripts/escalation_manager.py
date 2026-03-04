#!/usr/bin/env python3
"""escalation_manager.py — Centralized escalation lifecycle management.

Escalation lifecycle management for worker-handoff.json. Owns dedup, pruning,
and querying of escalation state.
"""

from datetime import UTC, datetime, timedelta


def deduplicate_escalations(handoff):
    """Remove duplicate escalations with the same (source, sourceId).

    Keeps the earliest createdAt entry for each unique (source, sourceId) pair.
    Modifies handoff in place.
    """
    escalations = handoff.get("escalations", [])
    if not escalations:
        return

    seen = {}
    deduped = []
    for esc in escalations:
        key = (esc.get("source"), esc.get("sourceId"))
        if key == (None, None):
            deduped.append(esc)
            continue
        if key in seen:
            existing = seen[key]
            # Keep the one with earlier createdAt
            if (esc.get("createdAt") or "") < (existing.get("createdAt") or ""):
                deduped.remove(existing)
                deduped.append(esc)
                seen[key] = esc
        else:
            seen[key] = esc
            deduped.append(esc)

    handoff["escalations"] = deduped


def prune_resolved(handoff, max_age_hours=24):
    """Remove escalations that have been resolved for longer than max_age_hours.

    Keeps unresolved escalations and recently-resolved ones.
    Modifies handoff in place.
    """
    escalations = handoff.get("escalations", [])
    if not escalations:
        return

    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    cutoff_iso = cutoff.isoformat()

    kept = []
    for esc in escalations:
        resolved_at = esc.get("resolvedAt")
        if resolved_at and resolved_at < cutoff_iso:
            continue  # Resolved and old enough to prune
        kept.append(esc)

    handoff["escalations"] = kept


def get_existing_source_ids(handoff):
    """Return set of sourceId values for active (unresolved) escalations.

    Workers use this to avoid re-escalating items already in the handoff.
    """
    return {
        esc["sourceId"]
        for esc in handoff.get("escalations", [])
        if esc.get("sourceId") and not esc.get("resolvedAt")
    }
