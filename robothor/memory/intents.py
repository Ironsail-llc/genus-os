"""Prospective / Intent memory — what the operator is working toward.

Stores standing objectives that persist across sessions so the main agent's
heartbeat can advance them proactively, rather than only reacting. Parallel
to ``session_goal`` (per-run, evidence-gated); intents are longer-lived.

Confirmation model:
    * ``stated`` intents (the operator said so) are ``active`` immediately.
    * ``inferred`` intents (proposed by ``infer_intents_from_facts``) start as
      ``proposed`` and only become ``active`` via ``confirm_intent`` with a
      valid HMAC token — the agent never auto-activates a goal it invented.

Gated by ``ROBOTHOR_RIP_14_ENABLED``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.llm import ollama as llm_client

logger = logging.getLogger(__name__)

VALID_STATUS = frozenset({"proposed", "active", "dormant", "achieved", "dropped"})
VALID_HORIZON = frozenset({"ongoing", "this_quarter", "this_week", "dated"})
VALID_SOURCE = frozenset({"stated", "inferred"})

# Intents untouched for this long are auto-marked dormant by the maintenance pass.
DORMANT_AFTER_DAYS = 30


# --------------------------------------------------------------------------- #
# HMAC confirmation (mirrors scripts/delphi_apply_proposal.py)
# --------------------------------------------------------------------------- #


def _hmac_secret() -> str:
    return os.environ.get("ROBOTHOR_INTENT_HMAC_SECRET", "")


def expected_token(intent_id: int, action: str = "confirm") -> str:
    """Deterministic approve/reject token for an inferred intent."""
    secret = _hmac_secret()
    if not secret:
        raise RuntimeError("ROBOTHOR_INTENT_HMAC_SECRET not set")
    return hmac.new(
        secret.encode("utf-8"),
        f"{intent_id}:{action}".encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_token(intent_id: int, token: str, action: str = "confirm") -> bool:
    try:
        return hmac.compare_digest(token, expected_token(intent_id, action))
    except RuntimeError:
        return False


# --------------------------------------------------------------------------- #
# Storage / retrieval
# --------------------------------------------------------------------------- #


async def upsert_intent(
    title: str,
    description: str = "",
    *,
    horizon: str = "ongoing",
    due_at: str | None = None,
    priority: int = 3,
    source: str = "stated",
    confidence: float = 0.5,
    status: str | None = None,
    tenant_id: str = "",
) -> int:
    """Create or update an intent (dedup by title within tenant). Returns id.

    ``stated`` intents default to ``active``; ``inferred`` default to
    ``proposed`` so they require confirmation before the agent acts on them.
    """
    if horizon not in VALID_HORIZON:
        raise ValueError(f"invalid horizon {horizon!r} (valid: {sorted(VALID_HORIZON)})")
    if source not in VALID_SOURCE:
        raise ValueError(f"invalid source {source!r} (valid: {sorted(VALID_SOURCE)})")
    if status is None:
        status = "proposed" if source == "inferred" else "active"
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status {status!r} (valid: {sorted(VALID_STATUS)})")

    resolved_tenant = tenant_id or DEFAULT_TENANT
    embedding = await llm_client.get_embedding_async(f"{title}\n{description}")

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO memory_intents
                (tenant_id, title, description, horizon, due_at, status, priority,
                 source, confidence, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, md5(title))
            DO UPDATE SET
                description = EXCLUDED.description,
                horizon = EXCLUDED.horizon,
                due_at = EXCLUDED.due_at,
                status = EXCLUDED.status,
                priority = EXCLUDED.priority,
                source = EXCLUDED.source,
                confidence = EXCLUDED.confidence,
                embedding = EXCLUDED.embedding,
                updated_at = NOW()
            RETURNING id
            """,
            (
                resolved_tenant,
                title,
                description,
                horizon,
                due_at,
                status,
                priority,
                source,
                confidence,
                embedding,
            ),
        )
        intent_id: int = cur.fetchone()[0]

    return intent_id


async def search_intents(
    query: str,
    *,
    limit: int = 5,
    status: str | None = "active",
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Semantic search over intents, optionally filtered by status."""
    resolved_tenant = tenant_id or DEFAULT_TENANT
    embedding = await llm_client.get_embedding_async(query)

    status_clause = "AND status = %s" if status else ""
    params: list[Any] = [embedding, resolved_tenant]
    if status:
        params.append(status)
    params.extend([embedding, limit])

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"""
            SELECT id, title, description, horizon, status, priority, source,
                   confidence, last_advanced_at,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM memory_intents
            WHERE embedding IS NOT NULL AND tenant_id = %s {status_clause}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        r["similarity"] = round(r.get("similarity") or 0.0, 4)
    return rows


def list_active_intents(*, limit: int = 10, tenant_id: str = "") -> list[dict[str, Any]]:
    """Highest-priority active intents (priority asc, least-recently-advanced first)."""
    resolved_tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, title, description, horizon, priority, last_advanced_at, due_at
            FROM memory_intents
            WHERE tenant_id = %s AND status = 'active'
            ORDER BY priority ASC, last_advanced_at ASC NULLS FIRST
            LIMIT %s
            """,
            (resolved_tenant, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def set_status(intent_id: int, status: str, *, tenant_id: str = "") -> bool:
    """Transition an intent's status. Returns True if a row changed."""
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status {status!r}")
    resolved_tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE memory_intents SET status = %s, updated_at = NOW() "
            "WHERE id = %s AND tenant_id = %s",
            (status, intent_id, resolved_tenant),
        )
        return cur.rowcount > 0


