"""
Conversation Session Ingestion for Genus OS Memory System.

Turns what is said in Telegram and webchat conversations into facts, so that
interactive exchanges compound into memory. Called fire-and-forget after each
agent run in telegram.py / chat.py.

The source is the stored conversation (``chat_sessions`` / ``chat_messages``),
not the in-memory history: it has every turn, each with its message id and the
time it was said, and the person the session belongs to. The in-memory history
is used only to wait until the turn that triggered this run has been stored.

Architecture:
    stored turns past the conversation's watermark -> windows of consecutive
    turns (user and assistant interleaved) -> one conversation-mode extraction
    per window (``robothor.memory.conversation_facts``), the turns before it
    shown read-only -> each fact written through the write path with its
    speaker's origin and a link to its source turns -> watermark moves to the
    window's last message id

Each window is extracted once. A failed extraction, or a fact that could not be
stored, leaves the watermark where it was, so the same turns are extracted on
the next run; nothing is stored as a raw transcript.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import weakref
from dataclasses import dataclass, field
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.engine.sanitize import sanitize_log
from robothor.memory.conversation_facts import (
    ConversationTurn,
    extract_conversation_facts,
    render_window,
)
from robothor.memory.ingest_state import get_ingested_count, record_ingested

logger = logging.getLogger(__name__)

#: Most turns in one extraction window (the old trailing window's size).
WINDOW_MAX_TURNS = 20
#: Most characters of turn text in one window; a single longer turn is a
#: window of its own, never clipped.
WINDOW_MAX_CHARS = 12_000
#: Turns before a window shown to the model as read-only context.
CONTEXT_TURNS = 4
#: Most windows one run extracts, so a long backlog (a conversation older than
#: this feature) is covered over several runs instead of one burst.
MAX_WINDOWS_PER_RUN = 5
#: How long a run waits for the turn that triggered it to be committed (the
#: save and this ingest are spawned side by side).
STORE_WAIT_ATTEMPTS = 20
STORE_WAIT_SECONDS = 0.5

#: Watermark (``ingested_items``): the last ``chat_messages.id`` extracted.
_WATERMARK_SOURCE = "conversation_facts"

# Heuristic markers for an agent's OWN scheduled output (briefings/summaries).
# Re-ingesting these mistakes the agent re-reporting an event for the event
# recurring. Names are generic feature labels.
_GENERATED_MARKERS = (
    "morning briefing",
    "evening wind-down",
    "evening winddown",
    "daily summary",
    "weekly review",
)

#: One run at a time per conversation in this process.
_LOCKS: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = weakref.WeakValueDictionary()


def _ingest_skip_generated_enabled() -> bool:
    """Leave the agent's own briefings out of extraction (``MEMORY_INGEST_SKIP_GENERATED``).

    Default off. Every window is extracted once whatever this says; the flag
    only drops assistant turns that look like a scheduled briefing.
    """
    raw = os.environ.get("MEMORY_INGEST_SKIP_GENERATED", "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _is_generated_briefing(content: str) -> bool:
    """True if an assistant turn looks like the agent's own scheduled briefing."""
    head = (content or "").strip()[:240].lower()
    return any(m in head for m in _GENERATED_MARKERS)


@dataclass
class StoredConversation:
    """One session as stored: the turns past the watermark (oldest first, at
    most the run's limit), up to :data:`CONTEXT_TURNS` turns before them, who
    may read it (``segments.session_scope``), whether it is a workspace room,
    and the text of its last stored turn."""

    turns: list[ConversationTurn] = field(default_factory=list)
    context: list[ConversationTurn] = field(default_factory=list)
    scope_kind: str = "user"
    scope_id: str | None = None
    is_room: bool = False
    tail_text: str | None = None
    #: The highest message id read past the watermark, turn or not (a run of
    #: system rows moves the watermark too, so the next run starts past it).
    scanned_through: int | None = None


