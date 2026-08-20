"""
Fact Extraction Layer for Genus OS Memory System.

Extracts structured facts from unstructured content using a local LLM,
then stores them with vector embeddings in PostgreSQL for semantic search.

Architecture:
    Content -> LLM extraction -> Parse JSON -> Store with embedding -> pgvector search

Dependencies:
    - robothor.memory.generation for LLM generation (local ollama by default,
      remote via ROBOTHOR_MEMORY_GENERATION_PROVIDER)
    - robothor.llm.ollama for embeddings (always local)
    - PostgreSQL with pgvector for storage and search
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.identity.scope import DataScope

import httpx
from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.llm import ollama as llm_client
from robothor.memory import generation
from robothor.memory.drift import (
    audit_snapshot,
    compute_fact_hash,
    evaluate_drift,
)
from robothor.memory.vector_tuning import apply_hnsw_session

logger = logging.getLogger(__name__)

VALID_CATEGORIES = [
    "personal",
    "project",
    "decision",
    "preference",
    "event",
    "contact",
    "technical",
    "resolution",
]

# JSON schema for Ollama structured output.
FACT_EXTRACTION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "fact_text": {"type": "string"},
            "category": {
                "type": "string",
                "enum": VALID_CATEGORIES,
            },
            "entities": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
        "required": ["fact_text", "category", "entities", "confidence"],
    },
}


def build_extraction_prompt(content: str) -> str:
    """Build the LLM prompt for fact extraction."""
    return f"""Extract specific, memorable facts from the following content.

Rules:
- Each fact MUST be a complete sentence with a subject and predicate
- Each fact MUST reference at least one specific named entity (person, organization, place, project, technology)
- Each fact MUST be specific to this content — NOT generic knowledge anyone would know
- Include temporal context when present (dates, "yesterday", "next week", etc.)
- Categorize each fact: decision (someone decided X), preference (someone prefers X), event (X happened), resolution (someone resolved/closed/confirmed an open item, alert, or question), contact (relationship info), project (work/technical), personal (personal life), technical (system/code)

Skip:
- Greetings, filler, partial sentences
- Generic statements ("X is a company", "X is available", "meetings are important")
- Single words or numbers without context
- Facts that don't mention any specific person, organization, or project by name

