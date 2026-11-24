"""Conversation segments — raw turns, split by topic, recalled verbatim.

The fact store keeps what an LLM chose to extract from a conversation, and
extraction loses detail. This module keeps the conversation itself as a recall
unit: each chat session is split into contiguous, topically coherent
*segments* (``memory_segments``, migration 177), each embedded and keyword
indexed, so a detail nobody extracted is still found.

Three parts:

- :func:`segment_turns` — the pure splitting rule. No LLM call. A segment
  breaks on a time gap, on a drop in embedding similarity between the turns
  before and after a user turn, and on a size budget; a fragment too small to
  stand alone (a lone "thanks") joins its neighbour.
- :func:`index_conversation` — load the turns a session's segments do not yet
  cover, re-segment from the last (open) segment, embed and upsert.
  Idempotent and incremental. Scheduled in the background by
  ``chat_store.save_exchange_async`` once a turn is committed.
- :func:`search_segments` — dense + keyword search fused by reciprocal rank
  (``robothor.memory.fusion``), always scoped to one tenant.

Text is redacted (``robothor.secrets.redaction.redact``) before it is
segmented, so neither the stored row nor the embedding request carries a
credential, including from rows written before chat_store redacted at the door.

Retention: a conversation segment hangs off its ``chat_sessions`` row
(``ON DELETE CASCADE``), and the nightly chat TTL (lifecycle step 10) prunes
segments that started more than :data:`SEGMENT_RETENTION_DAYS` ago — the same
window as the chat turns they copy. Turns older than that are never indexed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection
from robothor.memory.fusion import rrf_fuse
from robothor.memory.vector_tuning import apply_hnsw_session, max_scan_tuples

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence
    from datetime import datetime

    from robothor.identity.scope import DataScope

logger = logging.getLogger(__name__)

# ── Thresholds ──────────────────────────────────────────────────────────────
# Starting values, to be tuned by the memory eval (raw-detail recall cases).
# Change them there, with the eval's numbers, not by feel.

#: A silence longer than this between two turns starts a new segment.
SEGMENT_GAP_SECONDS = 30 * 60
#: Cosine similarity between the turns before and after a user turn below
#: which the user turn starts a new segment (a change of subject).
SEGMENT_SIMILARITY_BREAK = 0.5
#: Turns on each side of a candidate break whose mean vector is compared.
SEGMENT_WINDOW_TURNS = 2
#: Size budget of one segment's text, so a hit fits a reranker and a context
#: budget. A single turn longer than this is clipped to it.
SEGMENT_MAX_CHARS = 4000
#: Most turns in one segment.
SEGMENT_MAX_TURNS = 24
#: A fragment with fewer turns than this AND fewer characters than
#: SEGMENT_MIN_CHARS is not a segment of its own: it joins a neighbour.
SEGMENT_MIN_TURNS = 2
SEGMENT_MIN_CHARS = 200
#: Length of the topic label (the opening of the segment's first user turn).
SEGMENT_TOPIC_MAX_CHARS = 80
#: Segments live no longer than the chat turns they copy (chat_store's
#: CHAT_TURN_TTL_DAYS, pruned by the same nightly step).
SEGMENT_RETENTION_DAYS = 90
#: Candidates fetched per search leg before fusion (at least 3x the limit).
SEARCH_CANDIDATES = 30

_ROLE_LABELS = {"user": "User", "assistant": "Assistant"}
_CLIP_MARK = " […]"


@dataclass(frozen=True)
class Turn:
    """One conversation turn. ``ref`` is its ``chat_messages.id``."""

    ref: int
    role: str
    text: str
    at: datetime | None = None
    embedding: Sequence[float] | None = None


@dataclass(frozen=True)
class Segment:
    """A contiguous run of turns on one subject."""

    turns: tuple[Turn, ...]

    @property
    def start_ref(self) -> int:
        return self.turns[0].ref

    @property
    def end_ref(self) -> int:
        return self.turns[-1].ref

    @property
    def started_at(self) -> datetime | None:
        return self.turns[0].at

    @property
    def ended_at(self) -> datetime | None:
        return self.turns[-1].at

    @property
    def text(self) -> str:
        return "\n".join(_line(t) for t in self.turns)

    @property
    def topic(self) -> str | None:
        first = next((t for t in self.turns if t.role == "user"), self.turns[0])
        return _label(first.text)


def _line(turn: Turn) -> str:
    text = turn.text
    if len(text) > SEGMENT_MAX_CHARS - 20:
        text = text[: SEGMENT_MAX_CHARS - 20 - len(_CLIP_MARK)] + _CLIP_MARK
    return f"{_ROLE_LABELS.get(turn.role, turn.role.title())}: {text}"


def _label(text: str) -> str | None:
    words = " ".join(text.split())
    if not words:
        return None
    if len(words) <= SEGMENT_TOPIC_MAX_CHARS:
        return words
    cut = words[: SEGMENT_TOPIC_MAX_CHARS - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,.;:") + "…"


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _mean(vectors: list[Sequence[float]]) -> list[float]:
    n = len(vectors)
    return [sum(col) / n for col in zip(*vectors, strict=True)]


def _window_similarity(left: Sequence[Turn], right: Sequence[Turn]) -> float | None:
    """Cosine of the mean vectors of two windows; None if any vector is missing."""
    if not left or not right:
        return None
    if any(t.embedding is None for t in (*left, *right)):
        return None
    return _cosine(
        _mean([t.embedding for t in left if t.embedding is not None]),
        _mean([t.embedding for t in right if t.embedding is not None]),
    )


def _is_fragment(turns: Sequence[Turn]) -> bool:
    chars = sum(len(_line(t)) + 1 for t in turns)
    return len(turns) < SEGMENT_MIN_TURNS and chars < SEGMENT_MIN_CHARS


def _chars(turns: Sequence[Turn]) -> int:
    return len("\n".join(_line(t) for t in turns))


def segment_turns(
    turns: Sequence[Turn],
    *,
    embed: Callable[[list[str]], list[list[float] | None]] | None = None,
) -> list[Segment]:
    """Split a conversation into topically coherent, contiguous segments.

    Deterministic and LLM-free. Only ``user`` and ``assistant`` turns with text
    are kept, in the order given. Between turn *i-1* and turn *i* a new
    segment starts when:

    1. the gap between them exceeds :data:`SEGMENT_GAP_SECONDS`; or
    2. adding turn *i* would exceed :data:`SEGMENT_MAX_CHARS` or
       :data:`SEGMENT_MAX_TURNS`; or
    3. turn *i* is a user turn, the open segment already has
       :data:`SEGMENT_MIN_TURNS` turns, and the mean vector of the last
       :data:`SEGMENT_WINDOW_TURNS` turns before it is less similar than
       :data:`SEGMENT_SIMILARITY_BREAK` to the mean of the window starting at it.

    A fragment (see :data:`SEGMENT_MIN_TURNS` / :data:`SEGMENT_MIN_CHARS`)
    then joins the previous segment, or the next one if it is first, when the
    result fits the size budget. A conversation that is only a fragment yields
    no segment.

    ``embed`` is called once, with the texts of turns that carry no vector.
    Without it, such turns never cause a similarity break.
    """
    kept = [t for t in turns if t.role in _ROLE_LABELS and t.text and t.text.strip()]
    if not kept:
        return []
    if embed is not None:
        missing = [i for i, t in enumerate(kept) if t.embedding is None]
        if missing:
            vectors = embed([kept[i].text for i in missing])
            for i, vec in zip(missing, vectors, strict=True):
                if vec is not None:
                    kept[i] = Turn(kept[i].ref, kept[i].role, kept[i].text, kept[i].at, vec)

    groups: list[list[Turn]] = [[kept[0]]]
    for i in range(1, len(kept)):
        turn, prev, current = kept[i], kept[i - 1], groups[-1]
        if _breaks(kept, i, turn, prev, current):
            groups.append([turn])
        else:
            current.append(turn)

    return [Segment(tuple(g)) for g in _absorb_fragments(groups)]


def _breaks(kept: list[Turn], i: int, turn: Turn, prev: Turn, current: list[Turn]) -> bool:
    if (
        turn.at is not None
        and prev.at is not None
        and (turn.at - prev.at).total_seconds() > SEGMENT_GAP_SECONDS
    ):
        return True
    if len(current) >= SEGMENT_MAX_TURNS or _chars([*current, turn]) > SEGMENT_MAX_CHARS:
        return True
    if turn.role != "user" or len(current) < SEGMENT_MIN_TURNS:
        return False
    left = current[-SEGMENT_WINDOW_TURNS:]
    right = kept[i : i + SEGMENT_WINDOW_TURNS]
    similarity = _window_similarity(left, right)
    return similarity is not None and similarity < SEGMENT_SIMILARITY_BREAK


def _absorb_fragments(groups: list[list[Turn]]) -> list[list[Turn]]:
    out: list[list[Turn]] = []
    pending: list[Turn] = []  # a leading fragment waiting for the next group
    for group in groups:
        if pending:
            if _chars([*pending, *group]) <= SEGMENT_MAX_CHARS:
                group = [*pending, *group]
            else:
                out.append(pending)
            pending = []
        if _is_fragment(group):
            if out and _chars([*out[-1], *group]) <= SEGMENT_MAX_CHARS:
                out[-1] = [*out[-1], *group]
                continue
            if not out:
                pending = group
                continue
        out.append(group)
    if pending and out:
        out.append(pending)
    return out


# ── Indexing ────────────────────────────────────────────────────────────────


@dataclass
class _Conversation:
    """What :func:`index_conversation` needs from the database for one session.

    ``resume_from`` is the start of the session's last stored segment (the one
    new turns may extend), ``covered_through`` the highest message id any of
    its segments covers. ``turns`` are the session's redacted turns from
    ``resume_from`` on (all of them when there is no segment yet), within
    retention, oldest first.
    """

    session_id: int
    person_id: str | None
    resume_from: int | None
    covered_through: int | None
    turns: list[Turn] = field(default_factory=list)


def _parse_vector(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = json.loads(value)
        return [float(x) for x in parsed]
    return [float(x) for x in value]


def _load_conversation(
    session_key: str, tenant_id: str, retention_days: int
) -> _Conversation | None:
    """Read one session's uncovered turns (sync; run in a worker thread)."""
    from robothor.secrets.redaction import redact

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id, person_id FROM chat_sessions WHERE tenant_id = %s AND session_key = %s",
            (tenant_id, session_key),
        )
        session = cur.fetchone()
        if session is None:
            return None
        cur.execute(
            """
            SELECT MAX(start_ref) AS resume_from, MAX(end_ref) AS covered_through
            FROM memory_segments
            WHERE tenant_id = %s AND source_kind = 'conversation' AND source_ref = %s
            """,
            (tenant_id, session_key),
        )
        mark = cur.fetchone() or {}
        resume_from = mark.get("resume_from")
        cur.execute(
            """
            SELECT id, message, created_at, embedding::text AS embedding
            FROM chat_messages
            WHERE session_id = %s
              AND id >= %s
              AND created_at >= NOW() - make_interval(days => %s)
            ORDER BY id
            """,
            (session["id"], resume_from or 0, retention_days),
        )
        rows = cur.fetchall()

    turns: list[Turn] = []
    for row in rows:
        message = row["message"] if isinstance(row["message"], dict) else json.loads(row["message"])
        role = message.get("role")
        content = message.get("content")
        if role not in _ROLE_LABELS or not isinstance(content, str) or not content.strip():
            continue
        turns.append(
            Turn(
                ref=int(row["id"]),
                role=role,
                text=redact(content),
                at=row["created_at"],
                embedding=_parse_vector(row["embedding"]),
            )
        )
    return _Conversation(
        session_id=int(session["id"]),
        person_id=str(session["person_id"]) if session["person_id"] else None,
        resume_from=int(resume_from) if resume_from is not None else None,
        covered_through=(
            int(mark["covered_through"]) if mark.get("covered_through") is not None else None
        ),
        turns=turns,
    )