def _turn(row: dict[str, Any]) -> ConversationTurn | None:
    from robothor.secrets.redaction import redact

    message = row["message"] if isinstance(row["message"], dict) else json.loads(row["message"])
    role = message.get("role")
    content = message.get("content")
    if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
        return None
    speaker = message.get("author_display_name") if role == "user" else None
    return ConversationTurn(
        ref=int(row["id"]),
        role=role,
        text=redact(content),
        at=row["created_at"],
        speaker=str(speaker) if speaker else None,
    )


def _load_conversation(
    session_key: str, tenant_id: str, after_ref: int, limit: int
) -> StoredConversation | None:
    """Read one session's turns past ``after_ref`` (sync; run in a worker thread)."""
    from robothor.memory.segments import session_scope
    from robothor.rooms.keys import parse_room_key

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id, person_id FROM chat_sessions WHERE tenant_id = %s AND session_key = %s",
            (tenant_id, session_key),
        )
        session = cur.fetchone()
        if session is None:
            return None
        cols = "SELECT id, message, created_at FROM chat_messages WHERE session_id = %s "
        cur.execute(cols + "AND id > %s ORDER BY id LIMIT %s", (session["id"], after_ref, limit))
        new = list(cur.fetchall())
        cur.execute(
            cols + "AND id <= %s ORDER BY id DESC LIMIT %s",
            (session["id"], after_ref, CONTEXT_TURNS * 3),
        )
        before = list(reversed(cur.fetchall()))
        cur.execute(cols + "ORDER BY id DESC LIMIT 8", (session["id"],))
        last = [t for t in (_turn(r) for r in cur.fetchall()) if t is not None]
    person_id = str(session["person_id"]) if session["person_id"] else None
    kind, sid = session_scope(tenant_id, session_key, person_id)
    return StoredConversation(
        turns=[t for t in (_turn(r) for r in new) if t is not None],
        context=[t for t in (_turn(r) for r in before) if t is not None][-CONTEXT_TURNS:],
        scope_kind=kind,
        scope_id=sid,
        is_room=parse_room_key(session_key) is not None,
        tail_text=last[0].text if last else None,
        scanned_through=int(new[-1]["id"]) if new else None,
    )


def _expected_tail(history: list[dict[str, Any]]) -> str | None:
    from robothor.secrets.redaction import redact

    for m in reversed(history):
        content = m.get("content")
        if m.get("role") in ("user", "assistant") and isinstance(content, str) and content.strip():
            return redact(content).strip()
    return None


async def _load_when_stored(
    session_key: str, tenant_id: str, after_ref: int, history: list[dict[str, Any]]
) -> StoredConversation | None:
    """The stored conversation, once it holds the turn that triggered this run.

    Gives up waiting after :data:`STORE_WAIT_ATTEMPTS` tries and goes on with
    what is stored: a turn stored later is extracted by the next run.
    """
    expected = _expected_tail(history)
    limit = WINDOW_MAX_TURNS * MAX_WINDOWS_PER_RUN
    conv: StoredConversation | None = None
    for attempt in range(STORE_WAIT_ATTEMPTS):
        conv = await asyncio.to_thread(_load_conversation, session_key, tenant_id, after_ref, limit)
        stored = (conv.tail_text or "").strip() if conv is not None else None
        if expected is None or stored == expected:
            return conv
        if attempt + 1 < STORE_WAIT_ATTEMPTS:
            await asyncio.sleep(STORE_WAIT_SECONDS)
    return conv


def windows(turns: list[ConversationTurn]) -> list[list[ConversationTurn]]:
    """Consecutive runs of at most :data:`WINDOW_MAX_TURNS` turns and
    :data:`WINDOW_MAX_CHARS` characters, in order."""
    out: list[list[ConversationTurn]] = []
    current: list[ConversationTurn] = []
    chars = 0
    for t in turns:
        if current and (len(current) >= WINDOW_MAX_TURNS or chars + len(t.text) > WINDOW_MAX_CHARS):
            out.append(current)
            current, chars = [], 0
        current.append(t)
        chars += len(t.text)
    if current:
        out.append(current)
    return out


