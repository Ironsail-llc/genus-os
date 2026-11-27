"""
Conflict Resolution & Deduplication for Genus OS Memory System.

Detects duplicate, contradictory, or updated facts before storing them.
Uses semantic similarity search to find related facts, then LLM-based
classification to determine the relationship.

Architecture:
    New fact -> find_similar_facts -> classify_relationship -> act (store/skip/supersede)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.llm import ollama as llm_client
from robothor.memory import generation
from robothor.memory.bitemporal import (
    keep_overruled,
    record_conflict_decision,
    supersede_with_validity,
)
from robothor.memory.confirmed import is_operator_confirmed
from robothor.memory.drift import compute_fact_hash
from robothor.memory.facts import _write_dedup_enabled, store_fact
from robothor.memory.graph_schema import Origin, authority_rank
from robothor.memory.vector_tuning import apply_hnsw_session

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

logger = logging.getLogger(__name__)

# WS-3: above this similarity, a same-category match is the SAME fact re-reported
# (e.g. the agent's own briefing re-ingested), not a new one — reinforce it
# rather than fork a near-duplicate row.
_REINFORCE_THRESHOLD = 0.92

# Numbers, including decimals and thousands separators. Currency symbols and
# units are deliberately excluded — "$100" and "100 dollars" carry the same
# quantity and the digits are what matters.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_differ(a: str, b: str) -> bool:
    """True when two texts disagree on the numbers they contain.

    The reinforce shortcut assumes high lexical similarity means "same fact,
    said again". A number change is the exact opposite: "$100 -> $150" scores
    0.9882 cosine — far above the threshold — while completely reversing the
    fact's meaning. Treating that as a re-report discards the update AND bumps
    the importance of the stale value, so the wrong number gets retrieved more
    often. Prices, ports, versions, dates, times and quantities all live here.

    Compared as sorted multisets so word order cannot manufacture a difference,
    and normalized so "$1,200" and "$1200" are the same amount.
    """

    def _nums(text: str) -> list[str]:
        out = []
        for raw in _NUMBER.findall(text or ""):
            cleaned = raw.replace(",", "")
            # Trim a trailing ".0" so 2 and 2.0 compare equal.
            if "." in cleaned:
                cleaned = cleaned.rstrip("0").rstrip(".")
            out.append(cleaned or "0")
        return sorted(out)

    return _nums(a) != _nums(b)


# ── Same-slot detection ─────────────────────────────────────────────────────
# A slot is (subject, attribute). Two facts in one slot either repeat its value,
# change it (a single-valued attribute), or add a value (a multi-valued one).
# Similarity cannot tell these apart, so the slot is read from the fact's
# canonical predicate when it has one and otherwise from a small lexical frame.
# A fact matching no frame has no slot and keeps the similarity path.

#: Attributes a subject has one value of at a time. Every other attribute the
#: frames name is multi-valued: a new value is one more, not a replacement.
SINGLE_VALUED_ATTRIBUTES = frozenset(
    {
        "residence",
        "employer",
        "manager",
        "role",
        "title",
        "spouse",
        "partner",
        "car",
        "phone",
        "email",
        "address",
        "birthday",
        "age",
        "timezone",
    }
)

#: Canonical predicates (``write_path.normalize_predicate`` of a relation
#: phrase) and the attribute each fills.
PREDICATE_ATTRIBUTES: dict[str, str] = {
    "lives_in": "residence",
    "moved_to": "residence",
    "relocated_to": "residence",
    "resides_in": "residence",
    "based_in": "residence",
    "works_at": "employer",
    "works_for": "employer",
    "employed_by": "employer",
    "married_to": "spouse",
    "managed_by": "manager",
    "reports_to": "manager",
    "drives": "car",
    "likes": "likes",
    "enjoys": "likes",
    "loves": "likes",
    "owns": "owns",
    "visited": "visited",
    "uses": "uses",
    "speaks": "speaks",
    "plays": "plays",
}

_ADV = r"(?:(?:now|currently|recently|just|also|still|actually|finally)\s+)*"
_ART = r"(?:(?:a|an|the|her|his|their|its)\s+)?"
_SUBJ = r"(?P<subj>[^,;:]+?)"

#: (attribute, pattern). Patterns run on the whole fact text, case-insensitive;
#: ``subj`` and ``val`` are required groups, ``attr`` names the attribute for
#: the possessive frames and ``other`` the second option of a preference.
_FRAMES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (attr, re.compile(rx, re.IGNORECASE))
    for attr, rx in (
        (
            "prefers",
            rf"^{_SUBJ}\s+{_ADV}prefers?\s+(?P<val>.+?)\s+(?:over|to|rather than|instead of)\s+"
            r"(?P<other>.+)$",
        ),
        (
            "favorite",
            rf"^{_SUBJ}'s\s+(?:new\s+)?favou?rite\s+(?P<attr>\w+)\s+is\s+{_ADV}(?P<val>.+)$",
        ),
        (
            "possessive",
            rf"^{_SUBJ}'s\s+(?P<attr>manager|boss|role|job title|title|spouse|husband|wife|"
            r"partner|phone number|phone|email address|email|address|birthday|age|time zone|"
            rf"timezone)\s+is\s+{_ADV}(?P<val>.+)$",
        ),
        (
            "residence",
            rf"^{_SUBJ}\s+{_ADV}(?:lives|is living|resides|is based|has moved|moved|"
            rf"has relocated|relocated)\s+(?:back\s+)?(?:from\s+.+?\s+)?(?:in|to|at)\s+(?P<val>.+)$",
        ),
        (
            "employer",
            rf"^{_SUBJ}\s+{_ADV}(?:works|is working|started working|is employed)\s+(?:at|for|by)\s+"
            r"(?P<val>.+)$",
        ),
        ("spouse", rf"^{_SUBJ}\s+is\s+{_ADV}married\s+to\s+(?P<val>.+)$"),
        ("car", rf"^{_SUBJ}\s+{_ADV}drives\s+{_ART}(?P<val>.+)$"),
        (
            "visited",
            rf"^{_SUBJ}\s+{_ADV}(?:has visited|visited|has been to|has travell?ed to)\s+(?P<val>.+)$",
        ),
        ("likes", rf"^{_SUBJ}\s+{_ADV}(?:likes|enjoys|loves)\s+(?P<val>.+)$"),
        ("owns", rf"^{_SUBJ}\s+{_ADV}owns\s+{_ART}(?P<val>.+)$"),
        ("uses", rf"^{_SUBJ}\s+{_ADV}uses\s+{_ART}(?P<val>.+)$"),
        ("speaks", rf"^{_SUBJ}\s+{_ADV}speaks\s+(?P<val>.+)$"),
        ("plays", rf"^{_SUBJ}\s+{_ADV}plays\s+{_ART}(?P<val>.+)$"),
    )
)

_POSSESSIVE_ATTRIBUTES = {
    "boss": "manager",
    "job title": "title",
    "husband": "spouse",
    "wife": "spouse",
    "phone number": "phone",
    "email address": "email",
    "time zone": "timezone",
}

# Where a value stops: punctuation, or a clause about time or cause.
_VALUE_END = re.compile(
    r"\s*(?:[,;!?]|\.(?:\s|$)|\s(?:since|last|this|next|because|as of|after|before|recently|"
    r"yesterday|today|ago|when|while|as a|as an|in \d{4}|in (?:january|february|march|april|"
    r"may|june|july|august|september|october|november|december|spring|summer|autumn|fall|"
    r"winter))\b)",
    re.IGNORECASE,
)
_LEADING_ARTICLE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)
_VALUE_STOPWORDS = frozenset({"a", "an", "the", "in", "at", "of", "to", "for", "and", "now"})
_MAX_SUBJECT_WORDS = 5


def _norm_value(raw: str) -> str:
    text = _VALUE_END.split(raw.strip(), maxsplit=1)[0]
    text = _LEADING_ARTICLE.sub("", text.strip().rstrip(".")).strip()
    return re.sub(r"\s+", " ", text.lower())


def _norm_subject(raw: str) -> str | None:
    text = re.sub(r"\s+", " ", raw.strip().lower())
    text = _LEADING_ARTICLE.sub("", text)
    if not text or len(text.split()) > _MAX_SUBJECT_WORDS:
        return None
    return text


@dataclass(frozen=True)
class FactSlot:
    """One fact's (subject, attribute) and the value it gives that slot.

    ``subject_keys`` holds ``name:<lowercased subject>`` and, when known,
    ``entity:<id>`` for every entity the subject resolves to; two facts share a
    subject when their keys intersect.
    """

    subject_keys: frozenset[str]
    attribute: str
    value: str

    @property
    def single_valued(self) -> bool:
        base = self.attribute.split(":", 1)[0]
        return base in SINGLE_VALUED_ATTRIBUTES or base in ("prefers", "favorite")

    def with_entity_ids(self, ids: Iterable[int]) -> FactSlot:
        keys = self.subject_keys | {f"entity:{int(i)}" for i in ids}
        return FactSlot(frozenset(keys), self.attribute, self.value)

    @property
    def subject_name(self) -> str:
        return next(
            (k.split(":", 1)[1] for k in sorted(self.subject_keys) if k.startswith("name:")), ""
        )


def _subject_keys(subject: str, mention_entities: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    keys = {f"name:{subject}"}
    for m in mention_entities:
        surface = str(m.get("surface_form") or "").strip().lower()
        eid = m.get("entity_id")
        if eid is not None and surface and surface == subject:
            keys.add(f"entity:{int(eid)}")
    return frozenset(keys)


def _slot_from_relations(
    relations: Iterable[Mapping[str, Any]], mention_entities: list[Mapping[str, Any]]
) -> FactSlot | None:
    from robothor.memory.write_path import normalize_predicate

    for rel in relations:
        attribute = PREDICATE_ATTRIBUTES.get(normalize_predicate(rel.get("relation")) or "")
        subject = _norm_subject(str(rel.get("source") or ""))
        value = _norm_value(str(rel.get("target") or ""))
        if attribute and subject and value:
            return FactSlot(_subject_keys(subject, mention_entities), attribute, value)
    return None


def fact_slot(fact: Mapping[str, Any]) -> FactSlot | None:
    """The slot ``fact`` fills, or None when no predicate or frame names one.

    A relation whose canonical predicate is in :data:`PREDICATE_ATTRIBUTES`
    decides first; otherwise the first frame matching ``fact_text``.
    ``mention_entities`` (``[{surface_form, entity_id}]``, a stored fact's
    resolved mentions) adds entity keys to the subject.
    """
    mention_entities = list(fact.get("mention_entities") or [])
    slot = _slot_from_relations(fact.get("relations") or [], mention_entities)
    if slot is not None:
        return slot
    text = str(fact.get("fact_text") or "").strip()
    for attribute, pattern in _FRAMES:
        m = pattern.match(text)
        if not m:
            continue
        subject = _norm_subject(m.group("subj"))
        value = _norm_value(m.group("val"))
        if not subject or not value:
            continue
        groups = m.groupdict()
        if attribute == "prefers":
            other = _norm_value(groups.get("other") or "")
            if not other:
                continue
            attribute = "prefers:" + "|".join(sorted((value, other)))
        elif attribute == "favorite":
            attribute = f"favorite:{str(groups['attr']).lower()}"
        elif attribute == "possessive":
            raw = str(groups["attr"]).lower()
            attribute = _POSSESSIVE_ATTRIBUTES.get(raw, raw)
        return FactSlot(_subject_keys(subject, mention_entities), attribute, value)
    return None


def _value_tokens(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", value.lower())) - _VALUE_STOPWORDS


def compare_slots(new: FactSlot | None, old: FactSlot | None) -> str:
    """How ``new`` relates to ``old``: ``repeat``, ``change``, ``coexist`` or ``unrelated``.

    Same subject and attribute, and one value's words contain the other's:
    ``repeat``. A different value: ``change`` for a single-valued attribute,
    ``coexist`` for a multi-valued one. Anything else: ``unrelated``.
    """
    if new is None or old is None:
        return "unrelated"
    if new.attribute != old.attribute or not (new.subject_keys & old.subject_keys):
        return "unrelated"
    a, b = _value_tokens(new.value), _value_tokens(old.value)
    if a and b and (a <= b or b <= a):
        return "repeat"
    return "change" if new.single_valued else "coexist"


_SENTENCE_STARTERS = frozenset(
    {"the", "a", "an", "this", "that", "it", "he", "she", "they", "we", "i", "on", "in", "at"}
)


def _names(text: str) -> frozenset[str]:
    words = re.findall(r"\b[A-Z][\w'-]*", text or "")
    return frozenset(w.lower() for w in words if w.lower() not in _SENTENCE_STARTERS)


def values_differ(a: str, b: str) -> bool:
    """True when two near-identical texts carry different values.

    The reinforce shortcut treats a high-similarity pair as the same fact said
    again; that holds only when the values are the same. Different numbers,
    different names, or the same words in another order ("tea over coffee",
    "coffee over tea") are a different value.
    """
    if numbers_differ(a, b) or _names(a) != _names(b):
        return True
    wa, wb = re.findall(r"\w+", (a or "").lower()), re.findall(r"\w+", (b or "").lower())
    return sorted(wa) == sorted(wb) and wa != wb


# JSON schema for conflict classification structured output.
CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["new", "duplicate", "update", "contradiction"],
        },
        "reasoning": {"type": "string"},
    },
    "required": ["classification", "reasoning"],
}


async def find_similar_facts(
    query: str,
    limit: int = 5,
    threshold: float = 0.5,
    *,
    tenant_id: str = "",
    scope_kind: str | None = None,
    scope_id: str | None = None,
    embedding: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Find existing facts semantically similar to a query.

    Args:
        query: Text to search for similar facts.
        limit: Maximum number of results.
        threshold: Minimum cosine similarity score (0.0-1.0).
        scope_kind / scope_id: Only facts of exactly this scope. The write path
            always passes them: a private fact must never be reinforced into,
            deduplicated into or superseded by a shared fact or another reader's
            private one, nor the other way round. ``None`` searches every scope
            (the legacy behaviour, for callers that predate scope).
        embedding: The query's vector, when the caller already has it.

    Returns:
        List of similar fact dictionaries with similarity scores.
    """
    if embedding is None:
        embedding = await llm_client.get_embedding_async(query)
    scope_sql = ""
    scope_params: tuple[Any, ...] = ()
    if scope_kind is not None:
        scope_sql = "AND scope_kind = %s AND scope_id IS NOT DISTINCT FROM %s"
        scope_params = (scope_kind, None if scope_kind == "shared" else scope_id)

    tenant = tenant_id or DEFAULT_TENANT

    def _query() -> list[dict[str, Any]]:
        with get_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            apply_hnsw_session(cur)
            cur.execute(
                f"""
            SELECT
                id,
                fact_text,
                category,
                entities,
                confidence,
                source_type,
                is_active,
                metadata,
                origin,
                1 - (embedding <=> %s::vector) as similarity
            FROM memory_facts
            WHERE embedding IS NOT NULL
              AND is_active = TRUE
              AND tenant_id = %s
              {scope_sql}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,  # noqa: S608 -- scope_sql is one of two literals
                (embedding, tenant, *scope_params, embedding, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    # Off the event loop: the write path reaches this from the engine's loop.
    results = await asyncio.to_thread(_query)
    return [r for r in results if r["similarity"] >= threshold]


#: Most facts one slot lookup reads.
SLOT_CANDIDATE_LIMIT = 25


async def find_slot_candidates(
    slot: FactSlot,
    *,
    tenant_id: str = "",
    scope_kind: str = "shared",
    scope_id: str | None = None,
    limit: int = SLOT_CANDIDATE_LIMIT,
) -> tuple[list[int], list[dict[str, Any]]]:
    """Active facts that may fill ``slot``, and the entity ids its subject resolves to.

    One statement, bounded by indexes, never a scan: the subject's name
    through ``lower_entities(entities)`` (GIN, migration 109), and the
    entities of that name (``memory_entities (tenant_id, lower(name))``)
    through ``memory_mentions (tenant_id, entity_id)``, so a fact that named the
    same entity another way is found too. Newest first, at most ``limit``.
    Only the fact's own tenant and exactly its scope are read, as in
    :func:`find_similar_facts`. Each row carries ``mention_entities``
    (``[{surface_form, entity_id}]``) for :func:`fact_slot`.
    """
    name = slot.subject_name
    if not name:
        return [], []
    tenant = tenant_id or DEFAULT_TENANT
    kind = scope_kind or "shared"
    sid = None if kind == "shared" else scope_id

    def _query() -> tuple[list[int], list[dict[str, Any]]]:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                WITH subj AS (
                    SELECT id FROM memory_entities
                     WHERE tenant_id = %(t)s AND lower(name) = %(name)s
                ), cand AS (
                    SELECT id FROM memory_facts
                     WHERE tenant_id = %(t)s AND lower_entities(entities) && ARRAY[%(name)s]::text[]
                    UNION
                    SELECT m.fact_id FROM memory_mentions m
                     WHERE m.tenant_id = %(t)s AND m.entity_id IN (SELECT id FROM subj)
                ), hits AS (
                    SELECT f.id, f.fact_text, f.category, f.entities, f.origin, f.metadata,
                           COALESCE((SELECT json_agg(json_build_object(
                                        'surface_form', m.surface_form, 'entity_id', m.entity_id))
                                       FROM memory_mentions m
                                      WHERE m.fact_id = f.id AND m.entity_id IS NOT NULL),
                                    '[]'::json) AS mention_entities
                      FROM memory_facts f JOIN cand c ON c.id = f.id
                     WHERE f.tenant_id = %(t)s AND f.is_active = TRUE
                       AND f.scope_kind = %(kind)s AND f.scope_id IS NOT DISTINCT FROM %(sid)s
                     ORDER BY f.id DESC
                     LIMIT %(limit)s
                )
                SELECT (SELECT COALESCE(array_agg(id), '{}') FROM subj),
                       (SELECT COALESCE(json_agg(h), '[]'::json) FROM hits h)
                """,
                {"t": tenant, "name": name, "kind": kind, "sid": sid, "limit": int(limit)},
            )
            row = cur.fetchone()
        if not row:
            return [], []
        ids, hits = row
        return [int(i) for i in ids or []], list(hits or [])

    return await asyncio.to_thread(_query)


