"""
Preference Tracking with Drift Detection.

The `preferences` block stores a JSON list of structured operator preferences,
each with confidence, last-confirmed timestamp, supporting evidence fact ids,
and a `stale` flag. The list is maintained by two nightly passes:

  1. `extract_preferences_from_facts` — LLM scans high-importance recent facts
     tagged as opinion/choice/preference and proposes new preferences or
     reinforces existing ones.
  2. `detect_drift` — for each known preference, scan the last 30 days for
     contradictory evidence. If contradiction confidence > 0.6, mark it stale
     so warmup can surface a gentle re-confirmation prompt.

The `preferences` block is human-readable JSON plus a short summary line so
agent warmup can inject it directly.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.llm import ollama as llm_client
from robothor.memory.blocks import read_block, write_block

logger = logging.getLogger(__name__)

_PREFERENCE_BLOCK = "preferences"
_STALE_CONTRADICTION_THRESHOLD = 0.6
_DRIFT_WINDOW_DAYS = 30


def _load_preferences(tenant_id: str) -> list[dict[str, Any]]:
    """Load the preferences list from the block. Returns [] if missing/corrupt."""
    block = read_block(_PREFERENCE_BLOCK, tenant_id=tenant_id)
    content = (block or {}).get("content") or ""
    if not content.strip():
        return []
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            data = data.get("preferences", [])
        return [p for p in data if isinstance(p, dict) and p.get("preference")]
    except (json.JSONDecodeError, TypeError):
        logger.warning("preferences block is not valid JSON; starting fresh")
        return []


def _save_preferences(prefs: list[dict[str, Any]], tenant_id: str) -> None:
    """Persist the preferences list back to the block."""
    payload = {
        "updated_at": datetime.now(UTC).isoformat(),
        "preferences": prefs,
    }
    content = json.dumps(payload, indent=2, default=str)
    # Append a one-liner summary for warmup injection convenience.
    summary = _format_summary(prefs)
    combined = f"{summary}\n\n{content}" if summary else content
    write_block(_PREFERENCE_BLOCK, combined, tenant_id=tenant_id)


def _format_summary(prefs: list[dict[str, Any]]) -> str:
    """Plain-text rollup for quick scanning by agents."""
    if not prefs:
        return "No tracked preferences yet."
    lines = [f"# Preferences ({len(prefs)} tracked)"]
    for p in sorted(prefs, key=lambda x: float(x.get("confidence", 0)), reverse=True):
        marker = " [STALE]" if p.get("stale") else ""
        lines.append(f"- {p.get('preference', '?')}{marker} (conf={p.get('confidence', 0):.2f})")
    return "\n".join(lines[:25])


def _match_existing(prefs: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    """Simple substring/normalized match to detect duplicates."""
    norm = text.strip().lower()
    for p in prefs:
        existing = (p.get("preference") or "").strip().lower()
        if existing and (existing == norm or existing in norm or norm in existing):
            return p
    return None


async def extract_preferences_from_facts(
    hours_back: int = 72,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """LLM pass: mine recent high-importance facts for preferences.

    Candidate facts are limited to those with category in (preference, opinion,
    decision, personal) and importance_score >= 0.6. For each candidate, we
    ask the LLM for a normalized one-line preference statement or "none".
    Matches merge into existing entries (bump confidence), misses create new.
    """
    tid = tenant_id or DEFAULT_TENANT
    cutoff = datetime.now(UTC) - timedelta(hours=hours_back)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, fact_text, category, confidence, importance_score
            FROM memory_facts
            WHERE is_active = TRUE
              AND tenant_id = %s
              AND created_at >= %s
              AND (category IN ('preference', 'opinion', 'decision', 'personal')
                   OR metadata->>'type' IN ('preference', 'choice'))
              AND importance_score >= 0.6
            ORDER BY importance_score DESC, created_at DESC
            LIMIT 40
            """,
            (tid, cutoff),
        )
        candidates = [dict(r) for r in cur.fetchall()]

    stats = {"candidates": len(candidates), "new": 0, "reinforced": 0, "skipped": 0}
    if not candidates:
        return stats

    prefs = _load_preferences(tid)
    schema = {
        "type": "object",
        "properties": {
            "preference": {"type": "string"},
            "applies": {"type": "boolean"},
        },
        "required": ["preference", "applies"],
    }

    for fact in candidates:
        try:
            raw = await llm_client.generate(
                prompt=(
                    "Below is a fact from memory. If it expresses a durable "
                    "preference of the user (e.g., 'prefers X over Y', 'dislikes Z', "
                    "'always does A'), restate it as one short line starting with "
                    "'Prefers', 'Avoids', or 'Always'. If it is NOT a preference, "
                    "set applies=false and return an empty preference.\n\n"
                    f"Fact: {fact['fact_text']}"
                ),
                system="Return strict JSON.",
                max_tokens=150,
                format=schema,
                think=False,
            )
            data = json.loads(raw) if raw else {}
        except Exception as e:
            logger.debug("preference extract failed for fact %s: %s", fact["id"], e)
            stats["skipped"] += 1
            continue

        if not data.get("applies"):
            stats["skipped"] += 1
            continue

        text = (data.get("preference") or "").strip()
        if len(text) < 5:
            stats["skipped"] += 1
            continue

        existing = _match_existing(prefs, text)
        now = datetime.now(UTC).isoformat()
        if existing:
            existing["confidence"] = min(1.0, float(existing.get("confidence", 0.5)) + 0.05)
            existing["last_confirmed"] = now
            existing["stale"] = False
            evidence = existing.setdefault("evidence_fact_ids", [])
            if fact["id"] not in evidence:
                evidence.append(fact["id"])
            stats["reinforced"] += 1
        else:
            prefs.append(
                {
                    "preference": text,
                    "confidence": float(fact.get("importance_score", 0.6)),
                    "last_confirmed": now,
                    "evidence_fact_ids": [fact["id"]],
                    "stale": False,
                }
            )
            stats["new"] += 1

    _save_preferences(prefs, tid)
    return stats


