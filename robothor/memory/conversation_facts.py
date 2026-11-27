"""The conversation extraction mode: what a person says about themselves is kept.

The default extraction (``facts.build_extraction_prompt``) is for documents and
notes: every fact must name a specific entity, and a fact that names none is
dropped. In a conversation most of what is worth remembering names no entity:
"I prefer dark roast", "I bought a road bike yesterday", "we're moving in June",
and the list of options the assistant offered. This mode, used only by
conversation ingest (``robothor.memory.conversation_ingest``), extracts from the
exchange as it happened:

* the window is the turns in order, user and assistant interleaved, each with
  its number, speaker and time (:func:`build_conversation_prompt`); turns
  extracted before are shown as read-only context;
* the model returns, per fact, the speaker and the turn numbers it came from
  (:data:`CONVERSATION_EXTRACTION_SCHEMA`); a first-person statement becomes a
  fact about "the user" (``about_user``), with no named entity needed;
* trust is decided here, not by the model (:func:`parse_conversation_response`):
  a fact is the user's (``speaker_role == "user"``, stored ``user_stated``) only
  when the model says so AND every turn it cites is a user turn. A fact citing
  an assistant turn, or no usable turn, is the assistant's (``agent_generated``).
  Assistant text never becomes a user-stated fact;
* each fact keeps the chat message ids it came from (``turn_refs``) and when it
  was said (``mentioned_at``), so it can be linked back to its source turns;
* a relative date is counted from the turn that says it (:func:`_dated`), never
  from the day the conversation is ingested.

One model call per window, with the strict schema; a valid answer with no facts
is final (it is not asked again).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from robothor.memory import generation
from robothor.memory.facts import (
    MENTION_TYPES,
    VALID_CATEGORIES,
    Extraction,
    _event_time,
    _mentions,
    _parse,
    _relations,
    passes_quality_filter,
)
from robothor.memory.relative_dates import relative_days

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)

#: What a conversation fact is, beside its category (kept in its metadata).
CONVERSATION_KINDS = [
    "preference",
    "possession",
    "plan",
    "state",
    "event",
    "recommendation",
    "other",
]

SPEAKERS = ["user", "assistant"]

_ROLE_LABELS = {"user": "User", "assistant": "Assistant"}

CONVERSATION_EXTRACTION_SCHEMA: dict[str, Any] = {
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
                    "kind",
                    "confidence",
                    "event_time",
                    "about_user",
                    "speaker",
                    "turns",
                    "mentions",
                    "relations",
                ],
                "properties": {
                    "fact_text": {"type": "string"},
                    "category": {"type": "string", "enum": VALID_CATEGORIES},
                    "kind": {"type": "string", "enum": CONVERSATION_KINDS},
                    "confidence": {"type": "number"},
                    "event_time": {"type": ["string", "null"]},
                    "about_user": {"type": "boolean"},
                    "speaker": {"type": "string", "enum": SPEAKERS},
                    "turns": {"type": "array", "items": {"type": "integer"}},
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


@dataclass(frozen=True)
class ConversationTurn:
    """One stored turn. ``ref`` is its ``chat_messages.id``; ``speaker`` labels a
    room turn with its author (a one-to-one turn is just User or Assistant)."""

    ref: int
    role: str
    text: str
    at: datetime | None = None
    speaker: str | None = None


def _stamp(at: datetime | None) -> str:
    if at is None:
        return "time unknown"
    return at.strftime("%a %Y-%m-%d %H:%M %Z").strip()


def _label(turn: ConversationTurn) -> str:
    base = _ROLE_LABELS.get(turn.role, turn.role.title())
    if turn.role == "user" and turn.speaker:
        return f"{base} ({turn.speaker})"
    return base


def render_turn(number: int, turn: ConversationTurn) -> str:
    """``[n] <time> <Speaker>: <text>`` — the line the model reads for one turn."""
    return f"[{number}] {_stamp(turn.at)} {_label(turn)}: {turn.text}"


def render_window(window: list[ConversationTurn], *, start: int = 1) -> str:
    return "\n".join(render_turn(start + i, t) for i, t in enumerate(window))


def build_conversation_prompt(
    window: list[ConversationTurn], *, context: list[ConversationTurn] | tuple[()] = ()
) -> str:
    """The prompt for one window of a conversation (see the module docstring).

    ``context`` turns come first, numbered from 1, inside a read-only block;
    the window's turns follow, numbered on from there. Dates are resolved
    against each turn's own time: no "today" appears anywhere in the prompt.
    """
    ctx = list(context)
    background = ""
    if ctx:
        background = f"""