Content:
{content}"""


def parse_extraction_response(raw: str) -> list[dict[str, Any]]:
    """Parse the LLM's extraction response into structured facts.

    Handles markdown fences, single objects, missing fields, and
    out-of-range confidence values.

    Args:
        raw: Raw LLM response text.

    Returns:
        List of validated fact dictionaries.
    """
    if not raw or not raw.strip():
        return []

    text = raw.strip()

    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    if not text:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        else:
            return []

    if isinstance(parsed, dict):
        parsed = [parsed]

    if not isinstance(parsed, list):
        return []

    valid_facts = []
    for item in parsed:
        if not isinstance(item, dict):
            continue

        fact_text = item.get("fact_text", "")
        if not fact_text or not fact_text.strip():
            continue

        category = str(item.get("category", "personal")).lower().strip()
        if category not in VALID_CATEGORIES:
            category = "personal"

        confidence = item.get("confidence", 0.8)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.8
        confidence = max(0.0, min(1.0, confidence))

        entities = item.get("entities", [])
        if not isinstance(entities, list):
            entities = []
        entities = [str(e) for e in entities if e]

        valid_facts.append(
            {
                "fact_text": fact_text.strip(),
                "category": category,
                "entities": entities,
                "confidence": confidence,
            }
        )

    # Hard quality filters — reject garbage before it enters the database
    filtered = []
    for fact in valid_facts:
        text = fact["fact_text"]

        # Too short to be meaningful (resolutions may be terse but high-value).
        if len(text) < 15 and fact["category"] != "resolution":
            logger.debug("Rejected (too short): %s", text[:50])
            continue

        # No entities — can't be a specific fact
        if not fact["entities"]:
            logger.debug("Rejected (no entities): %s", text[:50])
            continue

        # Too low confidence
        if fact["confidence"] < 0.3:
            logger.debug("Rejected (low confidence %.2f): %s", fact["confidence"], text[:50])
            continue

        # Single word/number
        if re.match(r"^\s*\w+\s*$", text):
            logger.debug("Rejected (single word): %s", text[:50])
            continue

        # Generic patterns that add no value
        generic_patterns = [
            r"^.{1,30}\s+is\s+a\s+(company|person|tool|platform|service|technology)\b",
            r"^.{1,30}\s+is\s+available\b",
            r"^(Hello|Hi|Hey|Thanks|Thank you|Bye|Goodbye)\b",
        ]
        is_generic = False
        for pattern in generic_patterns:
            if re.match(pattern, text, re.IGNORECASE):
                logger.debug("Rejected (generic pattern): %s", text[:50])
                is_generic = True
                break
        if is_generic:
            continue

        filtered.append(fact)

    if len(filtered) < len(valid_facts):
        logger.info("Quality filter: %d/%d facts passed", len(filtered), len(valid_facts))

    return filtered


async def extract_facts(
    content: str, max_retries: int = 3, *, timeout: float = 180.0
) -> list[dict[str, Any]]:
    """Extract facts from content using the local LLM.

    Retries on empty results because thinking models sometimes exhaust
    their token budget on reasoning before producing content.

    ``timeout`` defaults to 180s, which suits off-request-path callers
    (ingestion, the eval) where nothing is waiting on the result. The request
    path must pass something smaller than its own wall: ``store_memory`` runs
    inside a 120s tool timeout, so the previous unconditional 180s meant the
    inner budget exceeded the outer one and a slow extraction was guaranteed to
    be killed before it could return — 15 of 121 calls over 30 days sat at
    exactly 120,003 ms.

    Measured with the model warm, extraction is ~23s and embedding ~0.12s, so
    this budget governs essentially the whole call; the remaining gap to the
    63.5s production p50 is cold model loading.

    Args:
        content: Unstructured text content.
        max_retries: Number of attempts before giving up.
        timeout: Hard cap in seconds for all attempts combined.

    Returns:
        List of extracted fact dictionaries, or empty list on failure.
    """
    try:
        return await asyncio.wait_for(_extract_facts_inner(content, max_retries), timeout=timeout)
    except TimeoutError:
        logger.warning("extract_facts hard timeout (%.0fs) — returning empty", timeout)
        return []


async def _extract_facts_inner(
    content: str,
    max_retries: int,
) -> list[dict[str, Any]]:
    """Inner implementation of extract_facts (no timeout wrapper)."""
    prompt = build_extraction_prompt(content)
    for attempt in range(max_retries):
        try:
            logger.info("extract_facts attempt %d/%d", attempt + 1, max_retries)
            raw = await generation.generate(
                prompt=prompt,
                system="Extract facts from the content as a JSON array.",
                max_tokens=1024,
                format=FACT_EXTRACTION_SCHEMA,
                think=False,
            )
            logger.info("LLM returned %d chars", len(raw) if raw else 0)
            if not raw or not raw.strip():
                logger.warning("Empty response from LLM on attempt %d", attempt + 1)
                continue
            facts = parse_extraction_response(raw)
            if facts:
                logger.info("Parsed %d facts on attempt %d", len(facts), attempt + 1)
                return facts
            logger.warning("Parsed 0 facts from %d chars on attempt %d", len(raw), attempt + 1)
        except Exception as e:
            logger.warning("extract_facts attempt %d failed: %s", attempt + 1, e)
    logger.error("extract_facts failed after %d attempts", max_retries)
    return []


_dedup_flag_warned = False


def _write_dedup_enabled() -> bool:
    """Write-time dedup is always on. The MEMORY_WRITE_DEDUP flag is retired.

    Migration 078's partial unique index on active (tenant_id, content_hash)
    is global, so gating dedup per-process turned duplicate stores from any
    writer launched without the flag (e.g. cron-launched ingest) into hard
    duplicate-key ERRORs. The env var is read only to warn that it no longer
    does anything.
    """
    global _dedup_flag_warned  # noqa: PLW0603
    if os.environ.get("MEMORY_WRITE_DEDUP") is not None and not _dedup_flag_warned:
        logger.warning(
            "MEMORY_WRITE_DEDUP is deprecated and ignored — "
            "write-time dedup is always enabled (migration 078 index is global)"
        )
        _dedup_flag_warned = True
    return True


_INSERT_FACT_SQL = """
    INSERT INTO memory_facts
    (fact_text, category, entities, confidence, source_content, source_type,
     embedding, metadata, tenant_id, content_hash)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
_INSERT_FACT_ON_CONFLICT = (
    " ON CONFLICT (tenant_id, content_hash) "
    "WHERE is_active = TRUE AND content_hash IS NOT NULL DO NOTHING"
)


def _insert_fact(cur: Any, params: tuple[Any, ...], *, tenant_id: str, content_hash: str) -> int:
    """Insert one memory_facts row and return its id.

    Dedup is unconditional: a byte-identical active fact short-circuits to
    the existing row's id (no new row, no duplicate-key error) via the 078
    partial unique index.
    """
    _write_dedup_enabled()  # emits the deprecation warning if the retired flag is set
    cur.execute(_INSERT_FACT_SQL + _INSERT_FACT_ON_CONFLICT + " RETURNING id", params)
    row = cur.fetchone()
    if row is not None:
        return int(row[0])
    cur.execute(
        "SELECT id FROM memory_facts "
        "WHERE tenant_id = %s AND content_hash = %s AND is_active = TRUE "
        "ORDER BY id DESC LIMIT 1",
        (tenant_id, content_hash),
    )
    existing = cur.fetchone()
    return int(existing[0]) if existing else 0