def _write_segments(
    tenant_id: str,
    session_key: str,
    conversation: _Conversation,
    segs: list[Segment],
    vectors: list[list[float] | None],
) -> int:
    """Upsert the re-segmented tail of one conversation (sync; worker thread).

    One transaction under a per-conversation advisory lock. If a concurrent
    run already covered more of the conversation, this one writes nothing.
    Rows from ``resume_from`` on that the new split no longer produces are
    removed, so the stored segments always partition the covered turns.
    Returns the rows inserted or changed.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"memory_segments:{tenant_id}:conversation:{session_key}",),
        )
        cur.execute(
            "SELECT MAX(end_ref) FROM memory_segments "
            "WHERE tenant_id = %s AND source_kind = 'conversation' AND source_ref = %s",
            (tenant_id, session_key),
        )
        current = cur.fetchone()[0]
        if current is not None and current > segs[-1].end_ref:
            conn.rollback()
            return 0
        if conversation.resume_from is not None:
            cur.execute(
                "DELETE FROM memory_segments "
                "WHERE tenant_id = %s AND source_kind = 'conversation' AND source_ref = %s "
                "AND start_ref >= %s AND start_ref <> ALL(%s::bigint[])",
                (tenant_id, session_key, conversation.resume_from, [s.start_ref for s in segs]),
            )
        written = 0
        for seg, vec in zip(segs, vectors, strict=True):
            cur.execute(
                """
                INSERT INTO memory_segments (
                    tenant_id, source_kind, source_ref, start_ref, end_ref,
                    started_at, ended_at, topic, text, embedding,
                    person_id, chat_session_id
                ) VALUES (%s, 'conversation', %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s)
                ON CONFLICT (tenant_id, source_kind, source_ref, start_ref) DO UPDATE SET
                    end_ref = EXCLUDED.end_ref,
                    started_at = EXCLUDED.started_at,
                    ended_at = EXCLUDED.ended_at,
                    topic = EXCLUDED.topic,
                    text = EXCLUDED.text,
                    embedding = EXCLUDED.embedding,
                    person_id = EXCLUDED.person_id,
                    chat_session_id = EXCLUDED.chat_session_id,
                    updated_at = NOW()
                WHERE memory_segments.end_ref IS DISTINCT FROM EXCLUDED.end_ref
                   OR memory_segments.text IS DISTINCT FROM EXCLUDED.text
                """,
                (
                    tenant_id,
                    session_key,
                    seg.start_ref,
                    seg.end_ref,
                    seg.started_at,
                    seg.ended_at,
                    seg.topic,
                    seg.text,
                    vec,
                    conversation.person_id,
                    conversation.session_id,
                ),
            )
            written += int(cur.rowcount or 0)
        conn.commit()
        return written


async def _default_embed_batch(texts: list[str]) -> list[list[float] | None]:
    from robothor.llm import ollama as llm_client

    return list(await llm_client.get_embeddings_batch_async(texts))


async def index_conversation(
    session_key: str,
    *,
    tenant_id: str,
    embed_batch: Callable[[list[str]], Awaitable[list[list[float] | None]]] | None = None,
) -> int:
    """Segment one chat session's new turns and store them. Returns rows written.

    Loads the turns from the start of the session's last stored segment on
    (every turn within retention when there is none), re-segments them so the
    open segment can grow, embeds what has no vector yet, and upserts on
    ``(tenant_id, 'conversation', session_key, start_ref)``. Running it again
    with no new turn writes nothing. Database work runs in a worker thread.

    ``embed_batch`` is injected in tests; the default is the local Ollama
    embedder. An embedding failure raises and writes nothing, so the next run
    retries the same turns. Callers on the request path must not await this
    directly; ``chat_store.save_exchange_async`` schedules it in the background
    and logs any failure.
    """
    if not tenant_id:
        raise ValueError("index_conversation needs a tenant_id")
    conversation = await asyncio.to_thread(
        _load_conversation, session_key, tenant_id, SEGMENT_RETENTION_DAYS
    )
    if conversation is None or not conversation.turns:
        return 0
    covered = conversation.covered_through
    if covered is not None and conversation.turns[-1].ref <= covered:
        return 0

    embed = embed_batch or _default_embed_batch
    turns = list(conversation.turns)
    missing = [i for i, t in enumerate(turns) if t.embedding is None]
    if missing:
        vectors = await embed([turns[i].text for i in missing])
        for i, vec in zip(missing, vectors, strict=True):
            turns[i] = Turn(turns[i].ref, turns[i].role, turns[i].text, turns[i].at, vec)

    segs = segment_turns(turns)
    if not segs:
        return 0
    seg_vectors = await embed([s.text for s in segs])
    return await asyncio.to_thread(
        _write_segments, tenant_id, session_key, conversation, segs, seg_vectors
    )


def cleanup_stale_segments(days: int = SEGMENT_RETENTION_DAYS, tenant_id: str | None = None) -> int:
    """Delete segments that started more than ``days`` ago. Returns rows deleted.

    Nightly maintenance passes ``tenant_id`` so one tenant's sweep never
    touches another's rows; ``None`` sweeps every tenant (manual GC). Measured
    from the segment's FIRST turn: a segment is gone no later than the oldest
    chat turn it copies.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        if tenant_id is None:
            cur.execute(
                "DELETE FROM memory_segments "
                "WHERE COALESCE(started_at, created_at) < NOW() - make_interval(days => %s)",
                (days,),
            )
        else:
            cur.execute(
                "DELETE FROM memory_segments WHERE tenant_id = %s "
                "AND COALESCE(started_at, created_at) < NOW() - make_interval(days => %s)",
                (tenant_id, days),
            )
        count = int(cur.rowcount or 0)
        conn.commit()
        return count