_MODE_KEYS = ("speaker_role", "about_user", "kind", "turn_refs", "mentioned_at")


#: The label of the speaker of a conversation nobody is known to own.
PARTICIPANT_LABEL = "Conversation participant (not linked to a person)"


@dataclass(frozen=True)
class Owner:
    """Who a conversation's facts belong to: the write scope, and what "the user"
    in them is anchored to.

    ``person_id`` is the CRM person the conversation belongs to (its recorded
    person, or the operator for the main session: ``segments.session_scope``).
    Without one, "the user" is a placeholder subject scoped to this
    conversation (``metadata.subject``), never a node: a private fact may not
    create one (``write_path``), and nothing is guessed from a name.
    """

    scope: tuple[str, str]
    person_id: str | None
    session_key: str

    def subject(self) -> dict[str, Any]:
        if self.person_id:
            return {"kind": "person", "person_id": self.person_id}
        return {
            "kind": "conversation_participant",
            "label": PARTICIPANT_LABEL,
            "conversation_ref": self.session_key,
            "owner": self.scope[1],
        }


def owner_of(conv: StoredConversation, session_key: str) -> Owner:
    """The conversation's own scope, exactly as its segments are scoped.

    A conversation with no known owner is ``("user", None)`` for its segments:
    readable by no one. A fact needs an owner id, so it gets
    ``conversation:{session_key}``, which no reader ever holds (reader ids are
    ``person:``, ``user:`` and ``agent:``): kept, and readable by no one, the
    same.
    """
    from robothor.memory.write_path import person_of

    sid = conv.scope_id or f"conversation:{session_key}"
    return Owner(("user", sid), person_of("user", sid), session_key)


def _prepared(fact: dict[str, Any], session_key: str, owner: Owner) -> dict[str, Any]:
    """An extracted fact as the write path takes it, its source turns in its metadata.

    A fact about the speaker gets the owner as its subject, and, when the owner
    is a CRM person, a ``the user`` mention anchored to that person's node.
    """
    refs = list(fact["turn_refs"])
    out = {k: v for k, v in fact.items() if k not in _MODE_KEYS}
    metadata: dict[str, Any] = {
        "extraction": "conversation",
        "conversation_kind": fact["kind"],
        "source": {
            "kind": "conversation",
            "ref": session_key,
            "start_ref": min(refs),
            "end_ref": max(refs),
            "turn_refs": refs,
            "mentioned_at": fact["mentioned_at"],
        },
    }
    if fact["about_user"]:
        metadata["subject"] = owner.subject()
        if owner.person_id:
            anchor = {"surface_form": "the user", "entity_type": "person"}
            out["mentions"] = [{**anchor, "crm_person_id": owner.person_id}, *fact["mentions"]]
    out["metadata"] = metadata
    return out


def _merge(total: dict[str, Any], part: dict[str, Any]) -> dict[str, Any]:
    """Two write results as one: ids joined, counts added."""
    out = dict(total)
    for key, value in part.items():
        if isinstance(value, bool):
            out[key] = bool(out.get(key)) or value
        elif isinstance(value, int):
            out[key] = int(out.get(key) or 0) + value
        elif isinstance(value, list):
            out[key] = list(out.get(key) or []) + value
    return out


async def _write_window(
    facts: list[dict[str, Any]],
    window: list[ConversationTurn],
    *,
    session_key: str,
    tenant_id: str,
    trigger_type: str,
    metadata: dict[str, Any],
    owner: Owner,
) -> dict[str, Any]:
    """Write one window's facts, grouped by speaker so each gets its origin, all
    in the conversation's own scope."""
    from robothor.memory import write_path

    source_content = render_window(window)
    result: dict[str, Any] = {}
    for role in ("user", "assistant"):
        group = [_prepared(f, session_key, owner) for f in facts if f["speaker_role"] == role]
        if not group:
            continue
        context = write_path.WriteContext.for_source(
            tenant_id=tenant_id,
            source_channel=trigger_type,
            content_type="conversation",
            role=role,
            metadata=metadata,
            scope=owner.scope,
        )
        part = await write_path.write_facts(group, source_content=source_content, context=context)
        result = _merge(result, part)
    return result