def mark_advanced(intent_id: int, *, tenant_id: str = "") -> bool:
    """Record that work advanced this intent (sets last_advanced_at = now)."""
    resolved_tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE memory_intents SET last_advanced_at = NOW(), updated_at = NOW() "
            "WHERE id = %s AND tenant_id = %s",
            (intent_id, resolved_tenant),
        )
        return cur.rowcount > 0


def link_goal(intent_id: int, goal_id: int, *, tenant_id: str = "") -> bool:
    """Attach a session-goal id to an intent and mark it advanced."""
    resolved_tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE memory_intents
            SET linked_goal_ids = (
                    SELECT ARRAY(SELECT DISTINCT unnest(linked_goal_ids || %s::int))
                ),
                last_advanced_at = NOW(),
                updated_at = NOW()
            WHERE id = %s AND tenant_id = %s
            """,
            (goal_id, intent_id, resolved_tenant),
        )
        return cur.rowcount > 0


def confirm_intent(intent_id: int, token: str, *, tenant_id: str = "") -> dict[str, Any]:
    """Promote a proposed (inferred) intent to active after HMAC verification."""
    if not verify_token(intent_id, token):
        return {"error": "invalid_token", "id": intent_id}
    resolved_tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE memory_intents SET status = 'active', updated_at = NOW()
            WHERE id = %s AND tenant_id = %s AND status = 'proposed'
            """,
            (intent_id, resolved_tenant),
        )
        if cur.rowcount == 0:
            return {"error": "not_proposed_or_not_found", "id": intent_id}
    return {"ok": True, "id": intent_id, "status": "active"}


def attribute_goal_completion(goal_id: int, *, tenant_id: str = "") -> int:
    """Mark every intent linked to a completed goal as advanced. Returns count."""
    resolved_tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE memory_intents
            SET last_advanced_at = NOW(), updated_at = NOW()
            WHERE tenant_id = %s AND %s = ANY(linked_goal_ids)
            """,
            (resolved_tenant, goal_id),
        )
        return cur.rowcount


def mark_dormant_intents(*, tenant_id: str = "", after_days: int = DORMANT_AFTER_DAYS) -> int:
    """Move long-idle active intents to dormant so they surface for review."""
    resolved_tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE memory_intents
            SET status = 'dormant', updated_at = NOW()
            WHERE tenant_id = %s AND status = 'active'
              AND COALESCE(last_advanced_at, created_at) < NOW() - (%s || ' days')::interval
            """,
            (resolved_tenant, after_days),
        )
        return cur.rowcount


# --------------------------------------------------------------------------- #
# Inference (nightly LLM pass) + warmup rendering
# --------------------------------------------------------------------------- #

_INFER_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "horizon": {"type": "string", "enum": sorted(VALID_HORIZON)},
            "confidence": {"type": "number"},
        },
        "required": ["title", "description", "confidence"],
    },
}


async def infer_intents_from_facts(*, tenant_id: str = "", fact_limit: int = 50) -> list[int]:
    """Propose standing intents from recent facts. Stored as proposed/inferred.

    Best-effort: returns the ids created. These are NOT active until confirmed.
    """
    resolved_tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT fact_text FROM memory_facts
            WHERE tenant_id = %s AND is_active = TRUE
            ORDER BY created_at DESC LIMIT %s
            """,
            (resolved_tenant, fact_limit),
        )
        facts = [r["fact_text"] for r in cur.fetchall()]

    if not facts:
        return []

    prompt = (
        "From these recent facts about the operator and their work, infer up to 3 "
        "standing INTENTS — ongoing objectives they appear to be working toward "
        "(not one-off tasks). Return JSON array of {title, description, horizon, "
        "confidence}. Only propose an intent if the facts genuinely support it.\n\n"
        + "\n".join(f"- {f}" for f in facts)
    )
    try:
        raw = await llm_client.generate(
            prompt=prompt,
            system="Infer standing intents as a JSON array.",
            max_tokens=1024,
            format=_INFER_SCHEMA,
            think=False,
        )
        proposed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:  # noqa: BLE001 — inference is best-effort
        logger.warning("infer_intents_from_facts failed: %s", e)
        return []

    created: list[int] = []
    for p in proposed or []:
        title = (p.get("title") or "").strip()
        if not title:
            continue
        intent_id = await upsert_intent(
            title,
            p.get("description", ""),
            horizon=p.get("horizon", "ongoing"),
            source="inferred",
            confidence=float(p.get("confidence", 0.4)),
            status="proposed",
            tenant_id=resolved_tenant,
        )
        created.append(intent_id)
    return created


def build_active_intents_context(tenant_id: str = "", *, limit: int = 5) -> str | None:
    """Render top active intents for the warmup preamble (≤ ~600 chars)."""
    intents = list_active_intents(limit=limit, tenant_id=tenant_id or DEFAULT_TENANT)
    if not intents:
        return None
    lines = ["# Standing intents (what the operator is working toward — advance these)"]
    lines.extend(f"- [{i['id']}] ({i['horizon']}, p{i['priority']}) {i['title']}" for i in intents)
    text = "\n".join(lines)
    return text[:600]