# ── Search ──────────────────────────────────────────────────────────────────

_COLUMNS = (
    "id, text, topic, source_kind, source_ref, start_ref, end_ref, started_at, ended_at, person_id"
)

# The keyword leg ORs the query's terms: plainto_tsquery ANDs them, so a long
# natural question ("could you remind me what the locker number was") matched
# nothing unless every word appeared. Stop words are dropped and terms stemmed
# by plainto_tsquery first; an all-stop-word query becomes NULL and matches
# nothing.
_OR_TSQUERY = "NULLIF(replace(plainto_tsquery('english', %s)::text, ' & ', ' | '), '')::tsquery"


def _filters(
    *,
    person_scope: DataScope | None,
    since: datetime | None,
    until: datetime | None,
    room_ids: Iterable[str] | None,
) -> tuple[str, list[Any]]:
    from robothor.rooms.keys import canonical_uuid

    clauses: list[str] = []
    params: list[Any] = []
    if person_scope is not None and person_scope.restricted:
        clauses.append("(person_id::text = %s OR person_id IS NULL)")
        params.append(person_scope.person_id)
    if since is not None:
        clauses.append("ended_at >= %s")
        params.append(since)
    if until is not None:
        clauses.append("started_at <= %s")
        params.append(until)
    # A workspace's conversation (``agent:{agent}:room:{id}``) is searched only
    # for the rooms the caller can access, as search_chat_turns does. Fails
    # closed: with no rooms passed, no workspace is searched.
    allowed = sorted({c for c in (canonical_uuid(r) for r in room_ids or ()) if c})
    clauses.append(
        "(source_kind <> 'conversation' OR split_part(source_ref, ':', 3) <> 'room' "
        "OR split_part(source_ref, ':', 4) = ANY(%s::text[]))"
    )
    params.append(allowed)
    return "".join(f" AND {c}" for c in clauses), params