async def ingest_conversation_session(
    session_key: str,
    history: list[dict[str, Any]],
    agent_id: str,
    trigger_type: str,
    run_id: str,
    tenant_id: str = "",
) -> dict[str, Any] | None:
    """Extract the conversation's turns not yet extracted; never raises.

    Returns the merged write result with ``llm_calls``, ``windows`` and
    ``turns_covered``, or ``None`` when nothing was extracted (nothing new, the
    session is not stored, or extraction or storage failed — those turns are
    retried by the next run).
    """
    tenant = tenant_id or DEFAULT_TENANT
    try:
        lock = _LOCKS.setdefault((tenant, session_key), asyncio.Lock())
        async with lock:
            return await _ingest(session_key, history, agent_id, trigger_type, run_id, tenant)
    except Exception:
        logger.warning(
            "Conversation ingestion failed for %s", sanitize_log(session_key), exc_info=True
        )
        return None


async def _ingest(
    session_key: str,
    history: list[dict[str, Any]],
    agent_id: str,
    trigger_type: str,
    run_id: str,
    tenant: str,
) -> dict[str, Any] | None:
    after = get_ingested_count(_WATERMARK_SOURCE, session_key, tenant_id=tenant)
    conv = await _load_when_stored(session_key, tenant, after, history)
    if conv is None:
        return None
    if not conv.turns:
        if conv.scanned_through is not None and conv.scanned_through > after:
            record_ingested(
                _WATERMARK_SOURCE, session_key, str(conv.scanned_through), [], tenant_id=tenant
            )
        return None
    if conv.is_room:
        # A workspace's segments are read only by its members, but a fact has
        # no room scope: written shared, it would reach every reader in the
        # tenant. Until facts can carry a room's scope, rooms are not extracted.
        logger.debug("Conversation %s is a room; not extracted", sanitize_log(session_key))
        return None
    owner = owner_of(conv, session_key)

    metadata = {"session_key": session_key, "agent_id": agent_id, "run_id": run_id}
    skip_generated = _ingest_skip_generated_enabled()
    seen = list(conv.context)
    result: dict[str, Any] = {"fact_ids": [], "llm_calls": 0, "windows": 0, "turns_covered": 0}
    for window in windows(conv.turns):
        last_ref = window[-1].ref
        kept = [
            t
            for t in window
            if not (skip_generated and t.role == "assistant" and _is_generated_briefing(t.text))
        ]
        if kept:
            context = seen[-CONTEXT_TURNS:]
            extraction = await extract_conversation_facts(kept, context=context)
            result["llm_calls"] += 1
            if extraction.error is not None:
                logger.warning(
                    "Conversation extraction failed for %s (%s); will retry",
                    sanitize_log(session_key),
                    extraction.error,
                )
                return _done(result)
            part = await _write_window(
                extraction.facts,
                kept,
                session_key=session_key,
                tenant_id=tenant,
                trigger_type=trigger_type,
                metadata=metadata,
                owner=owner,
            )
            if part.get("facts_failed"):
                logger.warning(
                    "Conversation facts not stored for %s (%s); will retry",
                    sanitize_log(session_key),
                    "; ".join(part.get("errors") or []),
                )
                return _done(result)
            result = _merge(result, part)
        record_ingested(
            _WATERMARK_SOURCE,
            session_key,
            str(last_ref),
            list(result.get("fact_ids") or []),
            tenant_id=tenant,
        )
        result["windows"] += 1
        result["turns_covered"] += len(window)
        seen.extend(window)

    logger.info(
        "Ingested conversation session %s: %d windows, %d facts",
        sanitize_log(session_key),
        result["windows"],
        len(result["fact_ids"]),
    )
    return result


def _done(result: dict[str, Any]) -> dict[str, Any] | None:
    """What a run that stopped early returns: what it completed, else None."""
    return result if result["windows"] else None