async def store_fact(
    fact: dict[str, Any],
    source_content: str,
    source_type: str,
    metadata: dict[str, Any] | None = None,
    *,
    tenant_id: str = "",
) -> int:
    """Store a fact with its embedding in the database.

    Args:
        fact: Fact dictionary with fact_text, category, entities, confidence.
        source_content: Original content the fact was extracted from.
        source_type: Type of source (conversation, email, etc.).
        metadata: Optional additional metadata.
        tenant_id: Tenant scope for data isolation.

    Returns:
        The database ID of the stored fact, or 0 if the quality gate refused it
        in enforce mode.
    """
    from robothor.memory.quality import quality_mode, record_shadow_rejection, score_fact

    _mode = quality_mode()
    _verdict = None
    if _mode != "off":
        _verdict = score_fact(fact["fact_text"], confidence=fact.get("confidence"))
        if not _verdict.accept and _mode == "enforce":
            # Refused BEFORE the embedding call: a fact we will not keep should
            # not cost an embedding. Measured on 25,910 live facts, this gate
            # refuses 0.80%.
            logger.info(
                "store_fact refused by quality gate: %s | %.60s",
                _verdict.reason,
                fact["fact_text"],
            )
            return 0

    embedding = await llm_client.get_embedding_async(fact["fact_text"])
    resolved_tenant = tenant_id or DEFAULT_TENANT
    content_hash = compute_fact_hash(
        fact["fact_text"], tenant_id=resolved_tenant, category=fact["category"]
    )

    with get_connection() as conn:
        cur = conn.cursor()
        fact_id = _insert_fact(
            cur,
            (
                fact["fact_text"],
                fact["category"],
                fact.get("entities", []),
                fact.get("confidence", 1.0),
                source_content,
                source_type,
                embedding,
                json.dumps(metadata or {}),
                resolved_tenant,
                content_hash,
            ),
            tenant_id=resolved_tenant,
            content_hash=content_hash,
        )

    # Shadow leaves a trace or it proves nothing. This repo already has one flag
    # whose "zero events" evidence was vacuous because observe wrote nothing.
    if _verdict is not None and not _verdict.accept and _mode == "shadow":
        record_shadow_rejection(fact_id, resolved_tenant, _verdict)

    return fact_id


async def store_facts_batch(
    facts: list[dict[str, Any]],
    source_content: str,
    source_type: str,
    metadata: dict[str, Any] | None = None,
    *,
    tenant_id: str = "",
) -> list[int]:
    """Store multiple facts with batch-embedded vectors.

    Embeds all fact texts in a single Ollama call, then inserts each fact
    with its pre-computed embedding.

    Args:
        facts: List of fact dicts with fact_text, category, entities, confidence.
        source_content: Original content the facts were extracted from.
        source_type: Type of source (conversation, email, etc.).
        metadata: Optional additional metadata.
        tenant_id: Tenant scope for data isolation.

    Returns:
        List of database IDs for the stored facts.
    """
    if not facts:
        return []

    texts = [f["fact_text"] for f in facts]
    embeddings = await llm_client.get_embeddings_batch_async(texts)
    resolved_tenant = tenant_id or DEFAULT_TENANT

    with get_connection() as conn:
        cur = conn.cursor()
        ids = []

        for fact, embedding in zip(facts, embeddings, strict=True):
            content_hash = compute_fact_hash(
                fact["fact_text"], tenant_id=resolved_tenant, category=fact["category"]
            )
            ids.append(
                _insert_fact(
                    cur,
                    (
                        fact["fact_text"],
                        fact["category"],
                        fact.get("entities", []),
                        fact.get("confidence", 1.0),
                        source_content,
                        source_type,
                        embedding,
                        json.dumps(metadata or {}),
                        resolved_tenant,
                        content_hash,
                    ),
                    tenant_id=resolved_tenant,
                    content_hash=content_hash,
                )
            )

    logger.info("store_facts_batch: stored %d facts with batch embeddings", len(ids))
    return ids