def _force_iterative_scan(cur: Any) -> None:
    """Ask pgvector to scan past the tenant filter until LIMIT is met.

    Segments are always tenant-filtered, and a small tenant beside a large one
    otherwise gets fewer rows than asked for: the HNSW candidate budget fills
    with the large tenant's vectors and the filter discards them. Unlike the
    shared opt-in (``MEMORY_HNSW_ITERATIVE``), this is always on for segments.
    In a savepoint, so a build without the setting leaves the query running.
    """
    cur.execute("SAVEPOINT segments_hnsw")
    try:
        cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        cur.execute("SET LOCAL hnsw.max_scan_tuples = %s", (max_scan_tuples(),))
        cur.execute("RELEASE SAVEPOINT segments_hnsw")
    except psycopg2.Error as e:
        cur.execute("ROLLBACK TO SAVEPOINT segments_hnsw")
        logger.debug("segments: iterative HNSW scan unavailable: %s", e)


def _query_segments(
    query: str,
    embedding: Sequence[float] | None,
    *,
    tenant_id: str,
    fetch: int,
    where: str,
    params: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run both legs (sync; worker thread). Returns (dense rows, keyword rows)."""
    dense: list[dict[str, Any]] = []
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        apply_hnsw_session(cur)
        _force_iterative_scan(cur)
        if embedding is not None:
            vec = list(embedding)
            cur.execute(
                f"""
                SELECT {_COLUMNS}, 1 - (embedding <=> %s::vector) AS similarity
                FROM memory_segments
                WHERE tenant_id = %s AND embedding IS NOT NULL {where}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,  # noqa: S608 - fixed fragments; values are parameters
                (vec, tenant_id, *params, vec, fetch),
            )
            # relaxed_order may return near-ties slightly out of order.
            dense = sorted(
                (dict(r) for r in cur.fetchall()),
                key=lambda r: (-float(r["similarity"]), r["id"]),
            )
        cur.execute(
            f"""
            SELECT {_COLUMNS}, ts_rank_cd(tsv, q.q, 1 | 32) AS keyword_rank
            FROM memory_segments, (SELECT {_OR_TSQUERY} AS q) q
            WHERE tenant_id = %s AND q.q IS NOT NULL AND tsv @@ q.q {where}
            ORDER BY keyword_rank DESC, id
            LIMIT %s
            """,  # noqa: S608 - fixed fragments; values are parameters
            (query, tenant_id, *params, fetch),
        )
        keyword = [dict(r) for r in cur.fetchall()]
        conn.commit()
    return dense, keyword