def build_classification_prompt(new_fact: str, existing_fact: str) -> str:
    """Build LLM prompt for classifying the relationship between two facts."""
    return f"""Compare these two facts and classify their relationship.

Existing fact: "{existing_fact}"
New fact: "{new_fact}"

Classify as one of:
- "new": The facts describe DIFFERENT things, even if they mention the same entity. Default to this when unsure.
- "duplicate": The facts say the EXACT same thing (possibly worded differently). Both would be redundant.
- "update": The new fact REPLACES the old — the old fact would be FALSE if the new one is true. Only use this when the old fact is now outdated/wrong.
- "contradiction": The facts make directly opposing claims about the same specific thing.

Examples:
- "Alex works at Acme Corp" vs "Alex attended a meeting at Acme Corp" → "new" (different claims about same entity)
- "Meeting is at 3pm" vs "Meeting moved to 4pm" → "update" (old time is now wrong)
- "Alex prefers dark mode" vs "Alex prefers dark mode for coding" → "duplicate" (same preference, minor rewording)

IMPORTANT: When two facts mention the same entity but describe different events, actions, or attributes, classify as "new". Only classify as "update" when the old fact would be INCORRECT given the new information.

Return JSON: {{"classification": "<type>", "reasoning": "<brief explanation>"}}
Return ONLY the JSON object, no other text."""


