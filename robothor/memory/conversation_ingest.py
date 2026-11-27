"""
Conversation Session Ingestion for Genus OS Memory System.

Turns what is said in Telegram and webchat conversations into facts, so that
interactive exchanges compound into memory. Called fire-and-forget after each
agent run in telegram.py / chat.py.

The source is the stored conversation (``chat_sessions`` / ``chat_messages``),
not the in-memory history: it has every turn, each with its message id and the
time it was said, and the person the session belongs to. The in-memory history
is used only to tell whether the turn that triggered this run is stored yet.

Architecture:
    claim the conversation's watermark row (a lease: one run at a time, across
    engine processes) -> stored turns past the watermark, except those written
    in the last few seconds -> windows of consecutive turns (user and assistant
    interleaved) -> one conversation-mode extraction per window
    (``robothor.memory.conversation_facts``), the turns before it shown
    read-only -> each fact written through the write path with its speaker's
    origin and a link to its source turns -> watermark moves to the window's
    last message id -> release; a follow-up run is scheduled for turns held
    back, not stored yet, or past this run's five windows

Each window is extracted once. A failed extraction, or a fact that could not be
stored, leaves the watermark where it was, so the same turns are extracted on
the next run; a window that fails three runs in a row is recorded as skipped
and passed over. Nothing is stored as a raw transcript. Every database call
runs off the event loop.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.engine.sanitize import sanitize_log
from robothor.memory.conversation_facts import (
    ConversationTurn,
    extract_conversation_facts,
)
from robothor.memory.graph_schema import Origin

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: Most turns in one extraction window (the old trailing window's size).
WINDOW_MAX_TURNS = 20
#: Most characters of turn text in one window. A single longer turn is a window
#: of its own, and the model reads only this much of it (``_clipped``).
WINDOW_MAX_CHARS = 12_000
#: Runs in a row that may fail to extract the same window before it is skipped.
MAX_WINDOW_FAILURES = 3
#: Where a skipped window is recorded (``ingested_items``), one row per window.
_SKIPPED_SOURCE = "conversation_facts_skipped"
#: Turns before a window shown to the model as read-only context.
CONTEXT_TURNS = 4
#: Most windows one run extracts, so a long backlog (a conversation older than
#: this feature) is covered over several runs instead of one burst.
MAX_WINDOWS_PER_RUN = 5
#: Rows written more recently than this are left for a follow-up run (see
#: ``held_from``), which is scheduled a little after they come of age.
HOLD_SECONDS = 5.0
#: How soon a follow-up run continues a backlog longer than one run's windows.
BACKLOG_DELAY_SECONDS = 1.0

#: Watermark (``ingested_items``): see "the watermark row" below.
_WATERMARK_SOURCE = "conversation_facts"
#: How long a claim on a conversation lasts if its run never releases it.
CLAIM_LEASE_SECONDS = 900
#: When a run finds the conversation claimed, how soon it looks again.
CLAIM_RETRY_SECONDS = 30.0
#: Most follow-up runs one exchange can lead to (see ``_Run.again``).
MAX_FOLLOW_UPS = 20

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
    #: The first message id held back from this run (see the loader), if any.
    held_from: int | None = None
    #: More turns are stored past those loaded.
    more: bool = False
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
        cur.execute(
            "SELECT id, message, created_at, "
            "created_at > now() - make_interval(secs => %s) AS fresh "
            "FROM chat_messages WHERE session_id = %s AND id > %s ORDER BY id LIMIT %s",
            (HOLD_SECONDS, session["id"], after_ref, limit + 1),
        )
        new = list(cur.fetchall())
        more = len(new) > limit
        new = new[:limit]
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
        held_from=held_from([(int(r["id"]), _role(r), bool(r["fresh"])) for r in new]),
        more=more,
    )


def _role(row: dict[str, Any]) -> str | None:
    message = row["message"] if isinstance(row["message"], dict) else json.loads(row["message"])
    role = message.get("role")
    return str(role) if role else None


def held_from(rows: list[tuple[int, str | None, bool]]) -> int | None:
    """The first message id this run must not extract yet, from ``(id, role, fresh)``.

    A row written in the last :data:`HOLD_SECONDS` may have a lower id still
    uncommitted beside it (two writers on one session): extracting it now would
    move the watermark past that row for good. So the first fresh row and
    everything after it wait for a follow-up run. A fresh assistant reply holds
    back the user turn it answers with it (the user turn is stamped with its
    arrival time, so it can look old).
    """
    for i, (ref, role, fresh) in enumerate(rows):
        if fresh:
            if i > 0 and role == "assistant" and rows[i - 1][1] == "user":
                return rows[i - 1][0]
            return ref
    return None


def _expected_tail(history: list[dict[str, Any]]) -> str | None:
    from robothor.secrets.redaction import redact

    for m in reversed(history):
        content = m.get("content")
        if m.get("role") in ("user", "assistant") and isinstance(content, str) and content.strip():
            return redact(content).strip()
    return None


def windows(
    turns: list[ConversationTurn], *, first_end: int | None = None
) -> list[list[ConversationTurn]]:
    """Consecutive runs of at most :data:`WINDOW_MAX_TURNS` turns and
    :data:`WINDOW_MAX_CHARS` characters, in order. ``first_end`` closes the
    first window after that message id (the window that failed before)."""
    if first_end is not None:
        head = [t for t in turns if t.ref <= first_end]
        if head:
            first = windows(head)[0]
            return [first, *windows(turns[len(first) :])]
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


def _clipped(turn: ConversationTurn) -> ConversationTurn:
    """The turn as the model reads it: at most :data:`WINDOW_MAX_CHARS` of its
    text, with a marker saying how much was left out. Stored turns are untouched."""
    if len(turn.text) <= WINDOW_MAX_CHARS:
        return turn
    hidden = len(turn.text) - WINDOW_MAX_CHARS
    text = f"{turn.text[:WINDOW_MAX_CHARS]}\n[… {hidden} more characters not shown]"
    return dataclasses.replace(turn, text=text)


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
    if fact["about_user"] and fact["speaker_role"] == "user":
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

    # A reference, never the turns themselves: the turns stay in chat_messages
    # (with their own retention), and no raw transcript is stored with a fact.
    source_content = f"conversation:{session_key}#{window[0].ref}-{window[-1].ref}"
    result: dict[str, Any] = {}
    for speaker in ("user", "assistant", "external"):
        group = [_prepared(f, session_key, owner) for f in facts if f["speaker_role"] == speaker]
        if not group:
            continue
        # Someone else's words, relayed in a user turn or said by a guest: they
        # reached us through the chat, so they are external, never user-stated.
        external = speaker == "external"
        context = write_path.WriteContext.for_source(
            tenant_id=tenant_id,
            source_channel=trigger_type,
            content_type="conversation",
            role="user" if external else speaker,
            metadata=metadata,
            scope=owner.scope,
            origin_cap=Origin.EXTERNAL if external else None,
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
    *,
    attempt: int = 0,
) -> dict[str, Any] | None:
    """Extract the conversation's turns not yet extracted; never raises.

    Returns the merged write result with ``llm_calls``, ``windows`` and
    ``turns_covered``, or ``None`` when nothing was extracted (nothing new, the
    session is not stored, or extraction or storage failed — those turns are
    retried by the next run). ``attempt`` counts the follow-up runs this one
    was scheduled by (:func:`_schedule`).
    """
    from robothor.rooms.keys import parse_room_key

    if parse_room_key(session_key) is not None:
        # A workspace's segments are read only by its members, but a fact has
        # no room scope: written shared, it would reach every reader in the
        # tenant. Until facts can carry a room's scope, rooms are not extracted,
        # and nothing about them is read or written here.
        return None
    tenant = tenant_id or DEFAULT_TENANT
    run = _Run(session_key, history, agent_id, trigger_type, run_id, tenant, attempt)
    try:
        return await _ingest(run)
    except Exception:
        logger.warning(
            "Conversation ingestion failed for %s", sanitize_log(session_key), exc_info=True
        )
        return None


@dataclass(frozen=True)
class _Run:
    session_key: str
    history: list[dict[str, Any]]
    agent_id: str
    trigger_type: str
    run_id: str
    tenant: str
    attempt: int

    def again(self, delay: float) -> None:
        """Schedule a follow-up run of this conversation (bounded)."""
        if self.attempt >= MAX_FOLLOW_UPS:
            logger.info(
                "Conversation %s: follow-up limit reached; the next exchange continues it",
                sanitize_log(self.session_key),
            )
            return

        async def follow_up() -> None:
            await ingest_conversation_session(
                self.session_key,
                [],
                self.agent_id,
                self.trigger_type,
                self.run_id,
                self.tenant,
                attempt=self.attempt + 1,
            )

        _schedule(delay, follow_up)


def _schedule(delay: float, fn: Callable[[], Awaitable[None]]) -> None:
    """Run ``fn`` after ``delay`` seconds, in the background (the task registry)."""
    from robothor.engine.task_registry import get_task_registry

    async def later() -> None:
        await asyncio.sleep(delay)
        await fn()

    get_task_registry().spawn(later(), name="conv-ingest-follow-up")


async def _ingest(run: _Run) -> dict[str, Any] | None:
    key, tenant = run.session_key, run.tenant
    mark = await asyncio.to_thread(_claim, key, tenant)
    if mark is None:
        # Another run (in this process or another engine) holds the
        # conversation. It may have read the turns before this exchange was
        # stored, so look again once it is likely done.
        run.again(CLAIM_RETRY_SECONDS)
        return None
    try:
        return await _extract(run, mark)
    finally:
        await asyncio.to_thread(_release, key, tenant, mark.token)


async def _extract(run: _Run, mark: Watermark) -> dict[str, Any] | None:
    key, tenant = run.session_key, run.tenant
    conv = await asyncio.to_thread(
        _load_conversation, key, tenant, mark.through, WINDOW_MAX_TURNS * MAX_WINDOWS_PER_RUN
    )
    expected = _expected_tail(run.history)
    if conv is None:
        if expected is not None:
            run.again(HOLD_SECONDS + 1)  # the save may not have committed yet
        return None
    later = _follow_up_delay(conv, expected)
    turns = [t for t in conv.turns if conv.held_from is None or t.ref < conv.held_from]
    if not turns:
        if (
            conv.held_from is None
            and conv.scanned_through is not None
            and conv.scanned_through > mark.through
        ):
            await asyncio.to_thread(_advance, key, tenant, mark.token, conv.scanned_through, [])
        if later is not None:
            run.again(later)
        return None
    owner = owner_of(conv, key)

    metadata = {"session_key": key, "agent_id": run.agent_id, "run_id": run.run_id}
    skip_generated = _ingest_skip_generated_enabled()
    seen = list(conv.context)
    result: dict[str, Any] = {
        "fact_ids": [],
        "llm_calls": 0,
        "windows": 0,
        "windows_skipped": 0,
        "turns_covered": 0,
    }
    all_windows = windows(turns, first_end=mark.failed_end)
    if len(all_windows) > MAX_WINDOWS_PER_RUN:
        later = min(later or BACKLOG_DELAY_SECONDS, BACKLOG_DELAY_SECONDS)
    for window in all_windows[:MAX_WINDOWS_PER_RUN]:
        last_ref = window[-1].ref
        kept = [
            _clipped(t)
            for t in window
            if not (skip_generated and t.role == "assistant" and _is_generated_briefing(t.text))
        ]
        if kept:
            context = seen[-CONTEXT_TURNS:]
            extraction = await extract_conversation_facts(kept, context=context)
            result["llm_calls"] += 1
            if extraction.error is not None:
                failures = await asyncio.to_thread(_fail, key, tenant, mark.token, last_ref)
                if failures < MAX_WINDOW_FAILURES:
                    logger.warning(
                        "Conversation extraction failed for %s (%s, %d of %d); will retry",
                        sanitize_log(key),
                        extraction.error,
                        failures,
                        MAX_WINDOW_FAILURES,
                    )
                    return _done(result)
                # It never extracts: pass over it, so the rest of the conversation
                # is not held behind it, and say so.
                logger.warning(
                    "Conversation %s: skipping turns %d-%d after %d failed extractions (%s)",
                    sanitize_log(key),
                    window[0].ref,
                    last_ref,
                    failures,
                    extraction.error,
                )
                await asyncio.to_thread(
                    _record_skip, key, tenant, window[0].ref, last_ref, str(extraction.error)
                )
                await asyncio.to_thread(
                    _advance, key, tenant, mark.token, last_ref, list(result["fact_ids"])
                )
                result["windows_skipped"] += 1
                seen.extend(window)
                continue
            part = await _write_window(
                extraction.facts,
                kept,
                session_key=key,
                tenant_id=tenant,
                trigger_type=run.trigger_type,
                metadata=metadata,
                owner=owner,
            )
            if part.get("facts_failed"):
                logger.warning(
                    "Conversation facts not stored for %s (%s); will retry",
                    sanitize_log(key),
                    "; ".join(part.get("errors") or []),
                )
                return _done(result)
            result = _merge(result, part)
        await asyncio.to_thread(
            _advance, key, tenant, mark.token, last_ref, list(result.get("fact_ids") or [])
        )
        result["windows"] += 1
        result["turns_covered"] += len(window)
        seen.extend(window)

    if later is not None:
        run.again(later)
    logger.info(
        "Ingested conversation session %s: %d windows, %d facts",
        sanitize_log(key),
        result["windows"],
        len(result["fact_ids"]),
    )
    return result


def _follow_up_delay(conv: StoredConversation, expected: str | None) -> float | None:
    """When a follow-up run is needed, how soon: held-back rows and a triggering
    turn not stored yet wait until they come of age; a backlog goes on shortly."""
    if conv.held_from is not None or (
        expected is not None and (conv.tail_text or "").strip() != expected
    ):
        return HOLD_SECONDS + 1
    if conv.more:
        return BACKLOG_DELAY_SECONDS
    return None


def _done(result: dict[str, Any]) -> dict[str, Any] | None:
    """What a run that stopped early returns: what it completed, else None."""
    return result if result["windows"] or result["windows_skipped"] else None


# ═══ the watermark row ═══════════════════════════════════════════════════
#
# One ``ingested_items`` row per conversation (``source_name`` =
# :data:`_WATERMARK_SOURCE`, ``item_id`` = the session key). Its
# ``content_hash`` (a 64-character column) holds the state as
# ``through|failures|failed_end|claim|claimed_until``: the last
# ``chat_messages.id`` extracted, how many runs in a row could not extract the
# next window, that window's end while it keeps failing, and, while a run works
# on the conversation, its claim token and when the claim lapses (epoch
# seconds). Every change is a read-modify-write under ``SELECT … FOR UPDATE``,
# so two engine processes never hold one conversation and never extract the
# same window twice; a claim lapses after :data:`CLAIM_LEASE_SECONDS` if its run
# died. All of this is synchronous: callers run it off the event loop.


@dataclass(frozen=True)
class Watermark:
    """The claimed state of one conversation."""

    through: int
    failures: int
    token: str
    #: The end of the window that failed, while it keeps failing.
    failed_end: int | None = None


@dataclass
class _State:
    through: int = 0
    failures: int = 0
    failed_end: int | None = None
    claim: str | None = None
    claimed_until: int | None = None

    @classmethod
    def parse(cls, raw: str | None) -> _State:
        parts = (raw or "").split("|")
        if len(parts) != 5:
            return cls()

        def num(v: str) -> int | None:
            return int(v) if v.isdigit() else None

        return cls(
            num(parts[0]) or 0, num(parts[1]) or 0, num(parts[2]), parts[3] or None, num(parts[4])
        )

    def encode(self) -> str:
        fields = (self.through, self.failures, self.failed_end, self.claim, self.claimed_until)
        return "|".join("" if v is None else str(v) for v in fields)


def _mutate(
    session_key: str, tenant_id: str, change: Callable[[_State, int], _State | None]
) -> _State | None:
    """Apply ``change(state, now)`` to the conversation's row, locked; the new state.

    ``change`` returns ``None`` to leave the row as it is (and get ``None``).
    The row is created, at ``through = 0``, when the conversation has none.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ingested_items (tenant_id, source_name, item_id, content_hash, "
            "fact_ids, ingested_at) VALUES (%s, %s, %s, %s, '{}', NOW()) "
            "ON CONFLICT (tenant_id, source_name, item_id) DO NOTHING",
            (tenant_id, _WATERMARK_SOURCE, session_key, _State().encode()),
        )
        cur.execute(
            "SELECT content_hash, extract(epoch FROM now())::bigint FROM ingested_items "
            "WHERE tenant_id = %s AND source_name = %s AND item_id = %s FOR UPDATE",
            (tenant_id, _WATERMARK_SOURCE, session_key),
        )
        raw, now = cur.fetchone()
        new = change(_State.parse(raw), int(now))
        if new is not None:
            cur.execute(
                "UPDATE ingested_items SET content_hash = %s, ingested_at = NOW() "
                "WHERE tenant_id = %s AND source_name = %s AND item_id = %s",
                (new.encode(), tenant_id, _WATERMARK_SOURCE, session_key),
            )
        conn.commit()
    return new