Earlier turns, already processed (read-only: use them to understand the new
turns, never extract a fact that only they support):
<<<EARLIER
{render_window(ctx)}
EARLIER>>>
"""
    return f"""Extract what is worth remembering from the new turns of this conversation
between a user and an AI assistant. Each turn is numbered and shows when it was
said and who said it.

What to keep:
- What the user says about themselves: preferences and dislikes, what they own,
  bought, use or sold, their plans and intentions, their situation and any change
  in it (say what it changed from when they say it: "switched from X to Y"),
  their activities and events with when they happened, people and places in
  their life, and their goals and problems.
- What the assistant recommended, suggested, listed or concluded for the user:
  keep the items of a list in the fact, with how many there were and what
  distinguishes each, so the list can be recalled as it was given.
- Facts about named people, organisations, projects and things, as usual.

How to write each fact:
- One complete, self-contained sentence. A first-person statement by the user is
  a fact about "the user" ("The user prefers dark roast coffee."), with
  about_user true. It needs no other named entity. Write a relative date in the
  sentence as the absolute date it means ("on 2023-05-19", not "yesterday").
- speaker: who said it, "user" or "assistant". A suggestion, recommendation or
  claim made by the assistant is the assistant's, even when it is about the
  user; never present it as something the user said.
- turns: the numbers of the new turns the fact comes from.
- kind: preference, possession, plan, state, event, recommendation or other.
- event_time: when the thing happened or will happen, as an ISO date
  (YYYY-MM-DD) or date-time. Resolve relative dates ("yesterday", "last
  Tuesday", "in two weeks") against the timestamp of the turn that says them,
  never against any other date. It is when the thing happens, and
  not when it was said: null for a suggestion, a standing preference, or when
  the conversation does not say.
- mentions: every specific name the fact mentions, as written in the fact, with
  its type (person, project, organization, technology, location, event).
  "the user" is not a mention.
- relations: relationships the fact states between two of its mentions (source,
  a short verb phrase, target). Empty when none.

Skip greetings, thanks, filler, questions that state nothing, and general
knowledge that is not about this user or this conversation.
{background}
New turns:
{render_window(window, start=len(ctx) + 1)}"""


def _cited(item: dict[str, Any], window: list[ConversationTurn], offset: int) -> list[int] | None:
    """Window positions the fact cites, or None when it cites none of them.

    Numbers ``1..offset`` are context turns; ``offset+1..`` the window's.
    """
    raw = item.get("turns")
    if not isinstance(raw, list):
        return None
    out: list[int] = []
    for n in raw:
        if isinstance(n, bool) or not isinstance(n, int):
            continue
        pos = n - offset - 1
        if 0 <= pos < len(window) and pos not in out:
            out.append(pos)
    return sorted(out) or None


def _cites_context_only(item: dict[str, Any], offset: int) -> bool:
    raw = item.get("turns")
    if not isinstance(raw, list) or not raw or offset == 0:
        return False
    nums = [n for n in raw if isinstance(n, int) and not isinstance(n, bool)]
    return bool(nums) and all(1 <= n <= offset for n in nums)


def _dated(model_time: str | None, cited: list[ConversationTurn]) -> str | None:
    """The fact's event time, with a relative date counted from its own turn.

    When the cited turns name exactly one day (:func:`relative_days`, each
    counted from its own turn's time), that day is the event time: it fills an
    empty one and replaces the model's, which miscounts weekdays and spans and
    may count from today. A day named only as "today" fills an empty event time
    and nothing more ("today I booked a trip for next month" is not dated
    today). Turns naming two different days leave the model's answer alone.
    """
    every = {d for t in cited for d in relative_days(t.text, t.at)}
    if len(every) != 1:
        return model_time
    (day,) = every
    if model_time is None:
        return day.isoformat()
    exact = {d for t in cited for d in relative_days(t.text, t.at, include_today=False)}
    if exact == every:
        return day.isoformat()
    return model_time


def _redated(text: str, model_time: str | None, event_time: str | None) -> str:
    """The fact text with the model's date replaced by the corrected one, so the
    sentence and ``event_time`` never disagree."""
    if not model_time or not event_time or model_time[:10] == event_time[:10]:
        return text
    return text.replace(model_time[:10], event_time[:10])


def parse_conversation_response(
    raw: str,
    window: list[ConversationTurn],
    *,
    context: list[ConversationTurn] | tuple[()] = (),
) -> list[dict[str, Any]]:
    """The facts of one window's answer, each linked to its turns and speaker.

    Every fact carries the write path's keys (``fact_text``, ``category``,
    ``confidence``, ``mentions``, ``relations``, ``event_time``, ``entities``)
    and this mode's: ``speaker_role`` (``user`` only when the model says so and
    every cited turn is the user's, else ``assistant``), ``about_user``,
    ``kind``, ``turn_refs`` (chat message ids; the whole window when the fact
    cites no window turn) and ``mentioned_at``. A fact citing only context
    turns was extracted before and is dropped. The default quality filters
    apply, except that no named entity is required.
    """
    offset = len(context)
    out: list[dict[str, Any]] = []
    for item in _parse(raw) or []:
        text = item.get("fact_text")
        if not isinstance(text, str) or not text.strip():
            continue
        if _cites_context_only(item, offset):
            continue
        positions = _cited(item, window, offset)
        cited = [window[p] for p in positions] if positions is not None else list(window)
        claimed = item.get("speaker") if item.get("speaker") in SPEAKERS else "assistant"
        user_said_it = (
            positions is not None and claimed == "user" and all(t.role == "user" for t in cited)
        )
        category = str(item.get("category", "personal")).lower().strip()
        if category not in VALID_CATEGORIES:
            category = "personal"
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.8))))
        except (TypeError, ValueError):
            confidence = 0.8
        kind = item.get("kind") if item.get("kind") in CONVERSATION_KINDS else "other"
        mentions = _mentions(item)
        model_time = _event_time(item.get("event_time"))
        event_time = _dated(model_time, cited if positions is not None else [])
        last = max(cited, key=lambda t: t.ref)
        fact: dict[str, Any] = {
            "fact_text": _redated(text.strip(), model_time, event_time),
            "category": category,
            "entities": [m["surface_form"] for m in mentions],
            "confidence": confidence,
            "mentions": mentions,
            "relations": _relations(item),
            "event_time": event_time,
            "speaker_role": "user" if user_said_it else "assistant",
            "about_user": item.get("about_user") is True,
            "kind": kind,
            "turn_refs": sorted(t.ref for t in cited),
            "mentioned_at": last.at.isoformat() if last.at is not None else None,
        }
        if passes_quality_filter(fact, require_entities=False):
            out.append(fact)
    return out


async def extract_conversation_facts(
    window: list[ConversationTurn],
    *,
    context: list[ConversationTurn] | tuple[()] = (),
    timeout: float = 180.0,
    max_retries: int = 3,
) -> Extraction:
    """One window's facts: one model call, retried only when the answer is not
    the JSON asked for (or the call failed). ``error`` as in ``facts.Extraction``."""
    prompt = build_conversation_prompt(window, context=context)

    async def _run() -> Extraction:
        error = "empty"
        for attempt in range(max_retries):
            try:
                raw = await generation.generate(
                    prompt=prompt,
                    system="Extract facts from the conversation as JSON.",
                    max_tokens=generation.EXTRACTION_MAX_TOKENS,
                    format=CONVERSATION_EXTRACTION_SCHEMA,
                    think=False,
                )
            except Exception as exc:
                logger.warning("conversation extraction attempt %d failed: %s", attempt + 1, exc)
                error = type(exc).__name__
                continue
            if not raw or not raw.strip():
                error = "empty"
                continue
            if _parse(raw) is None:
                error = "unparseable"
                continue
            return Extraction(parse_conversation_response(raw, window, context=context))
        return Extraction([], error)

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except TimeoutError:
        logger.warning("conversation extraction hard timeout (%.0fs)", timeout)
        return Extraction([], "timeout")


__all__ = [
    "CONVERSATION_EXTRACTION_SCHEMA",
    "CONVERSATION_KINDS",
    "ConversationTurn",
    "build_conversation_prompt",
    "extract_conversation_facts",
    "parse_conversation_response",
    "render_turn",
    "render_window",
]