async def classify_relationship(new_fact: str, existing_fact: str) -> dict[str, Any]:
    """Classify the relationship between a new fact and an existing one.

    Args:
        new_fact: The newly extracted fact text.
        existing_fact: The existing fact text from the database.

    Returns:
        Dict with 'classification' (new/duplicate/update/contradiction) and 'reasoning'.
    """
    try:
        prompt = build_classification_prompt(new_fact, existing_fact)
        raw = await generation.generate(
            prompt=prompt,
            system="Classify the relationship between these two facts.",
            max_tokens=256,
            format=CLASSIFICATION_SCHEMA,
        )

        parsed = json.loads(raw.strip())
        classification = parsed.get("classification", "new").lower().strip()
        if classification not in ("new", "duplicate", "update", "contradiction"):
            classification = "new"

        return {
            "classification": classification,
            "reasoning": parsed.get("reasoning", ""),
        }
    except (json.JSONDecodeError, Exception):
        return {"classification": "new", "reasoning": "Failed to classify, treating as new"}


def _reinforce_fact(fact_id: int, *, tenant_id: str = "") -> None:
    """Strengthen an existing fact instead of forking a near-duplicate.

    Repeated mentions of the same event (the dominant churn source) should raise
    the fact's salience, not inflate the table with reworded copies. Nudges
    importance up (capped at 1.0), counts a *reinforcement*, and refreshes
    updated_at.

    This used to increment access_count, which was wrong twice over. The event
    is re-observation, not retrieval, so it belongs on reinforcement_count —
    which had no writer at all, leaving one of compute_decay_score's five inputs
    permanently zero across every row. And because access_count is weighted in
    the retrieval blend (facts._blend_rank), counting it here also inflated a
    fact's search ranking every time something merely mentioned it again.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE memory_facts
            SET importance_score = LEAST(COALESCE(importance_score, 0.5) + 0.05, 1.0),
                reinforcement_count = COALESCE(reinforcement_count, 0) + 1,
                updated_at = NOW()
            WHERE id = %s AND tenant_id = %s
            """,
            (fact_id, tenant_id or DEFAULT_TENANT),
        )