async def update_fact(
    fact_id: int,
    *,
    fact_text: str,
    tenant_id: str = "",
    category: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update a `memory_facts` row with Rip 7 external-drift detection.

    Reads the current row, recomputes the canonical content_hash from
    the stored fields, and compares it to the persisted hash. If the
    two disagree, another writer has touched the row out of band — we
    snapshot the stored state into `memory_facts_audit` and (in
    enforce mode) refuse the update.

    Returns one of::

        {"ok": True, "fact_id": int}
        {"error": "not_found", "fact_id": int}
        {"error": "drift_refused", "fact_id": int, "audit_snapshot_id": int|None}

    The drift check is fully bypassed when Rip 7 is off (default).
    """
    resolved_tenant = tenant_id or DEFAULT_TENANT

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT fact_text, tenant_id, category, person_id, content_hash
              FROM memory_facts
             WHERE id = %s AND tenant_id = %s
            """,
            (fact_id, resolved_tenant),
        )
        row = cur.fetchone()
        if row is None:
            return {"error": "not_found", "fact_id": fact_id}

        current_text, current_tenant, current_category, current_person, stored_hash = row

        decision = evaluate_drift(
            stored_hash,
            fact_text=current_text or "",
            tenant_id=current_tenant or resolved_tenant,
            category=current_category or "",
            person_id=str(current_person) if current_person else None,
        )

        if decision.drift_detected:
            recomputed = compute_fact_hash(
                current_text or "",
                tenant_id=current_tenant or resolved_tenant,
                category=current_category or "",
                person_id=str(current_person) if current_person else None,
            )
            snap_id = audit_snapshot(
                cur,
                fact_id=fact_id,
                tenant_id=current_tenant or resolved_tenant,
                fact_text=current_text,
                hash_at_snapshot=stored_hash,
                hash_expected=recomputed,
                reason="pre_update_drift_detected",
            )
            logger.warning(
                "Memory drift detected on fact %d (tenant=%s, mode=%s, snapshot=%s)",
                fact_id,
                current_tenant,
                decision.mode,
                snap_id,
            )
            if decision.action == "refuse":
                return {
                    "error": "drift_refused",
                    "fact_id": fact_id,
                    "audit_snapshot_id": snap_id,
                }

        # Apply the update with a fresh hash over the new state.
        new_category = category if category is not None else current_category
        new_person = str(current_person) if current_person else None
        new_hash = compute_fact_hash(
            fact_text,
            tenant_id=current_tenant or resolved_tenant,
            category=new_category or "",
            person_id=new_person,
        )

        cur.execute(
            """
            UPDATE memory_facts
               SET fact_text = %s,
                   category = COALESCE(%s, category),
                   metadata = COALESCE(%s::jsonb, metadata),
                   content_hash = %s,
                   updated_at = NOW()
             WHERE id = %s AND tenant_id = %s
            """,
            (
                fact_text,
                category,
                json.dumps(metadata) if metadata is not None else None,
                new_hash,
                fact_id,
                resolved_tenant,
            ),
        )

    return {"ok": True, "fact_id": fact_id}


async def search_insights(
    query: str,
    limit: int = 5,
    *,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Search cross-domain insights by vector similarity.

    Args:
        query: Search query text.
        limit: Maximum number of results.
        tenant_id: Tenant scope for data isolation.

    Returns:
        List of matching insight dictionaries sorted by similarity.
    """
    try:
        embedding = await llm_client.get_embedding_async(query)
    except httpx.HTTPError as e:
        # Insight search is vector-only: without an embedding there is no
        # keyword leg to fall back to. Degrade to no insights rather than
        # killing the caller's whole search with a transport error.
        logger.warning(
            "search_insights: embedding fetch failed (%s) — returning no insights",
            type(e).__name__,
        )
        return []

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        apply_hnsw_session(cur)
        cur.execute(
            """
            SELECT id, insight_text, source_fact_ids, categories, entities,
                   created_at, metadata,
                   1 - (embedding <=> %s::vector) as similarity
            FROM memory_insights
            WHERE is_active = TRUE AND embedding IS NOT NULL
              AND tenant_id = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (embedding, tenant_id or DEFAULT_TENANT, embedding, limit),
        )
        results = [dict(r) for r in cur.fetchall()]

    for r in results:
        r["source"] = "insight"

    return results


