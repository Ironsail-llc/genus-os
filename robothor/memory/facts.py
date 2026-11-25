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
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

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
from robothor.memory.recall import Candidate, apply_reserved, fuse, relevance_from_rank
from robothor.memory.vector_tuning import apply_hnsw_session
from robothor.memory.visibility import fact_visibility_sql, graph_reader, readers_for

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

#: The six entity types a mention may carry (``entities.VALID_ENTITY_TYPES``;
#: ``entities`` cannot be imported here without a cycle, and a test keeps the
#: two lists equal).
MENTION_TYPES = ["person", "project", "organization", "technology", "location", "event"]

# JSON schema for structured output: one extraction returns, per fact, its
# text, category and confidence, the names it mentions with their type, the
# relations it states between those names, and when the thing happened (only
# when the text says or clearly implies it). Strict: every field is required
# and nothing else is allowed, so constrained decoding (Ollama's grammar, the
# remote provider's strict json_schema) produces exactly this shape.
FACT_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "fact_text",
                    "category",
                    "confidence",
                    "event_time",
                    "mentions",
                    "relations",
                ],
                "properties": {
                    "fact_text": {"type": "string"},
                    "category": {"type": "string", "enum": VALID_CATEGORIES},
                    "confidence": {"type": "number"},
                    "event_time": {"type": ["string", "null"]},
                    "mentions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["surface_form", "entity_type"],
                            "properties": {
                                "surface_form": {"type": "string"},
                                "entity_type": {"type": "string", "enum": MENTION_TYPES},
                            },
                        },
                    },
                    "relations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["source", "relation", "target"],
                            "properties": {
                                "source": {"type": "string"},
                                "relation": {"type": "string"},
                                "target": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }
    },
}


def build_extraction_prompt(
    content: str, *, context: str | None = None, reference_date: str | None = None
) -> str:
    """Build the LLM prompt for fact extraction.

    ``context`` is shown to the model, clearly delimited, as read-only
    background (the other side of a conversation, say): facts come only from
    the content. The content is always last, after a ``Content:`` line.
    """
    from datetime import UTC, datetime

    today = reference_date or datetime.now(UTC).date().isoformat()
    background = ""
    if context and context.strip():
        background = f"""
Read-only context (background only: do not extract facts from it; use it only to
understand the content):
<<<CONTEXT
{context.strip()}
CONTEXT>>>
"""
    return f"""Extract specific, memorable facts from the content below.

Rules:
- Each fact MUST be a complete sentence with a subject and predicate
- Each fact MUST reference at least one specific named entity (person, organization, place, project, technology)
- Each fact MUST be specific to this content — NOT generic knowledge anyone would know
- Include temporal context when present (dates, "yesterday", "next week", etc.)
- Categorize each fact: decision (someone decided X), preference (someone prefers X), event (X happened), resolution (someone resolved/closed/confirmed an open item, alert, or question), contact (relationship info), project (work/technical), personal (personal life), technical (system/code)
- mentions: every specific name the fact mentions, written exactly as it appears in the fact, with its type (person, project, organization, technology, location, event)
- relations: relationships the fact itself states between two of its mentions, as source, a short verb phrase (works at, manages, uses, ...), target. Use only names listed in that fact's mentions. Leave it empty when the fact states none.
- event_time: when the thing the fact describes happened or will happen, as an ISO date (YYYY-MM-DD) or date-time, ONLY when the content states it or clearly implies it (relative dates are relative to {today}). Otherwise null. Never guess.

Skip:
- Greetings, filler, partial sentences
- Generic statements ("X is a company", "X is available", "meetings are important")
- Single words or numbers without context
- Facts that don't mention any specific person, organization, or project by name
{background}
Content:
{content}"""


def _event_time(value: Any) -> str | None:
    """An ISO date or date-time the model gave, or None. Anything else is dropped."""
    from datetime import date, datetime

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            date.fromisoformat(text)
        else:
            datetime.fromisoformat(text)
    except ValueError:
        return None
    return text