def _supersede_fact(old_id: int, new_id: int, *, tenant_id: str = "") -> None:
    """Mark an old fact as superseded by a new one.

    Delegates to the bi-temporal writer so every supersession path bounds the
    old fact in world-time. There are two callers — here and
    ``resolution.py`` — and a second writer that set ``is_active = FALSE``
    without setting ``valid_to`` would leave facts hidden from the normal read
    path AND invisible to the point-in-time one, which is worse than either
    behaviour alone.
    """
    supersede_with_validity(old_id, new_id, tenant_id=tenant_id, classification="resolution")


def _confirmed_now(fact_id: int, *, tenant_id: str = "") -> bool:
    """Re-read one fact: is it (still there and) operator-confirmed?"""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT metadata FROM memory_facts WHERE id = %s AND tenant_id = %s",
            (fact_id, tenant_id or DEFAULT_TENANT),
        )
        row = cur.fetchone()
    return row is not None and is_operator_confirmed({"metadata": row[0]})


def _find_overruled(
    fact: dict[str, Any],
    kept_id: int,
    *,
    tenant_id: str = "",
    scope_kind: str | None = None,
    scope_id: str | None = None,
) -> int:
    """The id of an earlier inactive copy of ``fact`` overruled by ``kept_id``, or 0.

    ``find_similar_facts`` and the insert's dedup index both see active rows
    only, so without this every re-ingestion of a source that contradicts a
    confirmed fact would store, embed and classify one more dead row.
    """
    tenant = tenant_id or DEFAULT_TENANT
    content_hash = compute_fact_hash(
        fact["fact_text"],
        tenant_id=tenant,
        category=fact.get("category") or "",
        scope_kind=scope_kind,
        scope_id=scope_id,
    )
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM memory_facts "
            "WHERE tenant_id = %s AND content_hash = %s AND superseded_by = %s "
            "AND is_active = FALSE ORDER BY id DESC LIMIT 1",
            (tenant, content_hash, kept_id),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def _authority(row: Mapping[str, Any]) -> int:
    """A stored fact's rank in the trust order; operator-confirmed ranks first."""
    if is_operator_confirmed(row):
        return authority_rank(Origin.OPERATOR_CONFIRMED.value)
    return authority_rank(row.get("origin"))