def _reranker_enabled_default() -> bool:
    """Feature flag for reranker. Default on; kill-switch via env."""
    raw = os.environ.get("MEMORY_RERANK_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _rank_blend_enabled() -> bool:
    """Blend importance/recency/access into final ranking. Default on."""
    raw = os.environ.get("MEMORY_RANK_BLEND", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _dedup_enabled() -> bool:
    """Suppress near-duplicate hits in results. Default on."""
    raw = os.environ.get("MEMORY_DEDUP", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _temporal_coherence_enabled() -> bool:
    """Supersession-aware temporal ranking (R1). Default OFF (observe first).

    When on, a `decision` fact that has been superseded — either explicitly
    (`superseded_by` set by conflict resolution) or by inference (a later
    decision sharing the same topic) — is demoted, and a fresh decision is
    boosted. Fixes "agent acts on a stale decision": the latest decision wins
    even when an older one is a closer lexical match. Gated so it can be
    measured on the memory-eval suite before flipping on.
    """
    raw = os.environ.get("MEMORY_TEMPORAL_COHERENCE", "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _rerank_wide_enabled() -> bool:
    """Rerank a WIDE survivor pool, then let `_blend_rank` cut to `limit` (WS-1).

    Default OFF (observe). When off, the cross-encoder truncates to `limit`
    *before* the recency/importance blend runs, so a fresh, high-importance,
    on-topic fact the 0.6B reranker drops can never be rescued. When on, rerank
    keeps ``max(limit*4, 24)`` survivors — each carrying its yes/no verdict — and
    the blend does the final cut, with a small bonus for ``rerank_relevant=yes``.
    """
    raw = os.environ.get("MEMORY_RERANK_WIDE", "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _episode_merge_enabled() -> bool:
    """Merge episodes/insights/chat-turns on the RERANKED path too (WS-1.3).

    Default OFF. The reranker branch used to return before the
    include_episodes / second include_insights append, so the (only populated)
    episode store was invisible whenever the reranker was on (the prod default).
    When on, both the reranked and fallback paths share ``_append_auxiliary``.
    """
    raw = os.environ.get("MEMORY_EPISODE_MERGE", "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _recency_key(r: dict[str, Any]) -> tuple[float, int]:
    """Sortable "how recent" key: (created_at epoch, id).

    For co-ingested facts (same batch → identical created_at), `id` (serial,
    monotonic with insertion order) is the only signal of which came later —
    so it is the tie-breaker for both the deterministic sort and supersession
    inference.
    """
    ca = r.get("created_at")
    ts = ca.timestamp() if ca is not None and hasattr(ca, "timestamp") else 0.0
    return (ts, int(r.get("id") or 0))


# Recency half-life: a 7-day half-life keeps a usable gradient across the
# week+ window where most queries live (a 72h half-life flattened everything
# older than a few days to ~0, so recency stopped differentiating).
_RECENCY_HALFLIFE_HOURS = 168.0


def _norm_tokens(text: str) -> frozenset[str]:
    return frozenset(re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split())


def _dedupe(results: list[dict[str, Any]], threshold: float = 0.75) -> list[dict[str, Any]]:
    """Drop near-duplicate rows, keeping the higher-ranked one.

    Catches exact dups, subsets, and lexical near-dups (Jaccard token overlap
    >= threshold) — e.g. the camera-detection spam and reworded restatements
    that otherwise fill the top of a result set. Conservative: only suppresses
    clear textual overlap, not merely topically-related facts.
    """
    kept: list[dict[str, Any]] = []
    kept_tokens: list[frozenset[str]] = []
    for r in results:
        txt = r.get("fact_text") or r.get("insight_text") or r.get("content") or ""
        toks = _norm_tokens(txt)
        is_dup = False
        for kt in kept_tokens:
            if not toks or not kt:
                continue
            union = len(toks | kt)
            jac = len(toks & kt) / union if union else 0.0
            if jac >= threshold or toks <= kt or kt <= toks:
                is_dup = True
                break
        if not is_dup:
            kept.append(r)
            kept_tokens.append(toks)
    return kept


def _blend_rank(results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Unified-score re-rank (relevance + recency + importance + access), then dedupe.

    Per live feedback, recency is weighted heavily (operator wants the freshest
    of several relevant hits surfaced first) while relevance stays the largest
    single signal so on-topic hits keep the top. Near-duplicates are then
    collapsed so the first few results are distinct.
    """
    coherence_on = _temporal_coherence_enabled()
    # When the wide-rerank pool is in use, the candidate list mixes cross-encoder
    # "yes" and "no" rows; fold the verdict in as a soft signal (not a hard gate)
    # so relevance stays authoritative but a confirmed-relevant hit is nudged up.
    wide_on = _rerank_wide_enabled()

    # R1: read-time supersession inference. Among `decision` facts, a decision
    # that shares a topic (>=2 entities) with a LATER decision is treated as
    # superseded — the freshest decision on a topic is the current one. >=2
    # shared entities avoids cross-linking unrelated decisions that merely
    # mention the same person.
    inferred_superseded: set[int] = set()
    if coherence_on:
        decisions = [r for r in results if (r.get("category") or "") == "decision"]
        for r in decisions:
            r_ents = set(r.get("entities") or [])
            if len(r_ents) < 2:
                continue
            r_key = _recency_key(r)
            for o in decisions:
                if o is r:
                    continue
                if len(r_ents & set(o.get("entities") or [])) >= 2 and _recency_key(o) > r_key:
                    inferred_superseded.add(id(r))
                    break

    for i, r in enumerate(results):
        rel = r.get("similarity")
        rel = (1.0 - i / (len(results) or 1)) if rel is None else float(rel)
        imp = r.get("importance_score")
        imp = 0.5 if imp is None else float(imp)
        age_s = r.get("age_seconds")
        recency = 0.3 if age_s is None else 0.5 ** (float(age_s) / 3600.0 / _RECENCY_HALFLIFE_HOURS)
        acc_norm = min(float(r.get("access_count") or 0) / 20.0, 0.3)
        blend = 0.55 * rel + 0.25 * recency + 0.15 * imp + 0.05 * acc_norm
        if wide_on and r.get("rerank_relevant") == "yes":
            blend += 0.10  # cross-encoder confirmed relevance — soft boost, not a gate
        if coherence_on:
            if r.get("superseded_by") is not None or id(r) in inferred_superseded:
                blend *= 0.5  # demote known-stale / superseded decision
            elif (r.get("category") or "") == "decision":
                blend *= 1.0 + 0.30 * recency  # freshest decision floats up
        r["_blend"] = blend
    # Deterministic newest-wins tie-break: on equal _blend, prefer the later
    # fact (created_at, id). Safe unflagged — only matters on exact ties.
    ranked = sorted(results, key=lambda x: (x.get("_blend", 0.0), *_recency_key(x)), reverse=True)
    for r in ranked:
        r.pop("_blend", None)
    if _dedup_enabled():
        ranked = _dedupe(ranked)
    return ranked[:limit]


async def _append_auxiliary(
    result: list[dict[str, Any]],
    *,
    query: str,
    embedding: list[float] | None,
    tenant_id: str,
    _tenant: str,
    include_insights: bool,
    include_episodes: bool,
    include_chat_turns: bool,
) -> list[dict[str, Any]]:
    """Append cross-domain insights, episodic summaries, and verbatim chat turns.

    Each leg is best-effort. Shared by the reranker and fallback paths so the
    populated episode store is no longer dropped on the reranked path (WS-1.3).
    """
    if include_insights:
        try:
            insights = await search_insights(query, limit=3, tenant_id=tenant_id)
            result.extend(insights)
        except Exception:
            pass  # Insight search is best-effort
    if include_episodes:
        try:
            from robothor.memory.episodes import search_episodes

            result.extend(await search_episodes(query, limit=3, tenant_id=tenant_id))
        except Exception:
            pass  # Episode search is best-effort
    if include_chat_turns and embedding is not None:
        try:
            from robothor.engine.chat_store import search_chat_turns

            turns = await asyncio.to_thread(
                search_chat_turns, embedding, limit=5, tenant_id=_tenant
            )
            for t in turns:
                t["fact_text"] = f"[{t['role']}] {t['content']}"
                t["category"] = "chat_turn"
                t["confidence"] = 0.5
                t["rrf_score"] = 0.3 * float(t.get("similarity", 0))
            result.extend(turns)
        except Exception:
            pass  # Chat turn search is best-effort
    return result


async def search_facts(
    query: str,
    limit: int = 10,
    active_only: bool = True,
    use_reranker: bool | None = None,
    expand_entities: bool = False,
    include_insights: bool = False,
    include_episodes: bool = False,
    include_chat_turns: bool = False,
    tenant_id: str = "",
    scope: DataScope | None = None,
) -> list[dict[str, Any]]:
    """Hybrid search: vector similarity + BM25 keyword matching with RRF fusion.

    Pipeline:
        1. Vector search: top 30 by cosine similarity (semantic)
        2. BM25 search: top 30 by ts_rank (keyword)
        3. Reciprocal Rank Fusion: score = 1/(60+rank_vector) + 1/(60+rank_bm25)
        4. Optional: entity-graph expansion for associated facts
        5. Optional: reranker (cross-encoder) for precision

    Args:
        query: Search query text.
        limit: Maximum number of results.
        active_only: If True, only return active (non-superseded) facts.
        use_reranker: If True, run reranker on candidates. If None (default),
            honors MEMORY_RERANK_ENABLED env flag (on by default).
        expand_entities: If True, pull related entity facts.
        scope: Optional "own data + shared" DataScope (Task 5, Unified
            Identity Context). ``None`` (the default — every pre-existing
            caller) is unrestricted, byte-identical to pre-Task-5 SQL. A
            restricted scope adds ``(person_id = %s OR person_id IS NULL)``
            to both candidate-generating queries below, AND to the
            entity-graph expansion query (``expand_entities=True`` — it
            queries the same ``memory_facts`` table and must not be able to
            pull in another person's facts through the expansion fan-out).
            Other auxiliary paths (insights, episodes, chat-turn merge in
            ``_append_auxiliary``) are unaffected — see
            robothor/identity/scope.py and the Task 5 report for the
            documented rationale.

    Returns:
        List of matching fact dictionaries sorted by relevance.
    """
    if use_reranker is None:
        use_reranker = _reranker_enabled_default()

    # Hybrid search treats the embedding as one leg, not a prerequisite: when
    # the embedding service is down (GPU wedge, post-boot model load), degrade
    # to the BM25 keyword leg instead of failing the whole memory read path.
    degraded: str | None = None
    try:
        embedding = await llm_client.get_embedding_async(query)
    except httpx.HTTPError as embed_exc:
        logger.warning(
            "search_facts: embedding fetch failed (%s) — degrading to keyword-only search",
            type(embed_exc).__name__,
        )
        embedding = None
        degraded = "keyword-only (embedding service unavailable)"

    def _mark_degraded(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if degraded is not None:
            for r in rows:
                r["degraded"] = degraded
        return rows

    active_clause = "AND is_active = TRUE" if active_only else ""
    scope_clause = ""
    scope_params: tuple[Any, ...] = ()
    if scope is not None and scope.restricted:
        scope_clause = "AND (person_id = %s OR person_id IS NULL)"
        scope_params = (scope.person_id,)
    # WS-1.4: modestly widen the candidate pool when the wide-rerank path is on,
    # giving the blend more headroom. Kept conservative because the cross-encoder
    # scores every candidate (per-candidate Ollama cost).
    fetch_limit = max(45, limit * 3) if _rerank_wide_enabled() else max(30, limit * 3)
    _tenant = tenant_id or DEFAULT_TENANT

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Widen the HNSW candidate budget for this txn so the active-row filter
        # has headroom. Live build is pgvector 0.8.2 with a partial active index
        # (migrations 073/074) + optional iterative_scan; see apply_hnsw_session.
        apply_hnsw_session(cur)

        # Vector search — also pull the ranking signals (importance/recency/access).
        # Skipped entirely when the embedding fetch degraded: BM25 carries the search.
        vector_results: list[dict[str, Any]] = []
        if embedding is not None:
            cur.execute(
                f"""
                SELECT id, fact_text, category, entities, confidence, source_type,
                       metadata, created_at, importance_score, access_count, superseded_by,
                       person_id,
                       EXTRACT(EPOCH FROM (now() - created_at)) as age_seconds,
                       1 - (embedding <=> %s::vector) as similarity
                FROM memory_facts
                WHERE embedding IS NOT NULL AND tenant_id = %s {active_clause} {scope_clause}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (embedding, _tenant, *scope_params, embedding, fetch_limit),
            )
            vector_results = [dict(r) for r in cur.fetchall()]

        # BM25 keyword search
        cur.execute(
            f"""
            SELECT id, fact_text, category, entities, confidence, source_type,
                   metadata, created_at, importance_score, access_count, superseded_by,
                   person_id,
                   EXTRACT(EPOCH FROM (now() - created_at)) as age_seconds,
                   ts_rank(tsv, plainto_tsquery('english', %s)) as bm25_score
            FROM memory_facts
            WHERE tsv @@ plainto_tsquery('english', %s) AND tenant_id = %s
              {active_clause} {scope_clause}
            ORDER BY ts_rank(tsv, plainto_tsquery('english', %s)) DESC
            LIMIT %s
            """,
            (query, query, _tenant, *scope_params, query, fetch_limit),
        )
        bm25_results = [dict(r) for r in cur.fetchall()]

    # Reciprocal Rank Fusion
    vector_ranks = {r["id"]: rank for rank, r in enumerate(vector_results)}
    bm25_ranks = {r["id"]: rank for rank, r in enumerate(bm25_results)}

    all_ids = set(vector_ranks.keys()) | set(bm25_ranks.keys())
    all_results_by_id: dict[int, dict[str, Any]] = {}
    for r in vector_results + bm25_results:
        if r["id"] not in all_results_by_id:
            all_results_by_id[r["id"]] = r

    k = 60  # RRF constant
    rrf_scores: dict[int, float] = {}
    for fact_id in all_ids:
        score = 0.0
        if fact_id in vector_ranks:
            score += 1.0 / (k + vector_ranks[fact_id])
        if fact_id in bm25_ranks:
            score += 1.0 / (k + bm25_ranks[fact_id])
        rrf_scores[fact_id] = score

    sorted_ids = sorted(rrf_scores, key=lambda k: rrf_scores[k], reverse=True)
    candidates = []
    for fact_id in sorted_ids:
        r = all_results_by_id[fact_id]
        r["rrf_score"] = round(rrf_scores[fact_id], 6)
        candidates.append(r)

    # Entity-graph expansion (best-effort)
    if expand_entities and candidates:
        try:
            from robothor.memory.entities import get_entity

            mentioned_entities: set[str] = set()
            for r in candidates[:5]:
                for e in r.get("entities") or []:
                    mentioned_entities.add(e)

            expansion_ids = {r["id"] for r in candidates}
            for entity_name in list(mentioned_entities)[:3]:
                entity = await get_entity(entity_name, tenant_id=_tenant)
                if entity and entity.get("relations"):
                    for rel in entity["relations"][:3]:
                        # get_entity builds relations with `SELECT r.*, e.name
                        # AS target_name` (outgoing) / `AS source_name`
                        # (incoming) — memory_relations itself has no `target`
                        # or `source` column. Reading the bare keys meant
                        # related_name was always empty and this entire
                        # expansion branch was unreachable, silently, because
                        # the block is best-effort. Bare keys are kept last as
                        # a fallback in case a caller hands us a flatter shape.
                        related_name = (
                            rel.get("target_name")
                            or rel.get("source_name")
                            or rel.get("target")
                            or rel.get("source")
                            or ""
                        )
                        if related_name:
                            with get_connection() as conn:
                                cur = conn.cursor(cursor_factory=RealDictCursor)
                                cur.execute(
                                    f"""
                                    SELECT id, fact_text, category, entities, confidence,
                                           source_type, metadata, created_at, importance_score,
                                           person_id
                                    FROM memory_facts
                                    WHERE is_active = TRUE AND tenant_id = %s
                                      AND %s = ANY(entities)
                                      AND importance_score > 0.5
                                      AND id != ALL(%s)
                                      {scope_clause}
                                    ORDER BY importance_score DESC, created_at DESC
                                    LIMIT 2
                                    """,
                                    (_tenant, related_name, list(expansion_ids), *scope_params),
                                )
                                for r in cur.fetchall():
                                    r = dict(r)
                                    r["rrf_score"] = 0.005
                                    r["source"] = "entity_expansion"
                                    candidates.append(r)
                                    expansion_ids.add(r["id"])
        except Exception:
            # Best-effort by design — a graph miss must not fail the search.
            # But it stays *logged*: a bare `pass` here is what let a key-name
            # mismatch disable expansion entirely without a single symptom.
            logger.debug("entity expansion failed (best-effort)", exc_info=True)

    # Optional reranker pass
    if use_reranker and candidates:
        try:
            import time

            from robothor.rag.reranker import rerank_with_fallback

            for c in candidates:
                c["content"] = c.get("fact_text", "")
            t0 = time.time()
            # WS-1: decouple the reranker survivor pool from the final limit so
            # the blend can rescue a fresh/high-importance fact the cross-encoder
            # under-scores. When off, behaviour is unchanged (truncate to limit).
            rerank_topk = max(limit * 4, 24) if _rerank_wide_enabled() else limit
            reranked: list[dict[str, Any]] = await rerank_with_fallback(
                query, candidates, top_k=rerank_topk
            )
            logger.info(
                "search_facts rerank: %d candidates → %d survivors (top_k=%d) in %dms",
                len(candidates),
                len(reranked),
                rerank_topk,
                int((time.time() - t0) * 1000),
            )
            # Unified-score re-rank: relevance-dominant, with importance/recency/
            # access as tie-breakers (surfaces the most recent of equally-relevant hits).
            if _rank_blend_enabled():
                reranked = _blend_rank(reranked, limit)
            else:
                reranked = reranked[:limit]
            if _episode_merge_enabled():
                reranked = await _append_auxiliary(
                    reranked,
                    query=query,
                    embedding=embedding,
                    tenant_id=tenant_id,
                    _tenant=_tenant,
                    include_insights=include_insights,
                    include_episodes=include_episodes,
                    include_chat_turns=include_chat_turns,
                )
            elif include_insights:
                # Legacy reranked-path behaviour (flag off): insights only.
                try:
                    insights = await search_insights(query, limit=3, tenant_id=tenant_id)
                    reranked.extend(insights)
                except Exception:
                    pass
            return _mark_degraded(reranked)
        except Exception as e:
            logger.warning("search_facts rerank failed, falling back: %s", e)

    result = _blend_rank(candidates, limit) if _rank_blend_enabled() else candidates[:limit]
    return _mark_degraded(
        await _append_auxiliary(
            result,
            query=query,
            embedding=embedding,
            tenant_id=tenant_id,
            _tenant=_tenant,
            include_insights=include_insights,
            include_episodes=include_episodes,
            include_chat_turns=include_chat_turns,
        )
    )


def get_memory_stats(tenant_id: str = "") -> dict[str, Any]:
    """Get memory system statistics from the facts-based memory system.

    Args:
        tenant_id: Tenant scope for data isolation.

    Returns counts for total facts, active facts, superseded facts,
    scored facts, entities, and relations.
    """
    _tenant = tenant_id or DEFAULT_TENANT

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            "SELECT COUNT(*) as count FROM memory_facts WHERE tenant_id = %s",
            (_tenant,),
        )
        total_facts = cur.fetchone()["count"]

        cur.execute(
            "SELECT COUNT(*) as count FROM memory_facts WHERE is_active = TRUE AND tenant_id = %s",
            (_tenant,),
        )
        active_facts = cur.fetchone()["count"]

        cur.execute(
            "SELECT COUNT(*) as count FROM memory_facts "
            "WHERE is_active = FALSE AND superseded_by IS NOT NULL AND tenant_id = %s",
            (_tenant,),
        )
        superseded_count = cur.fetchone()["count"]

        cur.execute(
            "SELECT COUNT(*) as count FROM memory_facts "
            "WHERE importance_score != 0.5 AND is_active = TRUE AND tenant_id = %s",
            (_tenant,),
        )
        scored_count = cur.fetchone()["count"]

        cur.execute(
            "SELECT COUNT(*) as count FROM memory_entities WHERE tenant_id = %s",
            (_tenant,),
        )
        entity_count = cur.fetchone()["count"]

        cur.execute(
            "SELECT COUNT(*) as count FROM memory_relations WHERE tenant_id = %s",
            (_tenant,),
        )
        relation_count = cur.fetchone()["count"]

    return {
        "total_facts": total_facts,
        "active_facts": active_facts,
        "superseded_count": superseded_count,
        "scored_count": scored_count,
        "entity_count": entity_count,
        "relation_count": relation_count,
    }


def search_facts_compat(
    query: str,
    limit: int = 10,
    tenant_id: str = "",
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Sync compatibility wrapper for search_facts, matching the old tiers API.

    Maps fact fields to the RAG pipeline's expected format:
        fact_text -> content, source_type -> content_type, adds tier: "facts"

    Args:
        query: Search query text.
        limit: Maximum number of results.
        tenant_id: Tenant scope for data isolation.

    Returns:
        List of result dicts with 'content', 'content_type', 'tier' keys.
    """
    import asyncio

    results = asyncio.run(search_facts(query, limit=limit, tenant_id=tenant_id))
    compat_results = [
        {
            **r,
            "content": r.get("fact_text", ""),
            "content_type": r.get("source_type", "unknown"),
            "tier": "facts",
            "similarity": r.get("rrf_score", 0.0),
        }
        for r in results
    ]
    return compat_results