def _claim(session_key: str, tenant_id: str) -> Watermark | None:
    """Claim the conversation for this run, or ``None`` if another run holds it."""
    token = uuid.uuid4().hex[:10]

    def take(state: _State, now: int) -> _State | None:
        if state.claim and (state.claimed_until or 0) > now:
            return None
        state.claim, state.claimed_until = token, now + CLAIM_LEASE_SECONDS
        return state

    state = _mutate(session_key, tenant_id, take)
    if state is None:
        return None
    return Watermark(state.through, state.failures, token, state.failed_end)


def _owned(token: str, change: Callable[[_State], None]) -> Callable[[_State, int], _State | None]:
    def apply(state: _State, _now: int) -> _State | None:
        if state.claim != token:
            return None  # the claim lapsed and another run took it: change nothing
        change(state)
        return state

    return apply


def _advance(
    session_key: str, tenant_id: str, token: str, through: int, fact_ids: list[int]
) -> None:
    """Move the watermark (and clear the failure count) under this run's claim."""

    def change(state: _State) -> None:
        state.through, state.failures, state.failed_end = through, 0, None

    del fact_ids  # the facts carry their own link to the turns
    _mutate(session_key, tenant_id, _owned(token, change))


def _fail(session_key: str, tenant_id: str, token: str, window_end: int) -> int:
    """Count one more run that could not extract the next window; the new count.

    The first failure pins the window's end (``failed_end``), so the turns that
    arrive later are not folded into the window that fails.
    """

    def change(state: _State) -> None:
        state.failures += 1
        if state.failed_end is None:
            state.failed_end = window_end

    state = _mutate(session_key, tenant_id, _owned(token, change))
    return state.failures if state is not None else 0


def _release(session_key: str, tenant_id: str, token: str) -> None:
    """Give the conversation back (only this run's own claim)."""

    def change(state: _State) -> None:
        state.claim, state.claimed_until = None, None

    _mutate(session_key, tenant_id, _owned(token, change))


def _record_skip(session_key: str, tenant_id: str, start: int, end: int, reason: str) -> None:
    """Record a window passed over unextracted, so it can be found and retried."""
    with get_connection() as conn:
        conn.cursor().execute(
            "INSERT INTO ingested_items (tenant_id, source_name, item_id, content_hash, "
            "fact_ids, ingested_at) VALUES (%s, %s, %s, %s, '{}', NOW()) "
            "ON CONFLICT (tenant_id, source_name, item_id) DO UPDATE "
            "SET content_hash = EXCLUDED.content_hash, ingested_at = NOW()",
            (tenant_id, _SKIPPED_SOURCE, f"{session_key}#{start}-{end}", reason[:64]),
        )
        conn.commit()