async def _resolve_slot(
    fact: dict[str, Any],
    slot: FactSlot,
    similar: list[dict[str, Any]],
    store: Callable[[], Awaitable[int]],
    *,
    tenant_id: str,
    scope_kind: str,
    scope_id: str | None,
    origin: str | None,
) -> dict[str, Any] | None:
    """Decide a fact that fills a slot against the slot's current values.

    Candidates are the nearest neighbours already found plus the facts the
    subject's entity index finds (:func:`find_slot_candidates`), all in the
    fact's own tenant and scope. Returns None when none shares the slot, and
    the caller carries on by similarity.

    * repeat (same value): at cosine 0.92 or more in the same category the
      existing fact is reinforced and nothing stored; below that the caller's
      similarity path decides, as before.
    * coexist (a multi-valued attribute): the new fact is stored beside it.
    * change (a single-valued attribute): the new fact is stored and every
      active value of the slot is closed (``superseded_by``, ``valid_to``,
      reason ``changed``), unless one of them outranks the new fact's source;
      then the new fact is stored overruled by it and nothing is closed.
    """
    try:
        entity_ids, rows = await find_slot_candidates(
            slot, tenant_id=tenant_id, scope_kind=scope_kind, scope_id=scope_id
        )
    except Exception as exc:  # noqa: BLE001 - the similarity path still decides
        logger.warning("resolve_and_store: slot lookup failed (%s)", type(exc).__name__)
        entity_ids, rows = [], []
    new_slot = slot.with_entity_ids(entity_ids)
    candidates: dict[int, dict[str, Any]] = {int(r["id"]): r for r in similar}
    for r in rows:
        near = candidates.get(int(r["id"]), {})
        # The lookup's row adds resolved mentions; the similarity is the search's.
        candidates[int(r["id"])] = {**near, **r, "similarity": near.get("similarity")}
    matches = [(r, compare_slots(new_slot, fact_slot(r))) for r in candidates.values()]
    matches = [(r, kind) for r, kind in matches if kind != "unrelated"]
    if not matches:
        return None

    def _record(row: Mapping[str, Any], **kw: Any) -> Awaitable[Any]:
        sim = row.get("similarity")
        return asyncio.to_thread(
            record_conflict_decision,
            tenant_id=tenant_id,
            existing_fact_id=row["id"],
            similarity=float(sim) if sim is not None else None,
            new_fact_text=fact["fact_text"],
            existing_fact_text=row.get("fact_text") or "",
            **kw,
        )

    where = f"same slot ({new_slot.subject_name}, {new_slot.attribute})"
    repeat = next((r for r, kind in matches if kind == "repeat"), None)
    if repeat is not None:
        # The same value. Reinforced only as the similarity rule would (0.92,
        # same category): a restatement in other words, perhaps from a more
        # trusted source, is the classifier's to judge, not swallowed here.
        if not (
            _write_dedup_enabled()
            and float(repeat.get("similarity") or 0) >= _REINFORCE_THRESHOLD
            and (repeat.get("category") or "") == (fact.get("category") or "")
        ):
            return None
        await asyncio.to_thread(_reinforce_fact, repeat["id"], tenant_id=tenant_id)
        await _record(
            repeat,
            classification="reinforced",
            action="reinforced",
            new_fact_id=None,
            reasoning=f"{where}, same value",
        )
        return {"action": "reinforced", "existing_id": repeat["id"], "slot": "repeat"}

    changes = [r for r, kind in matches if kind == "change"]
    if not changes:
        new_id = await store()
        await _record(
            matches[0][0],
            classification="new",
            action="stored",
            new_fact_id=new_id or None,
            reasoning=f"{where}, multi-valued: one more value",
        )
        return {"action": "stored", "new_id": new_id, "slot": "coexist"}

    new_id = await store()
    old_values = [getattr(fact_slot(r), "value", "?") for r in changes[:3]]
    reasoning = f"{where}: " + ", ".join(f"{v} -> {new_slot.value}" for v in old_values)
    outranking = [r for r in changes if _authority(r) > authority_rank(origin)]
    if outranking:
        kept = max(outranking, key=_authority)
        action = "kept_confirmed" if is_operator_confirmed(kept) else "kept_authority"
        if new_id:
            await asyncio.to_thread(keep_overruled, new_id, kept["id"], tenant_id=tenant_id)
        await _record(
            kept,
            classification="update",
            action=action,
            new_fact_id=new_id or None,
            reasoning=f"{reasoning}; kept: higher-authority source",
        )
        return {
            "action": action,
            "new_id": new_id,
            "existing_id": kept["id"],
            "classification": "update",
            "reasoning": reasoning,
        }

    closed: list[int] = []
    for r in changes:
        done = bool(new_id) and await asyncio.to_thread(
            supersede_with_validity,
            r["id"],
            new_id,
            tenant_id=tenant_id,
            classification="update",
            reason="changed",
        )
        if done:
            closed.append(int(r["id"]))
        await _record(
            r,
            classification="update",
            action="superseded" if done else "supersede_skipped",
            new_fact_id=new_id or None,
            reasoning=reasoning,
        )
    return {
        "action": "superseded" if closed else "supersede_skipped",
        "new_id": new_id,
        "old_id": closed[0] if closed else changes[0]["id"],
        "old_ids": closed,
        "classification": "update",
        "reasoning": reasoning,
    }