def _hit(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "kind": "segment",
        "source": "segment",
        "text": row["text"],
        "topic": row["topic"],
        "source_kind": row["source_kind"],
        "source_ref": row["source_ref"],
        "start_ref": int(row["start_ref"]),
        "end_ref": int(row["end_ref"]),
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "similarity": None,
        "keyword_rank": None,
        "rrf_score": 0.0,
    }


def _fuse(
    dense: list[dict[str, Any]], keyword: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    hits: dict[int, dict[str, Any]] = {}
    for row in dense:
        hit = hits.setdefault(int(row["id"]), _hit(row))
        hit["similarity"] = round(float(row["similarity"]), 6)
    for row in keyword:
        hit = hits.setdefault(int(row["id"]), _hit(row))
        hit["keyword_rank"] = round(float(row["keyword_rank"]), 6)
    ranked = [
        [hits[int(r["id"])] for r in dense],
        [hits[int(r["id"])] for r in keyword],
    ]
    return rrf_fuse(ranked, key=lambda h: h["id"])[:limit]


async def _embed_query(query: str) -> list[float]:
    from robothor.llm import ollama as llm_client

    return list(await llm_client.get_embedding_async(query))


async def search_segments(
    query: str,
    *,
    tenant_id: str,
    limit: int = 10,
    person_scope: DataScope | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    query_embedding: Sequence[float] | None = None,
    room_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Search one tenant's conversation segments: dense + keyword, fused by RRF.

    ``tenant_id`` is required and never defaulted. ``person_scope`` (a
    restricted ``DataScope``) keeps the caller to its own and shared segments.
    ``since`` / ``until`` bound event time (a segment overlapping the window
    matches). ``room_ids`` are the workspaces the caller can access; without
    them no workspace conversation is searched. ``query_embedding`` skips the
    embedding call; when the embedder is down, the keyword leg answers alone.

    Each hit: ``id``, ``kind`` ("segment"), ``source`` ("segment"), ``text``,
    ``topic``, ``source_kind``, ``source_ref``, ``start_ref``, ``end_ref``,
    ``started_at``, ``ended_at``, ``similarity`` (cosine, None when the dense
    leg did not return it), ``keyword_rank`` (``ts_rank_cd``, None when the
    keyword leg did not match it) and ``rrf_score``. Best first.
    """
    if not tenant_id:
        raise ValueError("search_segments needs a tenant_id")
    if limit <= 0:
        return []
    embedding = query_embedding
    if embedding is None:
        try:
            embedding = await _embed_query(query)
        except httpx.HTTPError as e:
            logger.warning(
                "search_segments: embedding failed (%s); keyword leg only", type(e).__name__
            )
            embedding = None
    where, params = _filters(person_scope=person_scope, since=since, until=until, room_ids=room_ids)
    dense, keyword = await asyncio.to_thread(
        _query_segments,
        query,
        embedding,
        tenant_id=tenant_id,
        fetch=max(SEARCH_CANDIDATES, limit * 3),
        where=where,
        params=params,
    )
    return _fuse(dense, keyword, limit)
