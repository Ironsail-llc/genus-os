"""The one write path: every stored memory enters the graph the same way.

Every writer of ``memory_facts`` — ingestion, the ``store_memory`` tool, the
deferred write jobs, ``record_resolution``, breadcrumb promotion and
consolidation — goes through :func:`write_facts` (or :func:`ingest_text`, which
extracts first and then calls it). For each fact it:

1. applies the quality gate (``facts.store_fact``, unchanged);
2. runs conflict handling (``conflicts.resolve_and_store``: dedup, reinforce,
   classification, bitemporal supersession, operator-confirmed protection),
   bounded to facts of the same scope, unless the writer opts out;
3. writes the row with its origin, event time and scope;
4. resolves the fact's mentions through :mod:`robothor.memory.identity` and
   writes them (``memory_mentions``);
5. upserts each relation the fact states between two RESOLVED mentions, with an
   evidence row for this fact (``memory_relation_evidence``) and the edge's
   origin recomputed (see :func:`recompute_edge_origins`).

Origin (:func:`origin_for`) is decided by who said it, never upgraded by
default.

Scope. Every writer stores ``shared`` facts, exactly as before this module:
making facts from a one-to-one conversation private to its person would stop
scheduled agents and other team members recalling them, which is a product
decision for the operator, not a side effect of a write-path change. A PRIVATE
scope is written only when a caller passes one explicitly (``scope=`` on
:meth:`WriteContext.for_source`, ``ingest_content`` and ``store_memory_content``;
nothing passes it yet). The mechanism is complete and tested: a private fact
never deduplicates into, reinforces or supersedes a shared one or another
reader's (scope is in the content hash and bounds the similarity search),
and consolidation never mixes scopes. :func:`conversation_scope` mirrors the
conversation-segments rule for the caller that will choose to use it.

Two deliberate restrictions on PRIVATE (non-shared) facts, both because no
reader yet filters the graph by the scope of an edge's evidence (``get_entity``
and entity expansion show every edge of a node to every reader):

* no provisional node is created from a private fact's text (a shared node
  would show the name to everyone); its mentions resolve to existing nodes
  only, or stay unresolved;
* no edge is written from a private fact. The relations it stated are kept on
  the fact (``metadata.graph_pending_relations``), readable only as the fact is,
  and counted in ``relations_private_deferred``, so a reader that enforces
  evidence visibility can materialise them later without a model call.

Engine internals are async: every database step here runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection, tenant_scope
from robothor.memory import facts as _facts
from robothor.memory.confirmed import is_operator_confirmed
from robothor.memory.conflicts import resolve_and_store
from robothor.memory.graph_schema import (
    AUTHORITY_ORDER,
    FACT_ORIGINS,
    LEGACY_ORIGIN,
    Origin,
    least_trusted,
)

logger = logging.getLogger(__name__)

# ═══ origin ══════════════════════════════════════════════════════════════

#: Channels whose content comes from whoever wrote to us. With no speaker
#: named, their content is external.
INBOUND_CHANNELS: frozenset[str] = frozenset(
    {"email", "discord", "telegram", "gchat", "voice", "camera"}
)

#: Content that is someone else's document, whoever relays it.
EXTERNAL_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "email",
        "web",
        "webpage",
        "web_page",
        "document",
        "upload",
        "uploaded_document",
        "pdf",
        "attachment",
        "external",
    }
)

#: Who said it, when the caller knows.
ROLE_ORIGINS: Mapping[str, str] = {
    "user": Origin.USER_STATED,
    "human": Origin.USER_STATED,
    "assistant": Origin.AGENT_GENERATED,
    "agent": Origin.AGENT_GENERATED,
    "tool": Origin.TOOL,
}


def origin_for(
    source_channel: str | None,
    content_type: str | None = None,
    *,
    role: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """The origin of content from this source. The one mapping every writer uses.

    In order:

    * a fact marked operator-confirmed (``metadata.operator_confirmed``) is
      ``operator_confirmed``;
    * a named speaker decides: a person's turn (``user``) is ``user_stated``,
      the assistant's (``assistant``) ``agent_generated``, a tool result
      (``tool``) ``tool``; an unknown role counts as no role;
    * with no speaker: the ``crm`` channel is ``crm``; inbound channels
      (:data:`INBOUND_CHANNELS`) are ``external``; everything else (cli, api,
      mcp, a conversation transcript, an unknown channel) is
      ``agent_generated``;
    * external content (:data:`EXTERNAL_CONTENT_TYPES`: an email, a web page, a
      document) is ``external`` whoever relays it — the least trusted of the
      two wins, so relaying never raises trust.

    Nothing here ever returns a more trusted origin than the evidence supports.
    """
    if metadata and is_operator_confirmed({"metadata": dict(metadata)}):
        return Origin.OPERATOR_CONFIRMED
    channel = (source_channel or "").strip().lower()
    ctype = (content_type or "").strip().lower()
    r = (role or "").strip().lower()
    if r in ROLE_ORIGINS:
        base = ROLE_ORIGINS[r]
    elif channel == "crm":
        base = Origin.CRM
    elif channel in INBOUND_CHANNELS:
        base = Origin.EXTERNAL
    else:
        base = Origin.AGENT_GENERATED
    if ctype in EXTERNAL_CONTENT_TYPES:
        return least_trusted(base, Origin.EXTERNAL)
    return Origin(base).value


# ═══ scope ═══════════════════════════════════════════════════════════════


#: The operator's main session key when the engine cannot say (the segments
#: module's default, repeated so this module does not import the engine).
DEFAULT_MAIN_SESSION_KEY = "agent:main:primary"


def conversation_scope(
    session_key: str,
    person_id: str | None,
    *,
    owner_person_id: str | None = None,
    main_session_key: str = DEFAULT_MAIN_SESSION_KEY,
) -> tuple[str, str | None]:
    """Who owns a conversation: ``(scope_kind, scope_id)``. NOT applied by default.

    A copy of ``segments.conversation_scope`` (raw-segments branch); the two must
    become one function when the branches meet. Fails closed: ``shared`` only
    for a workspace room (``agent:{agent}:room:{id}``); every other conversation
    is ``user``, owned by its recorded person (``person:{id}``), else the member
    its webchat key names (``user:{uid}``), else, for the main session, the
    operator's person; with no owner, ``("user", None)``.

    No writer stores facts under this scope today (facts stay shared, see the
    module docstring). A caller that opts in passes the result as ``scope=``;
    ``("user", None)`` cannot be stored as a fact scope and is refused.
    """
    from robothor.rooms.keys import parse_room_key

    if parse_room_key(session_key) is not None:
        return "shared", None
    if person_id:
        return "user", f"person:{person_id}"
    parts = (session_key or "").split(":")
    if len(parts) == 4 and parts[0] == "agent" and parts[2] == "user" and parts[3]:
        return "user", f"user:{parts[3]}"
    if session_key == main_session_key and owner_person_id:
        return "user", f"person:{owner_person_id}"
    return "user", None


def person_of(scope_kind: str | None, scope_id: str | None) -> str | None:
    """The CRM person a ``user`` scope belongs to (``person:{id}``, or a bare id)."""
    if scope_kind != "user" or not scope_id:
        return None
    sid = str(scope_id)
    candidate = sid.split(":", 1)[1] if sid.startswith("person:") else sid
    return candidate if _is_uuid(candidate) else None


def _checked_scope(scope: tuple[str, str | None] | None) -> tuple[str, str | None]:
    """An explicit scope, validated. Never widened: a bad one is an error."""
    if scope is None:
        return "shared", None
    kind, sid = scope
    if kind == "shared":
        return "shared", None
    if kind not in ("user", "agent") or not sid:
        raise ValueError(f"write path: {kind!r} scope needs an owner (got {sid!r})")
    return kind, str(sid)


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


# ═══ predicates ══════════════════════════════════════════════════════════

_PREDICATE_SEP = re.compile(r"[\s\-]+")


def normalize_predicate(phrase: str | None) -> str | None:
    """The canonical form of a relation phrase: lowercase, trimmed, words joined by ``_``.

    Deliberately trivial (no synonyms, no stemming): it only folds spellings of
    the same phrase together. ``None`` for an empty phrase.
    """
    if not isinstance(phrase, str):
        return None
    folded = _PREDICATE_SEP.sub("_", phrase.strip().lower()).strip("_")
    return folded or None


# ═══ the write context ═══════════════════════════════════════════════════


@dataclass(frozen=True)
class WriteContext:
    """Everything a write needs besides the facts: tenant, source, origin, scope."""

    tenant_id: str
    source_channel: str
    content_type: str
    origin: str
    scope_kind: str = "shared"
    scope_id: str | None = None
    conversation_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_shared(self) -> bool:
        return self.scope_kind == "shared"

    @property
    def person_id(self) -> str | None:
        """The CRM person a ``user``-scoped write belongs to, if it is one."""
        return person_of(self.scope_kind, self.scope_id)

    @classmethod
    def for_source(
        cls,
        *,
        tenant_id: str,
        source_channel: str,
        content_type: str,
        role: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        session_key: str | None = None,
        scope: tuple[str, str | None] | None = None,
    ) -> WriteContext:
        """The context of content from a source. Shared unless ``scope`` says otherwise."""
        meta = dict(metadata or {})
        conv = session_key or (str(meta["session_key"]) if meta.get("session_key") else None)
        kind, sid = _checked_scope(scope)
        return cls(
            tenant_id=tenant_id or DEFAULT_TENANT,
            source_channel=source_channel or "",
            content_type=content_type or "",
            origin=origin_for(source_channel, content_type, role=role, metadata=meta),
            scope_kind=kind,
            scope_id=sid,
            conversation_ref=conv,
            metadata=meta,
        )

    def to_json(self) -> dict[str, Any]:
        """The context a deferred write job carries (the tenant is the job's own)."""
        return {
            "source_channel": self.source_channel,
            "content_type": self.content_type,
            "origin": self.origin,
            "scope_kind": self.scope_kind,
            "scope_id": self.scope_id,
            "conversation_ref": self.conversation_ref,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_json(
        cls, data: Any, *, tenant_id: str, content_type: str = "conversation"
    ) -> WriteContext:
        """Rebuild a job's context. The tenant always comes from the job row.

        A job with no context (written by an older release) is what it was: a
        shared write by an agent (or external, for external content). A
        malformed private scope raises (the job fails and says so) rather than
        being stored more widely than it was meant to be.
        """
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                data = None
        if not isinstance(data, Mapping):
            return cls(
                tenant_id=tenant_id,
                source_channel="",
                content_type=content_type,
                origin=origin_for(None, content_type),
            )
        origin = str(data.get("origin") or "")
        kind, sid = _checked_scope((str(data.get("scope_kind") or "shared"), data.get("scope_id")))
        return cls(
            tenant_id=tenant_id,
            source_channel=str(data.get("source_channel") or ""),
            content_type=str(data.get("content_type") or content_type),
            origin=origin if origin in FACT_ORIGINS else origin_for(None, content_type),
            scope_kind=kind,
            scope_id=sid,
            conversation_ref=data.get("conversation_ref"),
            metadata=dict(data.get("metadata") or {}),
        )


# ═══ writing ═════════════════════════════════════════════════════════════

#: Actions of ``resolve_and_store`` after which the new fact is the live one.
_LIVE_ACTIONS = frozenset({"stored", "superseded", "supersede_skipped"})


def _empty_result() -> dict[str, Any]:
    return {
        "fact_ids": [],
        "existing_ids": [],
        "facts_processed": 0,
        "facts_skipped": 0,
        "facts_kept_confirmed": 0,
        "facts_refused": 0,
        "conflict_checks_timed_out": 0,
        "mentions_written": 0,
        "relations_stored": 0,
        "relations_unresolved": 0,
        "relations_private_deferred": 0,
        "graph_errors": 0,
        "actions": [],
    }


async def _embed(texts: list[str]) -> list[list[float] | None]:
    """One batched embedding call; on failure each store embeds on its own."""
    from robothor.llm import ollama as llm_client

    try:
        vectors = await llm_client.get_embeddings_batch_async(texts)
    except Exception as exc:  # the store retries per fact; nothing is lost
        logger.warning("write path: batch embedding failed (%s)", type(exc).__name__)
        return [None] * len(texts)
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        return [None] * len(texts)
    return list(vectors)


async def write_facts(
    facts: Sequence[Mapping[str, Any]],
    *,
    source_content: str,
    context: WriteContext,
    conflict_check: bool = True,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Store facts and their graph through the one write path. Never raises per fact.

    ``facts`` are extraction dicts (``fact_text``, ``category``, ``confidence``,
    and optionally ``mentions``, ``relations``, ``event_time``, ``entities``,
    ``metadata``). A fact with no ``mentions`` key (a writer that does not
    extract) has its mentions found among the tenant's known entity names.

    ``conflict_check=False`` stores without conflict handling (a writer that
    does its own: ``record_resolution``, breadcrumb promotion). ``deadline`` (a
    ``time.monotonic()`` value) bounds conflict classification on a request
    path: past it, a fact is stored as new and the result counts it in
    ``conflict_checks_timed_out``.

    Returns counts and ``fact_ids`` (the facts now live from this write),
    ``existing_ids`` (facts it reinforced or duplicated).
    """
    result = _empty_result()
    items = [dict(f) for f in facts if str(f.get("fact_text") or "").strip()]
    if not items:
        return result
    vectors = await _embed([f["fact_text"] for f in items])
    for fact, vector in zip(items, vectors, strict=True):
        meta = {**dict(context.metadata), **dict(fact.get("metadata") or {})}
        store_kw: dict[str, Any] = {
            "tenant_id": context.tenant_id,
            "origin": context.origin,
            "event_time": fact.get("event_time"),
            "scope_kind": context.scope_kind,
            "scope_id": context.scope_id,
            "source_channel": context.source_channel or None,
            "embedding": vector,
        }
        try:
            if conflict_check:
                timeout = None if deadline is None else max(0.5, deadline - time.monotonic())
                try:
                    outcome = await resolve_and_store(
                        fact,
                        source_content,
                        context.content_type,
                        metadata=meta,
                        classify_timeout=timeout,
                        **store_kw,
                    )
                except Exception as exc:
                    logger.warning("conflict resolution failed (%s), storing directly", exc)
                    stored = await _facts.store_fact(
                        fact, source_content, context.content_type, meta or None, **store_kw
                    )
                    outcome = {"action": "stored", "new_id": stored}
            else:
                stored = await _facts.store_fact(
                    fact, source_content, context.content_type, meta or None, **store_kw
                )
                outcome = {"action": "stored", "new_id": stored}
        except Exception:
            logger.warning("write path: storing a fact failed", exc_info=True)
            result["facts_refused"] += 1
            continue

        action = outcome.get("action")
        new_id = outcome.get("new_id")
        result["actions"].append(action)
        if outcome.get("conflict_check") == "timed_out":
            result["conflict_checks_timed_out"] += 1
        if action == "kept_confirmed":
            result["facts_kept_confirmed"] += 1
            continue
        if action in _LIVE_ACTIONS:
            if not new_id:
                result["facts_refused"] += 1
                continue
            result["fact_ids"].append(int(new_id))
            result["facts_processed"] += 1
            try:
                graph = await asyncio.to_thread(
                    _write_graph, context.tenant_id, int(new_id), fact, context
                )
            except Exception:
                logger.warning("write path: graph write failed for fact %s", new_id, exc_info=True)
                result["graph_errors"] += 1
                continue
            for key in (
                "mentions_written",
                "relations_stored",
                "relations_unresolved",
                "relations_private_deferred",
            ):
                result[key] += int(graph.get(key, 0))
            continue
        result["facts_skipped"] += 1
        if outcome.get("existing_id"):
            result["existing_ids"].append(int(outcome["existing_id"]))
    return result


async def ingest_text(
    content: str,
    context: WriteContext,
    *,
    extract_timeout: float = 180.0,
    read_only_context: str | None = None,
    fallback_on_empty: bool = False,
    conflict_check: bool = True,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Extract facts from ``content`` (one model call) and write them.

    When extraction FAILS (timeout, unparseable answer, model error) the raw
    content is stored as one fact tagged ``metadata.extraction =
    "fallback_raw"`` and the result says ``extraction_failed`` with the reason.
    When the model answered and found nothing, nothing is stored — unless
    ``fallback_on_empty`` (``store_memory``: the caller asked for it to be kept).
    """
    extraction = await _facts.extract_facts_detailed(
        content, timeout=extract_timeout, context=read_only_context
    )
    facts: list[dict[str, Any]] = list(extraction.facts)
    if extraction.error is not None or (not facts and fallback_on_empty):
        tag: dict[str, Any] = {"extraction": "fallback_raw"}
        if extraction.error is not None:
            tag["extraction_error"] = extraction.error
        facts = [
            {
                "fact_text": content,
                "category": "personal",
                "entities": [],
                "confidence": 0.5,
                "metadata": tag,
            }
        ]
    if extraction.error is not None:
        logger.warning(
            "extraction failed (%s); storing the raw content (%d chars)",
            extraction.error,
            len(content),
        )
    result = await write_facts(
        facts,
        source_content=content,
        context=context,
        conflict_check=conflict_check,
        deadline=deadline,
    )
    result["facts_extracted"] = len(extraction.facts)
    result["extraction_failed"] = extraction.error is not None
    result["extraction_error"] = extraction.error
    return result


# ═══ the graph (synchronous: always called in a worker thread) ═══════════

#: SQL ranking an origin by authority (higher is more trusted); NULL and
#: unknown rank as the legacy origin, as ``graph_schema.authority_rank`` does.
_RANK_SQL = (
    "CASE COALESCE({col}, '"
    + LEGACY_ORIGIN
    + "') "
    + " ".join(f"WHEN '{o}' THEN {len(AUTHORITY_ORDER) - i}" for i, o in enumerate(AUTHORITY_ORDER))
    + f" ELSE {len(AUTHORITY_ORDER) - AUTHORITY_ORDER.index(LEGACY_ORIGIN)} END"
)


def recompute_edge_origins(cur: Any, tenant_id: str, relation_ids: Sequence[int]) -> None:
    """Set each edge's origin to the MOST trusted origin among its supporting facts.

    An edge is as trustworthy as its best support for automatic recall: a
    relation a person stated stays user-stated when an email repeats it. Active
    facts count first; an edge whose every supporting fact was retired keeps the
    best of those. Each evidence row keeps its own fact, so a strict reader can
    check every supporting fact's origin itself.
    """
    ids = sorted({int(i) for i in relation_ids})
    if not ids:
        return
    rank = _RANK_SQL.format(col="f.origin")
    cur.execute(
        f"""
        UPDATE memory_relations r
           SET origin = best.origin
          FROM (
            SELECT DISTINCT ON (e.relation_id) e.relation_id,
                   COALESCE(f.origin, %s) AS origin
              FROM memory_relation_evidence e
              JOIN memory_facts f ON f.id = e.fact_id AND f.tenant_id = e.tenant_id
             WHERE e.tenant_id = %s AND e.relation_id = ANY(%s)
             ORDER BY e.relation_id, f.is_active DESC, {rank} DESC
          ) best
         WHERE r.id = best.relation_id AND r.tenant_id = %s
        """,  # noqa: S608 -- rank is built from the origin constants
        (LEGACY_ORIGIN, tenant_id, ids, tenant_id),
    )


_WORD = re.compile(r"\w", re.UNICODE)


def _known_mentions(cur: Any, tenant_id: str, text: str) -> list[dict[str, Any]]:
    """Names of shared entities the text contains, as mentions (no model call).

    For writers that do not extract. Whole-word, case-insensitive. A
    single-token person name is skipped: the identity rules would never resolve
    it alone, and a first name is not evidence of anyone.
    """
    if not text or not text.strip():
        return []
    cur.execute(
        "SELECT name, entity_type FROM memory_entities WHERE tenant_id = %s "
        "AND scope_kind = 'shared' AND length(name) >= 3 "
        "AND strpos(lower(%s), lower(name)) > 0 ORDER BY length(name) DESC, id LIMIT 50",
        (tenant_id, text),
    )
    low = text.lower()
    out: list[dict[str, Any]] = []
    for name, etype in cur.fetchall():
        if etype == "person" and len(str(name).split()) < 2:
            continue
        i = low.find(str(name).lower())
        end = i + len(name)
        before = low[i - 1] if i > 0 else " "
        after = low[end] if end < len(low) else " "
        if _WORD.match(before) or _WORD.match(after):
            continue
        if any(str(name).lower() in m["surface_form"].lower() for m in out):
            continue  # "Project Helios" already covers "Helios"
        out.append({"surface_form": str(name), "entity_type": etype})
    return out


def _mention_inputs(cur: Any, fact: Mapping[str, Any], context: WriteContext) -> list[Any]:
    from robothor.memory.identity import MentionInput, normalize_surface

    mentions = fact.get("mentions")
    if mentions is None:
        mentions = _known_mentions(cur, context.tenant_id, str(fact.get("fact_text") or ""))
    pairs: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for m in mentions:
        surface = str(m.get("surface_form") or "").strip()
        if surface and normalize_surface(surface) not in seen:
            seen.add(normalize_surface(surface))
            pairs.append((surface, m.get("entity_type")))
    # A relation endpoint the model did not list is still a name in the fact.
    for rel in fact.get("relations") or []:
        for end in (rel.get("source"), rel.get("target")):
            surface = str(end or "").strip()
            if surface and normalize_surface(surface) not in seen:
                seen.add(normalize_surface(surface))
                pairs.append((surface, None))
    base: dict[str, Any] = {"scope_kind": context.scope_kind, "scope_id": context.scope_id}
    if context.person_id:
        base["person_id"] = context.person_id
    if context.conversation_ref:
        base["conversation_ref"] = context.conversation_ref
    names = [p[0] for p in pairs]
    return [
        MentionInput(s, t, context={**base, "co_mentions": [n for n in names if n != s]})
        for s, t in pairs
    ]


def _write_graph(
    tenant_id: str, fact_id: int, fact: Mapping[str, Any], context: WriteContext
) -> dict[str, Any]:
    """Mentions and relations of one stored fact, in one transaction."""
    from robothor.memory.identity import normalize_surface, resolve_mentions, write_mentions

    out = {
        "mentions_written": 0,
        "relations_stored": 0,
        "relations_unresolved": 0,
        "relations_private_deferred": 0,
    }
    with tenant_scope(tenant_id), get_connection() as conn:
        cur = conn.cursor()
        inputs = _mention_inputs(cur, fact, context)
        resolved = resolve_mentions(tenant_id, inputs, cur=cur, create=context.is_shared)
        out["mentions_written"] = write_mentions(tenant_id, fact_id, inputs, resolved, cur=cur)
        ids = {
            normalize_surface(m.surface_form): r.entity_id
            for m, r in zip(inputs, resolved, strict=True)
        }
        relations = [r for r in fact.get("relations") or [] if isinstance(r, Mapping)]
        if relations and not context.is_shared:
            out["relations_private_deferred"] = len(relations)
            cur.execute(
                "UPDATE memory_facts SET metadata = COALESCE(metadata, '{}'::jsonb) || "
                "jsonb_build_object('graph_pending_relations', %s::jsonb) "
                "WHERE id = %s AND tenant_id = %s",
                (json.dumps([dict(r) for r in relations]), fact_id, tenant_id),
            )
            return out
        touched: list[int] = []
        confidence = float(fact.get("confidence") or 1.0)
        for rel in relations:
            source = ids.get(normalize_surface(str(rel.get("source") or "")))
            target = ids.get(normalize_surface(str(rel.get("target") or "")))
            phrase = str(rel.get("relation") or "").strip()[:100]
            if source is None or target is None or source == target or not phrase:
                out["relations_unresolved"] += 1
                continue
            cur.execute(
                "INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, "
                "fact_id, confidence, tenant_id, predicate, origin) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, source_entity_id, target_entity_id, relation_type) "
                "DO UPDATE SET confidence = GREATEST(memory_relations.confidence, "
                "EXCLUDED.confidence), predicate = COALESCE(memory_relations.predicate, "
                "EXCLUDED.predicate) RETURNING id",
                (
                    source,
                    target,
                    phrase,
                    fact_id,
                    confidence,
                    tenant_id,
                    normalize_predicate(phrase),
                    context.origin,
                ),
            )
            rel_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO memory_relation_evidence (tenant_id, relation_id, fact_id) "
                "VALUES (%s, %s, %s) ON CONFLICT (relation_id, fact_id) DO NOTHING",
                (tenant_id, rel_id, fact_id),
            )
            touched.append(rel_id)
            out["relations_stored"] += 1
        recompute_edge_origins(cur, tenant_id, touched)
    return out


def _copy_graph(tenant_id: str, new_id: int, source_ids: Sequence[int]) -> list[int]:
    """A derived fact inherits its sources' mentions and becomes evidence of their edges."""
    with tenant_scope(tenant_id), get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO memory_mentions (tenant_id, fact_id, entity_id, surface_form, "
            "entity_type, decision_id) SELECT tenant_id, %s, entity_id, surface_form, "
            "entity_type, decision_id FROM (SELECT DISTINCT ON (lower(surface_form)) * "
            "FROM memory_mentions WHERE tenant_id = %s AND fact_id = ANY(%s) "
            "ORDER BY lower(surface_form), id) m "
            "ON CONFLICT (fact_id, lower(surface_form)) DO NOTHING",
            (new_id, tenant_id, list(source_ids)),
        )
        cur.execute(
            "INSERT INTO memory_relation_evidence (tenant_id, relation_id, fact_id) "
            "SELECT DISTINCT tenant_id, relation_id, %s FROM memory_relation_evidence "
            "WHERE tenant_id = %s AND fact_id = ANY(%s) "
            "ON CONFLICT (relation_id, fact_id) DO NOTHING",
            (new_id, tenant_id, list(source_ids)),
        )
        cur.execute(
            "SELECT relation_id FROM memory_relation_evidence WHERE tenant_id = %s "
            "AND fact_id = %s",
            (tenant_id, new_id),
        )
        return [int(r[0]) for r in cur.fetchall()]


def _recompute(tenant_id: str, relation_ids: Sequence[int]) -> None:
    with tenant_scope(tenant_id), get_connection() as conn:
        recompute_edge_origins(conn.cursor(), tenant_id, relation_ids)


async def write_derived_fact(
    fact: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]],
    *,
    source_content: str,
    source_type: str,
) -> tuple[int, list[int]]:
    """Store a fact derived from ``sources`` (consolidation). Returns ``(id, edge ids)``.

    Its tenant and scope are the sources' own and its origin the least trusted
    of theirs. Sources from more than one tenant or scope are refused (``0``):
    a derived row is never built across readers. Mentions are copied from the
    sources and the new fact becomes evidence of every edge they supported (no
    model call); edge origins are NOT recomputed here — the caller does it once
    the sources are retired (:func:`recompute_derived_edges`).
    """
    keys = {
        (
            s.get("tenant_id") or DEFAULT_TENANT,
            s.get("scope_kind") or "shared",
            None if (s.get("scope_kind") or "shared") == "shared" else s.get("scope_id"),
        )
        for s in sources
    }
    if len(keys) != 1:
        logger.warning("write path: refused a derived fact over %d scopes", len(keys))
        return 0, []
    tenant, kind, sid = next(iter(keys))
    origin = least_trusted(*[s.get("origin") for s in sources])
    new_id = await _facts.store_fact(
        dict(fact),
        source_content,
        source_type,
        tenant_id=tenant,
        origin=origin,
        scope_kind=kind,
        scope_id=sid,
    )
    if not new_id:
        return 0, []
    edges = await asyncio.to_thread(
        _copy_graph, tenant, int(new_id), [int(s["id"]) for s in sources]
    )
    return int(new_id), edges


async def recompute_derived_edges(tenant_id: str, relation_ids: Sequence[int]) -> None:
    """Recompute edge origins after a derivation retired its sources."""
    if relation_ids:
        await asyncio.to_thread(_recompute, tenant_id, list(relation_ids))


__all__ = [
    "EXTERNAL_CONTENT_TYPES",
    "INBOUND_CHANNELS",
    "WriteContext",
    "ingest_text",
    "normalize_predicate",
    "origin_for",
    "recompute_edge_origins",
    "conversation_scope",
    "person_of",
    "write_derived_fact",
    "write_facts",
]