async def resolve_and_store(
    fact: dict[str, Any],
    source_content: str,
    source_type: str,
    similarity_threshold: float = 0.7,
    *,
    tenant_id: str = "",
    metadata: dict[str, Any] | None = None,
    origin: str | None = None,
    event_time: str | None = None,
    scope_kind: str | None = None,
    scope_id: str | None = None,
    source_channel: str | None = None,
    embedding: list[float] | None = None,
    classify_timeout: float | None = None,
) -> dict[str, Any]:
    """Full conflict resolution pipeline: find similar -> classify -> act.

    Args:
        fact: Fact dictionary with fact_text, category, entities, confidence.
        source_content: Original content the fact was extracted from.
        source_type: Type of source (conversation, email, etc.).
        similarity_threshold: Minimum similarity to trigger classification.
        metadata, origin, event_time, scope_kind, scope_id, source_channel,
        embedding: Written with the new row (see ``facts.store_fact``). The
            scope also bounds the search: only facts of the same scope are
            reinforced, skipped as duplicates or superseded.
        classify_timeout: Seconds the one classification call may take. On
            timeout the fact is stored as new (nothing is superseded, an
            operator-confirmed match stays active) and the result says
            ``conflict_check: "timed_out"``.

    Returns:
        Dict with 'action' (stored/skipped/superseded/reinforced/kept_confirmed)
        and optionally 'new_id'. ``kept_confirmed`` means the best match is
        operator-confirmed: the new fact was stored, inactive and superseded by it.
    """
    store_kw: dict[str, Any] = {
        "tenant_id": tenant_id,
        "origin": origin,
        "event_time": event_time,
        "scope_kind": scope_kind or "shared",
        "scope_id": scope_id,
        "source_channel": source_channel,
        "embedding": embedding,
    }

    async def _store(extra: dict[str, Any] | None = None) -> int:
        meta = {**(metadata or {}), **(extra or {})}
        return await store_fact(fact, source_content, source_type, meta or None, **store_kw)

    similar = await find_similar_facts(
        fact["fact_text"],
        limit=3,
        threshold=similarity_threshold,
        tenant_id=tenant_id,
        scope_kind=scope_kind,
        scope_id=scope_id,
        embedding=embedding,
    )

    # Same-slot facts decide first: a repeat needs the same value, and a
    # different value for a single-valued slot is a change, whatever the
    # similarity says.
    slot = fact_slot(fact)
    if slot is not None:
        decided = await _resolve_slot(
            fact,
            slot,
            similar,
            _store,
            tenant_id=tenant_id or DEFAULT_TENANT,
            scope_kind=scope_kind or "shared",
            scope_id=scope_id,
            origin=origin,
        )
        if decided is not None:
            return decided

    if not similar:
        fact_id = await _store()
        return {"action": "stored", "new_id": fact_id}

    best_match = similar[0]

    # WS-3 reinforce-not-fork: a near-identical, same-category match is the same
    # event re-reported, not a new one. Reinforce it and skip the LLM classify
    # (which is biased to "new") so re-ingested briefings stop minting copies.
    if (
        _write_dedup_enabled()
        and float(best_match.get("similarity") or 0) >= _REINFORCE_THRESHOLD
        and (best_match.get("category") or "") == (fact.get("category") or "")
        # A number change is not a re-report, it IS the update. "$100 -> $150"
        # scores 0.9882 cosine, sails over the threshold, and reverses the
        # fact's meaning. Without this guard the update was discarded and the
        # STALE value had its importance bumped, so the wrong number surfaced
        # more often over time. Falls through to the LLM classifier, which
        # correctly returns "update" and supersedes.
        # The same holds for a different name, or the same words reordered
        # ("tea over coffee" vs "coffee over tea" scores 0.975).
        and not values_differ(fact["fact_text"], best_match.get("fact_text") or "")
    ):
        await asyncio.to_thread(_reinforce_fact, best_match["id"], tenant_id=tenant_id)
        # Recorded too: an error rate needs the cases where nothing was
        # superseded as much as the ones where something was.
        await asyncio.to_thread(
            record_conflict_decision,
            tenant_id=tenant_id or DEFAULT_TENANT,
            classification="reinforced",
            action="reinforced",
            new_fact_id=None,
            existing_fact_id=best_match["id"],
            reasoning="similarity >= reinforce threshold, same category",
            similarity=float(best_match.get("similarity") or 0),
            new_fact_text=fact["fact_text"],
            existing_fact_text=best_match["fact_text"],
        )
        return {
            "action": "reinforced",
            "existing_id": best_match["id"],
            "similarity": round(float(best_match.get("similarity") or 0), 4),
        }

    # The same extraction, already overruled by this confirmed fact on an
    # earlier pass: record the decision against that row and stop, before the
    # classification and the insert.
    if is_operator_confirmed(best_match):
        overruled_id = await asyncio.to_thread(
            _find_overruled,
            fact,
            best_match["id"],
            tenant_id=tenant_id,
            scope_kind=scope_kind,
            scope_id=scope_id,
        )
        if overruled_id:
            await asyncio.to_thread(
                record_conflict_decision,
                tenant_id=tenant_id or DEFAULT_TENANT,
                classification="duplicate",
                action="kept_confirmed",
                new_fact_id=overruled_id,
                existing_fact_id=best_match["id"],
                reasoning="same extraction already overruled by this operator-confirmed fact",
                similarity=float(best_match.get("similarity") or 0) or None,
                new_fact_text=fact["fact_text"],
                existing_fact_text=best_match["fact_text"],
            )
            return {
                "action": "kept_confirmed",
                "new_id": overruled_id,
                "existing_id": best_match["id"],
                "classification": "duplicate",
                "reasoning": "already overruled",
            }

    try:
        classification = await asyncio.wait_for(
            classify_relationship(fact["fact_text"], best_match["fact_text"]),
            timeout=classify_timeout,
        )
    except TimeoutError:
        # Out of time on a request path: keep the fact rather than lose it. No
        # supersession happens without a classification, so whatever it might
        # contradict (an operator-confirmed fact included) stays active.
        logger.warning(
            "resolve_and_store: classification timed out after %.1fs; stored as new",
            classify_timeout or 0.0,
        )
        fact_id = await _store({"conflict_check": "timed_out"})
        return {
            "action": "stored",
            "new_id": fact_id,
            "conflict_check": "timed_out",
            "existing_id": best_match["id"],
        }

    kind = classification["classification"]
    _record_sync = partial(
        record_conflict_decision,
        tenant_id=tenant_id or DEFAULT_TENANT,
        classification=kind,
        existing_fact_id=best_match["id"],
        reasoning=classification["reasoning"],
        similarity=float(best_match.get("similarity") or 0) or None,
        new_fact_text=fact["fact_text"],
        existing_fact_text=best_match["fact_text"],
    )

    async def _record(**kw: Any) -> None:
        await asyncio.to_thread(_record_sync, **kw)

    if kind == "duplicate":
        await _record(action="skipped", new_fact_id=None)
        return {
            "action": "skipped",
            "existing_id": best_match["id"],
            "reasoning": classification["reasoning"],
        }

    if kind in ("contradiction", "update"):
        new_id = await _store()
        # An UPDATE and a CONTRADICTION are both bounded in world-time, but they
        # are now recorded distinctly. They mean different things — for an update
        # the old fact was genuinely true until now; for a contradiction one of
        # the two was always wrong and the bound is a guess about which. Acting
        # on that difference needs an error rate, and until this table existed
        # there was none: the classification deactivated rows and vanished.
        #
        # An operator-confirmed match is never superseded, whatever the new
        # fact's confidence. supersede_with_validity also refuses one confirmed
        # after the similarity search; both cases land in the branch below.
        # Trust order: a lower-authority source does not replace a fact from
        # a higher one. Both stay; the new one is kept overruled by it.
        if not is_operator_confirmed(best_match) and _authority(best_match) > authority_rank(
            origin
        ):
            if new_id:
                await asyncio.to_thread(
                    keep_overruled, new_id, best_match["id"], tenant_id=tenant_id
                )
            await _record(action="kept_authority", new_fact_id=new_id or None)
            return {
                "action": "kept_authority",
                "new_id": new_id,
                "existing_id": best_match["id"],
                "classification": kind,
                "reasoning": classification["reasoning"],
            }
        if not is_operator_confirmed(best_match) and await asyncio.to_thread(
            supersede_with_validity,
            best_match["id"],
            new_id,
            tenant_id=tenant_id,
            classification=kind,
        ):
            await _record(action="superseded", new_fact_id=new_id)
            return {
                "action": "superseded",
                "new_id": new_id,
                "old_id": best_match["id"],
                "classification": kind,
                "reasoning": classification["reasoning"],
            }

        # Refused for a reason other than confirmation (the target is gone):
        # the new fact is the current statement, so it stays active.
        if not is_operator_confirmed(best_match) and not await asyncio.to_thread(
            _confirmed_now, best_match["id"], tenant_id=tenant_id
        ):
            logger.info(
                "resolve_and_store: fact %s not superseded (absent); new fact %s kept active",
                best_match["id"],
                new_id,
            )
            await _record(action="supersede_skipped", new_fact_id=new_id)
            return {
                "action": "supersede_skipped",
                "new_id": new_id,
                "old_id": best_match["id"],
                "classification": kind,
                "reasoning": classification["reasoning"],
            }

        # Kept on record, overruled BY the confirmed fact, so the pipeline's
        # view is not lost and the operator's stays the active answer.
        if new_id:
            await asyncio.to_thread(keep_overruled, new_id, best_match["id"], tenant_id=tenant_id)
        logger.info(
            "resolve_and_store: fact %s is operator-confirmed; new fact %s kept overruled (%s)",
            best_match["id"],
            new_id,
            kind,
        )
        await _record(action="kept_confirmed", new_fact_id=new_id or None)
        return {
            "action": "kept_confirmed",
            "new_id": new_id,
            "existing_id": best_match["id"],
            "classification": kind,
            "reasoning": classification["reasoning"],
        }

    # Classified as new — store directly
    fact_id = await _store()
    return {"action": "stored", "new_id": fact_id}
