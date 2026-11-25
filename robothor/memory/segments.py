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
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection
from robothor.memory.fusion import rrf_fuse
from robothor.memory.vector_tuning import apply_hnsw_session

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence
    from datetime import datetime


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
#: Size budget of one segment's text, so a hit fits a context budget. A single
#: turn longer than this is never clipped: it is split at paragraph, then
#: sentence, then word boundaries into consecutive parts (see ``part``).
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
#: Most turns past those already covered that one indexing run reads, so the
#: first index of a long history proceeds in bounded runs (each later
#: exchange continues it). At least two full segments.
SEGMENT_INDEX_MAX_TURNS = 200
#: Candidates fetched per search leg before fusion (at least 3x the limit).
SEARCH_CANDIDATES = 30
#: Characters of a segment the cross-encoder reads: an excerpt around the best
#: keyword match (else the head). The stored and returned text stay whole.
SEGMENT_RERANK_CHARS = 1200

_ROLE_LABELS = {"user": "User", "assistant": "Assistant"}
_LABEL_ROOM = max(len(f"{label}: ") for label in _ROLE_LABELS.values())
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*\s+")
_EXCERPT_STOP = frozenset(
    [
        "the",
        "and",
        "for",
        "are",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "how",
        "you",
        "your",
        "can",
        "could",
        "would",
        "should",
        "with",
        "from",
        "that",
        "this",
        "there",
        "have",
        "has",
        "had",
        "did",
        "does",
        "about",
        "into",
    ]
)


@dataclass(frozen=True)
class Turn:
    """One conversation turn. ``ref`` is its ``chat_messages.id``.

    A turn longer than the size budget is carried as ``pieces`` consecutive
    turns with the same ``ref`` and ``part`` 0 .. pieces-1; their texts
    concatenate to the original.
    """

    ref: int
    role: str
    text: str
    at: datetime | None = None
    embedding: Sequence[float] | None = None
    part: int = 0
    pieces: int = 1


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
    def part(self) -> int:
        """0, or the piece number when the segment is one part of a long turn."""
        return self.turns[0].part

    @property
    def text(self) -> str:
        return "\n".join(_line(t) for t in self.turns)

    @property
    def topic(self) -> str | None:
        first = next((t for t in self.turns if t.role == "user"), self.turns[0])
        return _label(first.text)


def _line(turn: Turn) -> str:
    return f"{_ROLE_LABELS.get(turn.role, turn.role.title())}: {turn.text}"


def _cut(text: str, lo: int, hi: int) -> int:
    """Where to end a piece in ``text[lo:hi]``: after the last paragraph break,
    else the last sentence end, else the last space; ``hi`` if none."""
    window = text[lo:hi]
    for sep in ("\n\n", "\n"):
        j = window.rfind(sep)
        if j >= 0:
            return lo + j + len(sep)
    ends = list(_SENTENCE_END.finditer(window))
    if ends:
        return lo + ends[-1].end()
    j = window.rfind(" ")
    return lo + j + 1 if j >= 0 else hi