def _mentions(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Typed mentions of one extracted fact, de-duplicated, in order.

    A legacy item (``entities`` names, no ``mentions``) yields untyped mentions.
    An unknown type is kept as ``None`` rather than guessed.
    """
    raw = item.get("mentions")
    pairs: list[tuple[Any, Any]]
    if isinstance(raw, list):
        pairs = [(m.get("surface_form"), m.get("entity_type")) for m in raw if isinstance(m, dict)]
    else:
        names = item.get("entities")
        pairs = [(str(n), None) for n in names if n] if isinstance(names, list) else []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for surface, etype in pairs:
        if not isinstance(surface, str) or not surface.strip():
            continue
        name = surface.strip()
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        t = etype.strip().lower() if isinstance(etype, str) else None
        out.append({"surface_form": name, "entity_type": t if t in MENTION_TYPES else None})
    return out


def _relations(item: dict[str, Any]) -> list[dict[str, str]]:
    raw = item.get("relations")
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for r in raw:
        if not isinstance(r, dict):
            continue
        parts = [r.get(k) for k in ("source", "relation", "target")]
        if all(isinstance(p, str) and p.strip() for p in parts):
            source, relation, target = (str(p).strip() for p in parts)
            out.append({"source": source, "relation": relation, "target": target})
    return out


def _parse(raw: str) -> list[dict[str, Any]] | None:
    """Parsed items, or None when the response is not the JSON asked for."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, dict):
        parsed = parsed["facts"] if isinstance(parsed.get("facts"), list) else [parsed]
    if not isinstance(parsed, list):
        return None
    return [p for p in parsed if isinstance(p, dict)]


def parse_extraction_response(raw: str) -> list[dict[str, Any]]:
    """Parse the LLM's extraction response into structured facts.

    Accepts the current shape (``{"facts": [...]}`` with typed mentions,
    relations and event time per fact) and the older one (a bare array with
    ``entities`` names). Handles markdown fences, single objects, missing
    fields, and out-of-range confidence values. Every fact carries
    ``mentions``, ``relations``, ``event_time`` and ``entities`` (the mention
    names, for the ``memory_facts.entities`` column).

    Args:
        raw: Raw LLM response text.

    Returns:
        List of validated fact dictionaries.
    """
    valid_facts: list[dict[str, Any]] = []
    for item in _parse(raw) or []:
        fact_text = item.get("fact_text", "")
        if not isinstance(fact_text, str) or not fact_text.strip():
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

        mentions = _mentions(item)
        valid_facts.append(
            {
                "fact_text": fact_text.strip(),
                "category": category,
                "entities": [m["surface_form"] for m in mentions],
                "confidence": confidence,
                "mentions": mentions,
                "relations": _relations(item),
                "event_time": _event_time(item.get("event_time")),
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


@dataclass
class Extraction:
    """What one extraction produced.

    ``error`` is ``None`` when the model answered in the shape asked for (an
    empty ``facts`` then means there was nothing worth remembering), else why it
    did not: ``timeout``, ``empty``, ``unparseable`` or the exception's type.
    """

    facts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


async def extract_facts(
    content: str,
    max_retries: int = 3,
    *,
    timeout: float = 180.0,
    context: str | None = None,
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

    :func:`extract_facts_detailed` also says whether the extraction failed;
    this returns the facts only.

    Args:
        content: Unstructured text content.
        max_retries: Number of attempts before giving up.
        timeout: Hard cap in seconds for all attempts combined.
        context: Read-only background shown to the model, never extracted from.

    Returns:
        List of extracted fact dictionaries, or empty list on failure.
    """
    out = await extract_facts_detailed(content, max_retries, timeout=timeout, context=context)
    return out.facts


async def extract_facts_detailed(
    content: str,
    max_retries: int = 3,
    *,
    timeout: float = 180.0,
    context: str | None = None,
) -> Extraction:
    """:func:`extract_facts`, saying whether it failed and why.

    One model call per attempt returns the facts with their typed mentions,
    relations and event time (:data:`FACT_EXTRACTION_SCHEMA`). There is no
    second entity-extraction call.
    """
    try:
        return await asyncio.wait_for(
            _extract_facts_inner(content, max_retries, context=context), timeout=timeout
        )
    except TimeoutError:
        logger.warning("extract_facts hard timeout (%.0fs) — returning empty", timeout)
        return Extraction([], "timeout")


async def _extract_facts_inner(
    content: str,
    max_retries: int,
    *,
    context: str | None = None,
) -> Extraction:
    """Inner implementation of extract_facts (no timeout wrapper)."""
    prompt = build_extraction_prompt(content, context=context)
    error: str | None = "empty"
    for attempt in range(max_retries):
        try:
            logger.info("extract_facts attempt %d/%d", attempt + 1, max_retries)
            raw = await generation.generate(
                prompt=prompt,
                system="Extract facts from the content as JSON.",
                # Named constant, not a literal: 1024 truncated 59% of
                # production extractions mid-JSON, and the caller scored the
                # unparseable body as "no facts in this conversation".
                max_tokens=generation.EXTRACTION_MAX_TOKENS,
                format=FACT_EXTRACTION_SCHEMA,
                think=False,
            )
            logger.info("LLM returned %d chars", len(raw) if raw else 0)
            if not raw or not raw.strip():
                logger.warning("Empty response from LLM on attempt %d", attempt + 1)
                error = "empty"
                continue
            if _parse(raw) is None:
                logger.warning("Unparseable extraction on attempt %d", attempt + 1)
                error = "unparseable"
                continue
            error = None
            facts = parse_extraction_response(raw)
            if facts:
                logger.info("Parsed %d facts on attempt %d", len(facts), attempt + 1)
                return Extraction(facts)
            logger.warning("Parsed 0 facts from %d chars on attempt %d", len(raw), attempt + 1)
        except Exception as e:
            logger.warning("extract_facts attempt %d failed: %s", attempt + 1, e)
            error = type(e).__name__
    if error is not None:
        logger.error("extract_facts failed after %d attempts: %s", max_retries, error)
    return Extraction([], error)


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


#: ``store_fact``'s insert: origin, event time, scope and source channel are
#: written with the row (they decide its hash and who reads it), and
#: ``person_id`` only when it is a CRM person of the tenant (its foreign key).
_INSERT_FACT_FULL_SQL = """
    INSERT INTO memory_facts
    (fact_text, category, entities, confidence, source_content, source_type,
     embedding, metadata, tenant_id, content_hash,
     origin, event_time, scope_kind, scope_id, source_channel, person_id)
    SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s, %s, %s,
           (SELECT p.id FROM crm_people p WHERE p.id = %s::uuid AND p.tenant_id = %s)
"""


def _is_uuid(value: Any) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _insert_fact(
    cur: Any,
    params: tuple[Any, ...],
    *,
    tenant_id: str,
    content_hash: str,
    sql: str = _INSERT_FACT_SQL,
) -> int:
    """Insert one memory_facts row and return its id.

    Dedup is unconditional: a byte-identical active fact short-circuits to
    the existing row's id (no new row, no duplicate-key error) via the 078
    partial unique index.
    """
    _write_dedup_enabled()  # emits the deprecation warning if the retired flag is set
    cur.execute(sql + _INSERT_FACT_ON_CONFLICT + " RETURNING id", params)
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
    origin: str | None = None,
    event_time: str | None = None,
    scope_kind: str = "shared",
    scope_id: str | None = None,
    source_channel: str | None = None,
    embedding: list[float] | None = None,
) -> int:
    """Store a fact with its embedding in the database.

    Args:
        fact: Fact dictionary with fact_text, category, entities, confidence.
        source_content: Original content the fact was extracted from.
        source_type: Type of source (conversation, email, etc.).
        metadata: Optional additional metadata.
        tenant_id: Tenant scope for data isolation.
        origin: Who asserted it (``graph_schema.Origin``); None for a caller
            that predates origins (the row is then ranked as agent-generated).
        event_time: When the thing happened, ISO, if the source said so.
        scope_kind / scope_id: Who may read it; shared unless a caller asks.
            A ``user`` scope's ``scope_id`` is its owner (``person:{id}`` or
            ``user:{uid}``); a ``person:`` owner also sets ``person_id`` (when it
            is a CRM person of the tenant), which today's readers filter on. A
            non-shared scope enters the content hash, so a private fact never
            deduplicates into anyone else's.
        source_channel: Where the content came from.
        embedding: A vector already computed for ``fact_text``, to save a call.

    Returns:
        The database ID of the stored fact, or 0 if the quality gate refused it
        in enforce mode.
    """
    from robothor.engine.run_context import benchmark_write_refused
    from robothor.memory.quality import quality_mode, record_shadow_rejection, score_fact

    # A benchmark run may only write the sandbox tenant (incident 2026-09-12).
    # Checked before the quality gate and the embedding: a fact we will not keep
    # should cost neither.
    if benchmark_write_refused(tenant_id or DEFAULT_TENANT, what="store_fact"):
        return 0

    _mode = quality_mode()
    _verdict = None
    if _mode != "off":
        _verdict = score_fact(fact["fact_text"], confidence=fact.get("confidence"))
        if not _verdict.accept and _mode == "enforce":
            # Refused BEFORE the embedding call: a fact we will not keep should
            # not cost an embedding. Measured on a live corpus, this gate
            # refuses under 1%.
            logger.info(
                "store_fact refused by quality gate: %s | %.60s",
                _verdict.reason,
                fact["fact_text"],
            )
            return 0

    if embedding is None:
        embedding = await llm_client.get_embedding_async(fact["fact_text"])
    resolved_tenant = tenant_id or DEFAULT_TENANT
    kind = scope_kind or "shared"
    sid = None if kind == "shared" else (str(scope_id) if scope_id else None)
    if kind != "shared" and sid is None:
        raise ValueError(f"store_fact: a {kind!r} fact needs a scope_id")
    content_hash = compute_fact_hash(
        fact["fact_text"],
        tenant_id=resolved_tenant,
        category=fact["category"],
        scope_kind=kind,
        scope_id=sid,
    )
    # memory_facts.person_id references crm_people, so a private fact names its
    # owner only when the owner is a person of this tenant.
    owner = (sid.split(":", 1)[1] if sid.startswith("person:") else sid) if sid else None
    person = owner if kind == "user" and _is_uuid(owner) else None

    def _insert() -> int:
        with get_connection() as conn:
            return _insert_fact(
                conn.cursor(),
                (
                    fact["fact_text"],
                    fact["category"],
                    fact.get("entities", []),
                    fact.get("confidence", 1.0),
                    source_content,
                    (source_type or "")[:50] or None,
                    embedding,
                    json.dumps(metadata or {}),
                    resolved_tenant,
                    content_hash,
                    origin,
                    event_time,
                    kind,
                    sid,
                    (source_channel or "")[:50] or None,
                    person,
                    resolved_tenant,
                ),
                tenant_id=resolved_tenant,
                content_hash=content_hash,
                sql=_INSERT_FACT_FULL_SQL,
            )

    # Off the event loop: the write path reaches this from the engine's loop.
    fact_id = await asyncio.to_thread(_insert)

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

    from robothor.engine.run_context import benchmark_write_refused

    # The boundary, not a call site. ``MEMORY_ASYNC_WRITE`` is off by default,
    # so ``store_memory`` reaches the database through here rather than through
    # the write-job queue — guarding only the queue would have left the path
    # that caused incident 2026-09-12 open.
    if benchmark_write_refused(tenant_id or DEFAULT_TENANT, what="store_facts_batch"):
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

    A changed ``fact_text`` is re-embedded between the read and the write. That
    call is not bounded here (it has the provider client's own timeout and
    retries), so a slow provider delays the write and widens the unlocked gap
    after the drift check. A caller that needs a bound sets its own, as the
    bridge's edit route does.
    """
    resolved_tenant = tenant_id or DEFAULT_TENANT

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT fact_text, tenant_id, category, person_id, content_hash,
                   scope_kind, scope_id
              FROM memory_facts
             WHERE id = %s AND tenant_id = %s
            """,
            (fact_id, resolved_tenant),
        )
        row = cur.fetchone()
        if row is None:
            return {"error": "not_found", "fact_id": fact_id}

        (
            current_text,
            current_tenant,
            current_category,
            current_person,
            stored_hash,
            scope_kind,
            scope_id,
        ) = row

        decision = evaluate_drift(
            stored_hash,
            fact_text=current_text or "",
            tenant_id=current_tenant or resolved_tenant,
            category=current_category or "",
            person_id=str(current_person) if current_person else None,
            scope_kind=scope_kind,
            scope_id=scope_id,
        )

        if decision.drift_detected:
            recomputed = compute_fact_hash(
                current_text or "",
                tenant_id=current_tenant or resolved_tenant,
                category=current_category or "",
                person_id=str(current_person) if current_person else None,
                scope_kind=scope_kind,
                scope_id=scope_id,
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
        scope_kind=scope_kind,
        scope_id=scope_id,
    )

    # A changed text gets a fresh vector, the way store_fact embeds a new
    # fact, so search ranks it by its new words. Embedded between the two
    # connections: the call must not hold a pool slot. On failure the old
    # vector stays (nothing backfills a missing one) and the row says it is
    # stale; a fresh vector clears that flag.
    vector: list[float] | None = None
    merged: dict[str, Any] = dict(metadata or {})
    if fact_text != (current_text or ""):
        try:
            vector = await llm_client.get_embedding_async(fact_text)
        except Exception as exc:  # noqa: BLE001 -- any provider failure degrades the same way
            logger.warning(
                "update_fact: fact %d saved without a fresh embedding: %s",
                fact_id,
                type(exc).__name__,
            )
            merged["embedding_stale"] = True
    cleared = ["embedding_stale"] if vector is not None else []

    with get_connection() as conn:
        cur = conn.cursor()
        # Metadata is merged onto the stored object, never replaced, so keys the
        # caller did not send (operator_confirmed among them) survive.
        cur.execute(
            """
            UPDATE memory_facts
               SET fact_text = %s,
                   category = COALESCE(%s, category),
                   embedding = COALESCE(%s::vector, embedding),
                   metadata = (COALESCE(metadata, '{}'::jsonb) - %s::text[]) || %s::jsonb,
                   content_hash = %s,
                   updated_at = NOW()
             WHERE id = %s AND tenant_id = %s
            """,
            (
                fact_text,
                category,
                vector,
                cleared,
                json.dumps(merged),
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
        embedding = await _embed_query(query)
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
    keeps ``max(limit*4, 24)`` survivors (bounded by the reranker's candidate
    cap) and the blend does the final cut, with a small bonus for
    ``rerank_relevant=yes`` (a graded score of at least 0.5). It also widens
    each fact leg's fetch to ``LEG_FETCH_MIN_WIDE``.
    """
    raw = os.environ.get("MEMORY_RERANK_WIDE", "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _episode_merge_enabled() -> bool:
    """Merge episodes/insights/chat-turns on the RERANKED path too (WS-1.3).

    Default OFF. The reranker branch used to return before the
    include_episodes / second include_insights append, so the (only populated)
    episode store was invisible whenever the reranker was on (the prod default).

    ``search_facts`` no longer reads this flag: the auxiliary stores are fusion
    legs (``_auxiliary_legs``) on the single ranking path, so ``include_*``
    alone decides whether they take part, reranked or not. The reader stays
    until the flag's removal story retires it with the other memory flags.
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


def _assign_relevance(results: list[dict[str, Any]]) -> None:
    """Set ``relevance`` (0..1) on every row: one scale for every leg.

    The reranker's graded score when the row has one, else a function of its
    fused rank (``fused_rank``, or its position when absent). Cosine
    similarity is never used: only the dense leg has it, so mixing it in put
    dense and keyword-only hits on two different scales.
    """
    for i, r in enumerate(results):
        score = r.get("rerank_score")
        if score is None:
            rank = r.get("fused_rank")
            score = relevance_from_rank(i if rank is None else int(rank))
        r["relevance"] = float(score)


#: Weights of the unified blend. Utility took over the 0.05 that raw
#: ``access_count`` had: ranking by how often a fact was retrieved made it
#: retrieved more, whatever it was worth.
BLEND_WEIGHTS: dict[str, float] = {
    "relevance": 0.55,
    "recency": 0.25,
    "importance": 0.15,
    "utility": 0.05,
}

#: Largest relevance lead utility can overturn, other signals equal: utility
#: in [-1, 1] spans exactly ``BLEND_WEIGHTS["utility"]`` of blend, so a fact
#: more relevant by more than this ranks first whatever either's utility.
UTILITY_RANK_MARGIN = BLEND_WEIGHTS["utility"] / BLEND_WEIGHTS["relevance"]


def utility_rank_term(utility: float | None) -> float:
    """Blend contribution of a fact's utility: 0 when neutral or unknown."""
    if utility is None:
        return 0.0
    u = max(-1.0, min(1.0, float(utility)))
    return BLEND_WEIGHTS["utility"] * u / 2.0


def _blend_rank(results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Unified-score re-rank (relevance + recency + importance + utility), then dedupe.

    Per live feedback, recency is weighted heavily (operator wants the freshest
    of several relevant hits surfaced first) while relevance stays the largest
    single signal so on-topic hits keep the top. Near-duplicates are then
    collapsed so the first few results are distinct. Relevance is the single
    scale set by ``_assign_relevance``.
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

    _assign_relevance(results)
    for r in results:
        rel = float(r["relevance"])
        imp = r.get("importance_score")
        imp = 0.5 if imp is None else float(imp)
        age_s = r.get("age_seconds")
        recency = 0.3 if age_s is None else 0.5 ** (float(age_s) / 3600.0 / _RECENCY_HALFLIFE_HOURS)
        blend = (
            BLEND_WEIGHTS["relevance"] * rel
            + BLEND_WEIGHTS["recency"] * recency
            + BLEND_WEIGHTS["importance"] * imp
            + utility_rank_term(r.get("utility"))
        )
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


# ── Recall ranking ──────────────────────────────────────────────────────────
# Every leg returns Candidates (robothor/memory/recall.py); one fusion, one
# reranker pass, one relevance scale in the blend, then reserved places.

# Rows each fact leg fetches: max(LEG_FETCH_MIN, limit * 3).
LEG_FETCH_MIN = 30
LEG_FETCH_MIN_WIDE = 45

# RRF weight per leg (unlisted legs count 1.0). The fan-out legs enter at half
# weight: they still reach the reranker when their best rows are near the head,
# without displacing the primary legs' top ranks.
LEG_WEIGHTS: dict[str, float] = {"graph": 0.5, "entity_expansion": 0.5, "chat_turn": 0.5}

# Places in the final top-k a leg keeps whenever it returned anything (see
# recall.apply_reserved). Graph-derived facts are related to the query, not
# similar to it, so a query-document reranker would otherwise always drop
# them. "graph" is the graph-activation leg (robothor/memory/activation.py);
# "entity_expansion" the one-hop leg that answers when the graph leg is empty
# (the two never both return rows for one search).
RESERVED_PLACES: dict[str, int] = {"graph": 2, "entity_expansion": 1}

_AUX_LIMITS = {"insight": 3, "episode": 3, "chat_turn": 5}

# Conversation segments fetched for fusion: at least this many, else the limit.
SEGMENT_LEG_MIN = 5


async def _embed_query(query: str) -> list[float]:
    """Embed a retrieval query with the model's query instruction."""
    return await llm_client.get_embedding_async(llm_client.format_embedding_query(query))


def _fact_candidates(rows: list[dict[str, Any]], leg: str, score_key: str) -> list[Candidate]:
    out: list[Candidate] = []
    for r in rows:
        score = r.get(score_key)
        out.append(
            Candidate(
                id=r["id"],
                kind="fact",
                text=r.get("fact_text") or "",
                similarity=r.get("similarity"),
                leg_scores={leg: float(score)} if score is not None else {},
                payload=r,
            )
        )
    return out


_FACT_COLUMNS = """id, fact_text, category, entities, confidence, source_type,
                   metadata, created_at, importance_score, access_count, superseded_by,
                   person_id, utility,
                   EXTRACT(EPOCH FROM (now() - created_at)) as age_seconds"""


def _or_query(query: str) -> str:
    """The query's words joined with OR, in websearch_to_tsquery syntax.

    Empty for a single word: the OR form would be the AND form again.
    """
    words = [w for w in re.findall(r"\w+", query or "") if w.lower() != "or"]
    return " or ".join(words) if len(words) > 1 else ""


def _keyword_rows(
    cur: Any,
    query: str,
    tenant: str,
    filters: str,
    filter_params: tuple[Any, ...],
    fetch_limit: int,
) -> list[dict[str, Any]]:
    """Full-text leg: every term first, any term when that finds nothing.

    ``websearch_to_tsquery`` never raises on user text. Scored by
    ``ts_rank_cd`` with normalisation 1|32: divided by 1 + log(document
    length), then mapped into 0..1 by rank / (rank + 1).
    """
    sql = f"""
        SELECT {_FACT_COLUMNS},
               ts_rank_cd(tsv, websearch_to_tsquery('english', %s), 33) as keyword_score
        FROM memory_facts
        WHERE tsv @@ websearch_to_tsquery('english', %s) AND tenant_id = %s {filters}
        ORDER BY keyword_score DESC
        LIMIT %s
        """
    for text in (query, _or_query(query)):
        if not text.strip():
            continue
        cur.execute(sql, (text, text, tenant, *filter_params, fetch_limit))
        rows = [dict(r) for r in cur.fetchall()]
        if rows:
            for r in rows:
                r.setdefault("bm25_score", r.get("keyword_score"))
            return rows
    return []


def _fact_legs(
    query: str,
    embedding: list[float] | None,
    tenant: str,
    filters: str,
    filter_params: tuple[Any, ...],
    fetch_limit: int,
) -> dict[str, list[Candidate]]:
    """The vector and keyword legs over memory_facts, in one transaction."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # ef_search. The iterative scan that lets a narrow tenant/person
        # filter fill its LIMIT is set on the connection at checkout.
        apply_hnsw_session(cur)
        vector_rows: list[dict[str, Any]] = []
        if embedding is not None:
            cur.execute(
                f"""
                SELECT {_FACT_COLUMNS},
                       1 - (embedding <=> %s::vector) as similarity
                FROM memory_facts
                WHERE embedding IS NOT NULL AND tenant_id = %s {filters}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (embedding, tenant, *filter_params, embedding, fetch_limit),
            )
            # relaxed_order iterative scan may return rows slightly out of
            # distance order; the leg's RRF rank must be its similarity order.
            vector_rows = sorted(
                (dict(r) for r in cur.fetchall()),
                key=lambda r: -float(r.get("similarity") or 0.0),
            )
        keyword_rows = _keyword_rows(cur, query, tenant, filters, filter_params, fetch_limit)
    return {
        "vector": _fact_candidates(vector_rows, "vector", "similarity"),
        "keyword": _fact_candidates(keyword_rows, "keyword", "keyword_score"),
    }


# Payload fields the graph leg adds to a row, also when another leg found it
# first (fusion keeps the first leg's payload).
_GRAPH_FIELDS = (
    "origin",
    "origin_recorded",
    "graph_score",
    "graph_hops",
    "graph_path",
    "graph_entity",
    "graph_entity_id",
    "graph_edge_id",
)


#: Time the graph leg of ``search_facts`` may take, enforced by Postgres on
#: the leg's own connection (``activation.graph_candidates_for_query``). On
#: timeout the leg returns nothing and the other legs' results stand. Warm-up
#: has its own, tighter budget (``warmup.GRAPH_WARMUP_BUDGET_SECONDS``).
GRAPH_LEG_BUDGET_SECONDS = 0.25

#: Whether search_facts runs the graph-activation leg unless told otherwise.
#: Off: on the memory evaluation's world, dev split (GEN-875), the leg did not
#: earn its place. Multi-hop top-5 21/126 with it against 20/126 without
#: (top-10 34 against 37); raw detail 27/110 against 40/110, its candidates
#: crowding conversation segments out of the reranked head. Reserved places
#: (2, 1, 0), seed linking (possessives), 10 or 20 facts per entity and a query
#: rerank of its candidates did not change that. The design's fallback: the
#: graph serves identity and current state; this leg runs on an explicit
#: ``include_graph=True``.
GRAPH_LEG_DEFAULT = False


async def _graph_leg(
    query: str,
    legs: dict[str, list[Candidate]],
    tenant: str,
    *,
    reader_scope_id: str | None,
    scope: DataScope | None,
    limit: int,
) -> list[Candidate]:
    """Graph activation from the query's entities and the top hits' entities.

    Off the loop, read-only, best-effort: a failure is a WARNING and the other
    legs still rank. A deliberate search, so every origin is walked and each
    row is labelled with its origin (see robothor/memory/activation.py).
    """
    from robothor.memory import activation

    top = [c.id for c in fuse(legs, weights=LEG_WEIGHTS) if c.kind == "fact"]
    restricted = scope is not None and scope.restricted
    try:
        return await asyncio.to_thread(
            activation.graph_candidates_for_query,
            tenant,
            query,
            top[: activation.SEARCH_SEED_HITS],
            reader_scope_id=reader_scope_id,
            person_restricted=restricted,
            person_id=scope.person_id if restricted and scope is not None else None,
            limit=max(activation.LIMIT, limit),
            deadline=time.monotonic() + GRAPH_LEG_BUDGET_SECONDS,
            connect=get_connection,
        )
    except Exception as e:  # noqa: BLE001 - best-effort leg; the facts still return
        logger.warning("search_facts: graph leg failed: %s: %s", type(e).__name__, e)
        return []


def _label_graph_rows(pool: list[dict[str, Any]], graph: list[Candidate]) -> None:
    """Carry the graph leg's origin and path onto every pooled row it found."""
    by_id = {c.id: c.payload for c in graph}
    for row in pool:
        payload = by_id.get(row.get("id")) if row.get("kind") == "fact" else None
        if payload is None or "graph" not in (row.get("leg_ranks") or {}):
            continue
        for key in _GRAPH_FIELDS:
            if key in payload:
                row[key] = payload[key]


async def _entity_expansion_leg(
    seed: list[Candidate],
    tenant: str,
    scope_clause: str,
    scope_params: tuple[Any, ...],
) -> list[Candidate]:
    """Facts about entities related to the top hits' entities (best-effort)."""
    out: list[Candidate] = []
    try:
        from robothor.memory.entities import get_entity

        mentioned: set[str] = set()
        for c in seed[:5]:
            mentioned.update(c.payload.get("entities") or [])
        exclude = {c.id for c in seed if c.kind == "fact"}
        for entity_name in list(mentioned)[:3]:
            entity = await get_entity(entity_name, tenant_id=tenant)
            for rel in (entity or {}).get("relations", [])[:3]:
                # get_entity aliases the far end as target_name (outgoing) or
                # source_name (incoming); bare keys are a fallback for a flatter
                # shape. Reading only the bare keys once disabled this silently.
                related = (
                    rel.get("target_name")
                    or rel.get("source_name")
                    or rel.get("target")
                    or rel.get("source")
                    or ""
                )
                if not related:
                    continue
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
                        (tenant, related, list(exclude), *scope_params),
                    )
                    rows = [dict(r) for r in cur.fetchall()]
                for r in rows:
                    r["source"] = "entity_expansion"
                    exclude.add(r["id"])
                out.extend(_fact_candidates(rows, "entity_expansion", "importance_score"))
    except Exception:
        # Best-effort by design — a graph miss must not fail the search. But it
        # stays *logged*: a bare `pass` here once hid a key-name mismatch that
        # disabled expansion entirely.
        logger.debug("entity expansion failed (best-effort)", exc_info=True)
    return out


def _aux_candidates(rows: list[dict[str, Any]], kind: str, text_key: str) -> list[Candidate]:
    return [
        Candidate(
            id=r.get("id"),
            kind=kind,
            text=r.get(text_key) or "",
            similarity=r.get("similarity"),
            leg_scores={kind: float(r["similarity"])} if r.get("similarity") is not None else {},
            payload=r,
        )
        for r in rows
    ]


def _segment_candidates(hits: list[dict[str, Any]], query: str) -> list[Candidate]:
    """Conversation segments as candidates of kind ``segment``.

    The reranker reads a bounded excerpt (``Candidate.text`` becomes the row's
    ``content``); the row keeps the whole verbatim text as ``fact_text`` so
    the tool output and the eval scorers see all of it. ``age_seconds`` is from
    the segment's last turn, so the blend's recency treats it like a fact's.
    """
    from datetime import UTC, datetime

    from robothor.memory.segments import rerank_excerpt

    now = datetime.now(UTC)
    out: list[Candidate] = []
    for h in hits:
        ended = h.get("ended_at")
        # A keyword-only hit has no cosine. Leave the key out rather than send
        # None: callers format rows with round(r.get("similarity", 0), 4).
        hit = {k: v for k, v in h.items() if not (k == "similarity" and v is None)}
        payload = {
            **hit,
            "fact_text": h["text"],
            "category": "segment",
            "confidence": 0.5,
            "age_seconds": (now - ended).total_seconds() if ended is not None else None,
        }
        out.append(
            Candidate(
                id=h["id"],
                kind="segment",
                text=rerank_excerpt(h["text"], query),
                similarity=h.get("similarity"),
                leg_scores={"segment": float(h.get("rrf_score") or 0.0)},
                payload=payload,
            )
        )
    return out


async def _auxiliary_legs(
    *,
    query: str,
    embedding: list[float] | None,
    tenant_id: str,
    _tenant: str,
    include_insights: bool,
    include_episodes: bool,
    include_chat_turns: bool,
    include_segments: bool = False,
    limit: int = 10,
    reader_person_id: str | None = None,
    room_ids: Iterable[str] | None = None,
    reader_user_id: str | None = None,
) -> dict[str, list[Candidate]]:
    """Insights, episode summaries, conversation segments and chat turns, as legs.

    Each leg is best-effort: a failure is a WARNING naming the leg, and the
    other legs still rank. They used to be appended after ranking (and the
    episode leg dropped whenever the reranker ran); they are now fused and
    reranked with the facts.

    The ``segment`` leg (dense + keyword over ``memory_segments``) supersedes
    the dense-only ``chat_turn`` leg: when segments are on, chat turns are not
    searched for the same query, so one conversation is not returned twice
    in two shapes. ``reader_person_id`` / ``room_ids`` decide which segments
    the reader may see (``robothor.memory.segments._filters``).
    """
    legs: dict[str, list[Candidate]] = {}
    if include_segments:
        try:
            from robothor.memory.segments import search_segments

            hits = await search_segments(
                query,
                tenant_id=_tenant,
                limit=max(SEGMENT_LEG_MIN, limit),
                reader_person_id=reader_person_id,
                reader_user_id=reader_user_id,
                room_ids=room_ids,
                query_embedding=embedding,
                embed_missing=False,
            )
            legs["segment"] = _segment_candidates(hits, query)
        except Exception as e:  # noqa: BLE001 - best-effort leg; the facts still return
            logger.warning("search_facts: segment leg failed: %s: %s", type(e).__name__, e)
        include_chat_turns = False
    if include_insights:
        try:
            rows = await search_insights(query, limit=_AUX_LIMITS["insight"], tenant_id=tenant_id)
            legs["insight"] = _aux_candidates(rows, "insight", "insight_text")
        except Exception as e:  # noqa: BLE001 - best-effort leg; the facts still return
            logger.warning("search_facts: insight leg failed: %s: %s", type(e).__name__, e)
    if include_episodes:
        try:
            from robothor.memory.episodes import search_episodes

            rows = await search_episodes(query, limit=_AUX_LIMITS["episode"], tenant_id=tenant_id)
            legs["episode"] = _aux_candidates(rows, "episode", "fact_text")
        except Exception as e:  # noqa: BLE001 - best-effort leg; the facts still return
            logger.warning("search_facts: episode leg failed: %s: %s", type(e).__name__, e)
    if include_chat_turns and embedding is not None:
        try:
            from robothor.engine.chat_store import search_chat_turns

            turns = await asyncio.to_thread(
                search_chat_turns, embedding, limit=_AUX_LIMITS["chat_turn"], tenant_id=_tenant
            )
            for t in turns:
                t["fact_text"] = f"[{t['role']}] {t['content']}"
                t["category"] = "chat_turn"
                t["confidence"] = 0.5
                t.setdefault("source", "chat_turn")
            legs["chat_turn"] = _aux_candidates(turns, "chat_turn", "fact_text")
        except Exception as e:  # noqa: BLE001 - best-effort leg; the facts still return
            logger.warning("search_facts: chat turn leg failed: %s: %s", type(e).__name__, e)
    return legs


async def _rank_pool(
    query: str, pool: list[dict[str, Any]], limit: int, use_reranker: bool
) -> list[dict[str, Any]]:
    """Rerank (graded) and blend the fused pool; fused order on reranker failure."""
    blend = _rank_blend_enabled()
    if use_reranker and pool:
        try:
            import time

            from robothor.rag.reranker import rerank_with_fallback

            t0 = time.time()
            # WS-1: with MEMORY_RERANK_WIDE the reranker keeps a wider survivor
            # pool and the blend makes the final cut.
            rerank_topk = max(limit * 4, 24) if _rerank_wide_enabled() else limit
            reranked: list[dict[str, Any]] = await rerank_with_fallback(
                query, pool, top_k=rerank_topk
            )
            logger.info(
                "search_facts rerank: %d candidates → %d survivors (top_k=%d) in %dms",
                len(pool),
                len(reranked),
                rerank_topk,
                int((time.time() - t0) * 1000),
            )
            if blend:
                return _blend_rank(reranked, limit)
            _assign_relevance(reranked)
            return reranked[:limit]
        except Exception as e:
            logger.warning("search_facts rerank failed, falling back: %s", e)
    if blend:
        return _blend_rank(pool, limit)
    _assign_relevance(pool)
    return pool[:limit]


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
    include_segments: bool = True,
    reader_person_id: str | None = None,
    room_ids: Iterable[str] | None = None,
    reader_user_id: str | None = None,
    include_graph: bool | None = None,
    reader_scope_id: str | None = None,
    *,
    admin_erasure_view: bool = False,
) -> list[dict[str, Any]]:
    """Hybrid recall: named legs fused by RRF, graded rerank, one relevance scale.

    Pipeline (contract in robothor/memory/recall.py):
        1. Legs: ``vector`` (HNSW cosine) and ``keyword`` (full text, every
           term, then any term) over memory_facts; ``graph`` (activation
           from the entities the query names and the top hits mention, see
           robothor/memory/activation.py) only when ``include_graph=True``
           (off by default, ``GRAPH_LEG_DEFAULT``); ``entity_expansion``
           when ``expand_entities`` and the graph leg found nothing;
           ``segment`` (raw conversation, on by default); ``insight`` /
           ``episode`` / ``chat_turn`` when asked for. Each fact leg fetches
           max(30, limit * 3).
        2. ``fuse`` — reciprocal rank fusion, k=60, ``LEG_WEIGHTS``.
        3. Reranker (on by default; ``MEMORY_RERANK_ENABLED``) scores the head
           of the fused list on 0..1 and orders by that score; on any error the
           fused order stands.
        4. Blend — relevance (rerank score, else from fused rank) + recency +
           importance + access, supersession demotion; near-duplicates dropped.
        5. ``RESERVED_PLACES`` — a leg keeps its reserved places in the top-k.

    Args:
        query: Search query text.
        limit: Maximum number of results (all legs share it).
        active_only: If True, only return active (non-superseded) facts.
        use_reranker: If True, run reranker on candidates. If None (default),
            honors MEMORY_RERANK_ENABLED env flag (on by default).
        expand_entities: Run the one-hop ``entity_expansion`` leg: facts
            about entities related to the top hits' entities. With
            ``include_graph`` it runs only when the graph leg returns nothing.
        include_graph: Run the graph-activation leg. ``None`` (the default)
            reads ``GRAPH_LEG_DEFAULT`` at call time: off, see its comment for
            why. Its rows
            carry ``origin`` (NULL reads as ``agent_generated``),
            ``graph_hops`` and ``graph_path``; a row found only by it has
            ``source="graph"``. It walks every origin: this is a deliberate
            search, not automatic recall. Read-only, like every leg.
        reader_scope_id: Another id of the reader (``person:{id}``,
            ``user:{uid}``, ``agent:{id}``, or a bare CRM person id). Every
            fact leg returns shared rows plus rows owned by one of the reader's
            ids (``reader_person_id``, ``reader_user_id``, this); the graph walk
            reads as the person, else the user (``visibility.graph_reader``).
            Under a restricted ``scope`` the reader is the scope's person only.
        scope: Optional "own data + shared" DataScope (Task 5, Unified
            Identity Context). ``None`` (the default — every pre-existing
            caller) is unrestricted. A restricted scope adds
            ``(person_id = %s OR person_id IS NULL)`` to both fact legs AND to
            the graph walk, both its edges' evidence and the facts it returns
            (it reads the same ``memory_facts`` table and must not pull in
            another person's facts through the fan-out). The insight, episode and chat-turn
            legs cannot filter by person, so a restricted scope leaves them
            out (the router's rule for unscopable stores) — see
            robothor/identity/scope.py. The segment leg can filter by person
            and stays on; its reader is then the scope's person only.
        include_segments: Fuse a ``segment`` leg of raw conversation
            (``robothor.memory.segments.search_segments``). On by default;
            when on, the ``chat_turn`` leg is not run even if asked for — the
            segment leg covers the same conversation with a keyword leg too.
        reader_person_id: The reader's own person. Segments private to that
            person are returned with the shared ones; without it, shared
            segments only. Applied on every call; under a restricted
            ``scope`` it is replaced by the scope's person.
        room_ids: Workspaces the reader can access; a workspace conversation's
            segments are returned only for these.
        reader_user_id: The reader's own user id; a member's webchat
            conversation (``agent:{agent}:user:{uid}``) is theirs. Without a
            reader person or user, no private segment is returned at all.
        admin_erasure_view: The tenant admin's erasure view: the fact legs
            read every owner scope, private facts included, so the admin can
            find a fact to forget it. Keyword-only and off by default. Only the
            bridge's admin fact search (behind ``require_tenant_admin``) passes
            it; no agent tool, MCP tool or tool argument reaches it
            (``robothor/memory/tests/test_erasure_view_reach.py``). The reader
            arguments do not narrow the fact legs, the graph leg does not run
            (its walk reads as one reader), and a restricted ``scope`` is
            refused with ``ValueError``.

    Returns:
        Rows ordered best first. Each carries its payload (``fact_text``,
        ``category`` …) plus ``kind``, ``leg_ranks`` (rank per leg that found
        it), ``leg_scores``, ``rrf_score``, ``fused_rank``, ``rerank_score``
        (None when not reranked) and ``relevance`` (0..1). Read-only.
    """
    if admin_erasure_view and scope is not None and scope.restricted:
        raise ValueError("the admin erasure view is unrestricted; it takes no restricted scope")
    if use_reranker is None:
        use_reranker = _reranker_enabled_default()

    # Hybrid search treats the embedding as one leg, not a prerequisite: when
    # the embedding service is down (GPU wedge, post-boot model load), degrade
    # to the keyword leg instead of failing the whole memory read path.
    degraded: str | None = None
    embedding: list[float] | None
    try:
        embedding = await _embed_query(query)
    except httpx.HTTPError as embed_exc:
        logger.warning(
            "search_facts: embedding fetch failed (%s) — degrading to keyword-only search",
            type(embed_exc).__name__,
        )
        embedding = None
        degraded = "keyword-only (embedding service unavailable)"

    scope_clause = ""
    scope_params: tuple[Any, ...] = ()
    if scope is not None and scope.restricted:
        scope_clause = "AND (person_id = %s OR person_id IS NULL)"
        scope_params = (scope.person_id,)
    # Who owns a row (scope_kind / scope_id, written by the write path) is
    # checked on every call, whatever the rollout mode: shared rows, plus rows
    # owned by one of the reader's ids. Under a restricted scope the reader is
    # the scope's person only. See robothor/memory/visibility.py. The one
    # exception is the tenant admin's erasure view, which must see a private
    # fact to forget it.
    readers = readers_for(
        scope=scope, person_id=reader_person_id, user_id=reader_user_id, extra=reader_scope_id
    )
    unscopable_ok = scope_clause == ""
    if not admin_erasure_view:
        visible_sql, visible_params = fact_visibility_sql(readers)
        scope_clause = f"{scope_clause} AND {visible_sql}"
        scope_params = (*scope_params, *visible_params)
    filters = f"{'AND is_active = TRUE' if active_only else ''} {scope_clause}"
    fetch_min = LEG_FETCH_MIN_WIDE if _rerank_wide_enabled() else LEG_FETCH_MIN
    _tenant = tenant_id or DEFAULT_TENANT

    # Synchronous DB work inside this coroutine is the pre-existing pattern
    # (it blocks the loop for the two leg queries); it is not widened here.
    legs = _fact_legs(query, embedding, _tenant, filters, scope_params, max(fetch_min, limit * 3))
    graph: list[Candidate] = []
    if include_graph is None:
        include_graph = GRAPH_LEG_DEFAULT
    if include_graph and not admin_erasure_view:
        graph = await _graph_leg(
            query, legs, _tenant, reader_scope_id=graph_reader(readers), scope=scope, limit=limit
        )
        if graph:
            legs["graph"] = graph
    # The one-hop expansion over memory_relations and the entity names on
    # facts; when the graph leg ran and found something, that replaces it.
    if expand_entities and not graph:
        seed = fuse(legs, weights=LEG_WEIGHTS)
        if seed:
            legs["entity_expansion"] = await _entity_expansion_leg(
                seed, _tenant, scope_clause, scope_params
            )
    # Insights, episodes and chat turns carry no person_id: a restricted caller
    # would read summaries built from other people's facts. As in the router,
    # a store that cannot express person scoping is left out, not unfiltered
    # (``unscopable_ok``, decided above before the owner predicate is added).
    # Segments filter by reader themselves, so they stay on. Under a restricted
    # scope the reader is the scope's person and nothing else, whatever the
    # caller passed as reader_person_id.
    segment_reader = scope.person_id if scope is not None and scope.restricted else reader_person_id
    legs.update(
        await _auxiliary_legs(
            query=query,
            embedding=embedding,
            tenant_id=tenant_id,
            _tenant=_tenant,
            include_insights=include_insights and unscopable_ok,
            include_episodes=include_episodes and unscopable_ok,
            include_chat_turns=include_chat_turns and unscopable_ok,
            include_segments=include_segments,
            limit=limit,
            reader_person_id=segment_reader,
            reader_user_id=reader_user_id,
            room_ids=room_ids,
        )
    )

    pool = [c.to_row() for c in fuse(legs, weights=LEG_WEIGHTS)]
    _label_graph_rows(pool, graph)
    ranked = await _rank_pool(query, pool, limit, use_reranker)
    # Deduped here, not assumed: with MEMORY_RANK_BLEND=0 the head never went
    # through the blend's dedupe and may hold a near-duplicate pair itself.
    unique_head = _dedupe(ranked[:limit])

    def _duplicates_head(row: dict[str, Any]) -> bool:
        # A reserved pick must not bring back a near-duplicate. _dedupe keeps
        # earlier rows, so a row it drops from the end duplicates the head.
        return _dedup_enabled() and len(_dedupe([*unique_head, row])) == len(unique_head)

    result = apply_reserved(ranked, pool, limit, RESERVED_PLACES, exclude=_duplicates_head)
    for r in result:
        if "relevance" not in r:  # a reserved pick the blend never saw
            _assign_relevance([r])
    if degraded is not None:
        for r in result:
            r["degraded"] = degraded
    return result


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