async def detect_drift(tenant_id: str | None = None) -> dict[str, Any]:
    """Scan last N days for evidence that contradicts each known preference.

    For each preference, run a semantic search for contradictions. If a
    contradiction with similarity > threshold appears, mark `stale=True` so
    warmup can prompt re-confirmation.
    """
    tid = tenant_id or DEFAULT_TENANT
    prefs = _load_preferences(tid)
    if not prefs:
        return {"checked": 0, "marked_stale": 0}

    from robothor.memory.facts import search_facts

    marked = 0
    cutoff = datetime.now(UTC) - timedelta(days=_DRIFT_WINDOW_DAYS)

    for p in prefs:
        query = f"evidence against: {p['preference']}"
        try:
            results = await search_facts(
                query,
                limit=5,
                use_reranker=False,
                tenant_id=tid,
            )
        except Exception as e:
            logger.debug("drift search failed for %s: %s", p["preference"], e)
            continue

        # Only consider facts newer than the drift window and newer than
        # `last_confirmed` — old facts don't invalidate recent confirmations.
        last_confirmed_str = p.get("last_confirmed") or ""
        try:
            last_confirmed = datetime.fromisoformat(last_confirmed_str)
        except (ValueError, TypeError):
            last_confirmed = cutoff
        # Postgres returns tz-aware timestamps; a naive ISO string from the
        # prefs store would raise on comparison. Coerce to UTC.
        if last_confirmed.tzinfo is None:
            last_confirmed = last_confirmed.replace(tzinfo=UTC)

        recent_contradictions = [
            r
            for r in results
            if r.get("created_at") is not None
            and r["created_at"] > last_confirmed
            and r["created_at"] > cutoff
            and r.get("similarity", 0) > _STALE_CONTRADICTION_THRESHOLD
            and r.get("id") not in (p.get("evidence_fact_ids") or [])
        ]

        if recent_contradictions:
            p["stale"] = True
            p["stale_evidence_fact_ids"] = [r["id"] for r in recent_contradictions[:3]]
            marked += 1

    _save_preferences(prefs, tid)
    return {"checked": len(prefs), "marked_stale": marked}


def get_stale_preferences(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Return preferences flagged as stale so warmup can surface re-confirmation."""
    tid = tenant_id or DEFAULT_TENANT
    return [p for p in _load_preferences(tid) if p.get("stale")]