def _split_long(turn: Turn) -> list[Turn]:
    """A turn over the size budget as consecutive pieces; never clipped.

    Each piece is at most the budget minus the role label, cut at a paragraph,
    sentence or word boundary in its second half when there is one. The
    pieces' texts concatenate exactly to the turn's.
    """
    limit = SEGMENT_MAX_CHARS - _LABEL_ROOM
    text = turn.text
    if len(text) <= limit:
        return [turn]
    pieces: list[str] = []
    pos = 0
    while len(text) - pos > limit:
        cut = _cut(text, pos + limit // 2, pos + limit)
        pieces.append(text[pos:cut])
        pos = cut
    pieces.append(text[pos:])
    return [
        Turn(turn.ref, turn.role, piece, turn.at, turn.embedding, part=i, pieces=len(pieces))
        for i, piece in enumerate(pieces)
    ]


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


def _is_piece(turns: Sequence[Turn]) -> bool:
    return any(t.pieces > 1 for t in turns)


def _is_fragment(turns: Sequence[Turn]) -> bool:
    if _is_piece(turns):
        return False
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

    A single turn over the budget is split (:func:`_split_long`) into pieces,
    each its own segment with the turn's ref and ``part`` 0, 1, 2 …; a piece
    never shares a segment with another turn.

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
    kept = [piece for t in kept for piece in _split_long(t)]

    groups: list[list[Turn]] = [[kept[0]]]
    for i in range(1, len(kept)):
        turn, prev, current = kept[i], kept[i - 1], groups[-1]
        if _breaks(kept, i, turn, prev, current):
            groups.append([turn])
        else:
            current.append(turn)

    return [Segment(tuple(g)) for g in _absorb_fragments(groups)]


def _breaks(kept: list[Turn], i: int, turn: Turn, prev: Turn, current: list[Turn]) -> bool:
    if turn.pieces > 1 or prev.pieces > 1:
        return True
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
            if not _is_piece(group) and _chars([*pending, *group]) <= SEGMENT_MAX_CHARS:
                group = [*pending, *group]
            else:
                out.append(pending)
            pending = []
        if _is_fragment(group):
            if out and not _is_piece(out[-1]) and _chars([*out[-1], *group]) <= SEGMENT_MAX_CHARS:
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
    retention, oldest first. ``scope_kind`` / ``scope_id`` are who may read the
    session's segments (:func:`conversation_scope`).
    """

    session_id: int
    person_id: str | None
    resume_from: int | None
    covered_through: int | None
    turns: list[Turn] = field(default_factory=list)
    scope_kind: str = "user"
    scope_id: str | None = None


#: The session key of the operator's main conversation when no engine config
#: names another (``EngineConfig.main_session_key``).
DEFAULT_MAIN_SESSION_KEY = "agent:main:primary"


def conversation_scope(
    session_key: str,
    person_id: str | None,
    *,
    owner_person_id: str | None = None,
    main_session_key: str = DEFAULT_MAIN_SESSION_KEY,
) -> tuple[str, str | None]:
    """Who may read a conversation's segments: ``(scope_kind, scope_id)``.

    Fails closed: a conversation is ``shared`` only when it is known to be
    multi-party, and today that is a workspace (``agent:{agent}:room:{id}``,
    gated by room membership at read time, even when it records a person).
    Every other conversation is ``user``, owned by the first of:

    1. the session's recorded person — ``person:{person_id}``;
    2. the member a webchat session key names (``agent:{agent}:user:{uid}``,
       the key ``chat.derive_user_session_key`` writes) — ``user:{uid}``;
    3. for the operator's main session, the operator's person
       (``owner_person_id``) — ``person:{id}``.

    With no owner (an unrecognised direct message, a TUI or service session,
    the main session of an instance whose operator is not linked to a person)
    the scope is ``("user", None)``: readable by no one until an owner is known.
    """
    from robothor.rooms.keys import parse_room_key

    if parse_room_key(session_key) is not None:
        return "shared", None
    if person_id:
        return "user", f"person:{person_id}"
    parts = session_key.split(":")
    if len(parts) == 4 and parts[0] == "agent" and parts[2] == "user" and parts[3]:
        return "user", f"user:{parts[3]}"
    if session_key == main_session_key and owner_person_id:
        return "user", f"person:{owner_person_id}"
    return "user", None


def reader_principals(person_id: str | None, user_id: str | None) -> list[str]:
    """The ``scope_id`` values a reader owns: their person and their user id."""
    out = []
    if person_id:
        out.append(f"person:{person_id}")
    if user_id:
        out.append(f"user:{user_id}")
    return out


def _main_session_key() -> str:
    """The engine's configured main session key, or the default one."""
    try:
        from robothor.engine.chat import get_main_session_key

        return get_main_session_key()
    except ImportError:
        return DEFAULT_MAIN_SESSION_KEY


def _owner_person_id(tenant_id: str) -> str | None:
    """The operator's person (root CLAUDE.md rule 12: ``get_owner_person``)."""
    from robothor.crm.dal import get_owner_person

    owner = get_owner_person(tenant_id).get("id")
    return str(owner) if owner else None


def _parse_vector(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = json.loads(value)
        return [float(x) for x in parsed]
    return [float(x) for x in value]


def _load_conversation(
    session_key: str, tenant_id: str, retention_days: int, max_turns: int
) -> _Conversation | None:
    """Read one session's uncovered turns (sync; run in a worker thread).

    The turns of the last stored segment (it may grow), then at most
    ``max_turns`` turns past everything already covered, so a long history is
    indexed over several runs instead of in one batch.
    """
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
        covered = mark.get("covered_through")
        query = """
            SELECT id, message, created_at, embedding::text AS embedding
            FROM chat_messages
            WHERE session_id = %s AND id >= %s AND id <= %s
              AND created_at >= NOW() - make_interval(days => %s)
            ORDER BY id
            """
        reopened: list[Any] = []
        if resume_from is not None and covered is not None:
            cur.execute(query, (session["id"], resume_from, covered, retention_days))
            reopened = list(cur.fetchall())
        cur.execute(
            """
            SELECT id, message, created_at, embedding::text AS embedding
            FROM chat_messages
            WHERE session_id = %s AND id > %s
              AND created_at >= NOW() - make_interval(days => %s)
            ORDER BY id
            LIMIT %s
            """,
            (session["id"], covered or 0, retention_days, max_turns),
        )
        rows = reopened + list(cur.fetchall())

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
    person_id = str(session["person_id"]) if session["person_id"] else None
    main_key = _main_session_key()
    owner = _owner_person_id(tenant_id) if session_key == main_key and not person_id else None
    scope_kind, scope_id = conversation_scope(
        session_key, person_id, owner_person_id=owner, main_session_key=main_key
    )
    return _Conversation(
        session_id=int(session["id"]),
        person_id=person_id,
        resume_from=int(resume_from) if resume_from is not None else None,
        covered_through=(
            int(mark["covered_through"]) if mark.get("covered_through") is not None else None
        ),
        turns=turns,
        scope_kind=scope_kind,
        scope_id=scope_id,
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
                "AND start_ref >= %s AND (start_ref, part) NOT IN "
                "(SELECT * FROM unnest(%s::bigint[], %s::smallint[]))",
                (
                    tenant_id,
                    session_key,
                    conversation.resume_from,
                    [s.start_ref for s in segs],
                    [s.part for s in segs],
                ),
            )
        # Scope belongs to the conversation, not to the run that wrote a row:
        # a session whose owner became known (or changed) re-scopes every
        # segment it already has, not only the tail rewritten below.
        cur.execute(
            "UPDATE memory_segments SET scope_kind = %s, scope_id = %s, updated_at = NOW() "
            "WHERE tenant_id = %s AND source_kind = 'conversation' AND source_ref = %s "
            "AND (scope_kind, scope_id) IS DISTINCT FROM (%s::text, %s::text)",
            (
                conversation.scope_kind,
                conversation.scope_id,
                tenant_id,
                session_key,
                conversation.scope_kind,
                conversation.scope_id,
            ),
        )
        written = int(cur.rowcount or 0)
        for seg, vec in zip(segs, vectors, strict=True):
            cur.execute(
                """
                INSERT INTO memory_segments (
                    tenant_id, source_kind, source_ref, start_ref, end_ref, part,
                    started_at, ended_at, topic, text, embedding,
                    person_id, scope_kind, scope_id, chat_session_id
                ) VALUES (
                    %s, 'conversation', %s, %s, %s, %s, %s, %s, %s, %s, %s::vector,
                    %s, %s, %s, %s
                )
                ON CONFLICT (tenant_id, source_kind, source_ref, start_ref, part) DO UPDATE SET
                    end_ref = EXCLUDED.end_ref,
                    started_at = EXCLUDED.started_at,
                    ended_at = EXCLUDED.ended_at,
                    topic = EXCLUDED.topic,
                    text = EXCLUDED.text,
                    embedding = EXCLUDED.embedding,
                    person_id = EXCLUDED.person_id,
                    scope_kind = EXCLUDED.scope_kind,
                    scope_id = EXCLUDED.scope_id,
                    chat_session_id = EXCLUDED.chat_session_id,
                    updated_at = NOW()
                WHERE memory_segments.end_ref IS DISTINCT FROM EXCLUDED.end_ref
                   OR memory_segments.text IS DISTINCT FROM EXCLUDED.text
                   OR memory_segments.scope_kind IS DISTINCT FROM EXCLUDED.scope_kind
                   OR memory_segments.scope_id IS DISTINCT FROM EXCLUDED.scope_id
                """,
                (
                    tenant_id,
                    session_key,
                    seg.start_ref,
                    seg.end_ref,
                    seg.part,
                    seg.started_at,
                    seg.ended_at,
                    seg.topic,
                    seg.text,
                    vec,
                    conversation.person_id,
                    conversation.scope_kind,
                    conversation.scope_id,
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
    ``(tenant_id, 'conversation', session_key, start_ref, part)``, with the
    session's reader scope (:func:`conversation_scope`). Running it again
    with no new turn writes nothing. Database work runs in a worker thread.

    ``embed_batch`` is injected in tests; the default is the local Ollama
    embedder. An embedding failure raises and writes nothing, so the next run
    retries the same turns. Callers on the request path must not await this
    directly; ``chat_store.save_exchange_async`` schedules it in the background
    and logs any failure.
    """
    if not tenant_id:
        raise ValueError("index_conversation needs a tenant_id")
    key = (tenant_id, session_key)
    if key in _IN_FLIGHT:
        # A run for this conversation is already loading or embedding; this
        # exchange's turns are picked up by it or by the next exchange.
        return 0
    _IN_FLIGHT.add(key)
    try:
        return await _index_conversation(session_key, tenant_id, embed_batch)
    finally:
        _IN_FLIGHT.discard(key)


#: Conversations with an indexing run in progress in this process.
_IN_FLIGHT: set[tuple[str, str]] = set()


async def _index_conversation(
    session_key: str,
    tenant_id: str,
    embed_batch: Callable[[list[str]], Awaitable[list[list[float] | None]]] | None,
) -> int:
    conversation = await asyncio.to_thread(
        _load_conversation,
        session_key,
        tenant_id,
        SEGMENT_RETENTION_DAYS,
        SEGMENT_INDEX_MAX_TURNS,
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
    "id, text, topic, source_kind, source_ref, start_ref, end_ref, part, "
    "started_at, ended_at, scope_kind"
)

# The keyword leg ORs the query's terms: plainto_tsquery ANDs them, so a long
# natural question ("could you remind me what the locker number was") matched
# nothing unless every word appeared. Stop words are dropped and terms stemmed
# by plainto_tsquery first; an all-stop-word query becomes NULL and matches
# nothing.
_OR_TSQUERY = "NULLIF(replace(plainto_tsquery('english', %s)::text, ' & ', ' | '), '')::tsquery"


def _filters(
    *,
    reader_person_id: str | None,
    reader_user_id: str | None = None,
    since: datetime | None,
    until: datetime | None,
    room_ids: Iterable[str] | None,
) -> tuple[str, list[Any]]:
    """The visibility rule and the time window, as SQL.

    Visibility — applied on every search, whatever the data-scoping rollout
    says, because verbatim conversation is more sensitive than an extracted
    fact: a reader sees ``shared`` segments, and ``user`` segments whose
    ``scope_id`` is one of the reader's own principals (``person:{id}``,
    ``user:{id}``; :func:`reader_principals`). With no reader, shared only. A
    ``user`` segment with no owner (``scope_id`` NULL) matches no reader. A shared segment of a workspace conversation
    (``agent:{agent}:room:{id}``) is seen only for the rooms in ``room_ids``,
    as ``search_chat_turns`` does; with none passed, no workspace is searched.
    """
    from robothor.rooms.keys import canonical_uuid

    allowed = sorted({c for c in (canonical_uuid(r) for r in room_ids or ()) if c})
    clauses = [
        "((scope_kind = 'shared' AND (source_kind <> 'conversation' "
        "OR split_part(source_ref, ':', 3) <> 'room' "
        "OR split_part(source_ref, ':', 4) = ANY(%s::text[]))) "
        "OR (scope_kind = 'user' AND scope_id = ANY(%s::text[])))"
    ]
    params: list[Any] = [allowed, reader_principals(reader_person_id, reader_user_id)]
    if since is not None:
        clauses.append("ended_at >= %s")
        params.append(since)
    if until is not None:
        clauses.append("started_at <= %s")
        params.append(until)
    return "".join(f" AND {c}" for c in clauses), params


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
        # ef_search plus, where pgvector supports it, an iterative scan: a small
        # tenant beside a large one still fills the LIMIT.
        apply_hnsw_session(cur)
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
        "part": int(row["part"]),
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "scope_kind": row["scope_kind"],
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
    """Embed a retrieval query the way ``search_facts`` does (query instruction)."""
    from robothor.llm import ollama as llm_client

    return list(await llm_client.get_embedding_async(llm_client.format_embedding_query(query)))


def rerank_excerpt(text: str, query: str, size: int = SEGMENT_RERANK_CHARS) -> str:
    """At most ``size`` characters of ``text`` for the cross-encoder to read.

    The window holding the most distinct query terms (three letters or more,
    common words dropped), else the head. Only what the reranker reads; the
    stored and returned text stay whole.
    """
    if len(text) <= size:
        return text
    terms = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 3} - _EXCERPT_STOP
    lowered = text.lower()
    hits = sorted(
        (m.start(), term) for term in terms for m in re.finditer(rf"\b{re.escape(term)}", lowered)
    )[:200]
    if not hits:
        return text[:size]
    half = size // 2
    best_pos, best_count = hits[0][0], 0
    for pos, _term in hits:
        count = len({t for p, t in hits if abs(p - pos) <= half})
        if count > best_count:
            best_pos, best_count = pos, count
    start = max(0, min(best_pos - half, len(text) - size))
    return text[start : start + size]


async def search_segments(
    query: str,
    *,
    tenant_id: str,
    limit: int = 10,
    reader_person_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    query_embedding: Sequence[float] | None = None,
    room_ids: Iterable[str] | None = None,
    embed_missing: bool = True,
    reader_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Search one tenant's conversation segments: dense + keyword, fused by RRF.

    ``tenant_id`` is required and never defaulted. ``reader_person_id`` and
    ``reader_user_id`` are the reader's own person and user: segments they own
    are returned with the shared ones; without either, shared segments only
    (see :func:`_filters`).
    ``room_ids`` are the workspaces the reader can access; without them no
    workspace conversation is searched. ``since`` / ``until`` bound event time
    (a segment overlapping the window matches). ``query_embedding`` skips the
    embedding call; ``embed_missing=False`` skips it too, for a caller whose
    own embedding already failed. When there is no embedding the keyword leg
    answers alone.

    Each hit: ``id``, ``kind`` ("segment"), ``source`` ("segment"), ``text``,
    ``topic``, ``source_kind``, ``source_ref``, ``start_ref``, ``end_ref``,
    ``part``, ``started_at``, ``ended_at``, ``scope_kind``, ``similarity``
    (cosine, None when the dense leg did not return it), ``keyword_rank``
    (``ts_rank_cd``, None when the keyword leg did not match it) and
    ``rrf_score``. Best first.
    """
    if not tenant_id:
        raise ValueError("search_segments needs a tenant_id")
    if limit <= 0:
        return []
    embedding = query_embedding
    if embedding is None and embed_missing:
        try:
            embedding = await _embed_query(query)
        except httpx.HTTPError as e:
            logger.warning(
                "search_segments: embedding failed (%s); keyword leg only", type(e).__name__
            )
            embedding = None
    where, params = _filters(
        reader_person_id=reader_person_id,
        reader_user_id=reader_user_id,
        since=since,
        until=until,
        room_ids=room_ids,
    )
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
