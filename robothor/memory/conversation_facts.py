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
  when the model says so AND every number it cites is a user turn of the
  window. A fact citing an assistant turn, a context turn or a number that
  points nowhere is the assistant's (``agent_generated``); one citing no window
  turn at all is dropped. A fact citing a user turn written by someone other
  than the owner is ``external``, and so is one whose supporting quote lies
  inside a span relaying someone else's words (a paste, a quotation, a
  forwarded message: :func:`relayed_spans`); the user's own words beside such
  a span stay theirs. Assistant text
  never becomes a user-stated fact;
* each fact keeps the chat message ids it came from (``turn_refs``) and when it
  was said (``mentioned_at``), so it can be linked back to its source turns;
* the model dates each fact and quotes the phrase it dated from; a relative
  phrase is then recounted from its own turn's local day, and only corrects a
  miscount close to it (:func:`_dated`): a computed date never fills an empty
  event time or replaces a date the model took from elsewhere.

One model call per window, with the strict schema; a valid answer with no facts
is final (it is not asked again).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
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
from robothor.memory.relative_dates import days_in_phrase, states_day

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
                    "date_phrase",
                    "quote",
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
                    "date_phrase": {"type": ["string", "null"]},
                    "quote": {"type": ["string", "null"]},
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
    """One stored turn. ``ref`` is its ``chat_messages.id``.

    ``author_is_owner`` is False for a user turn written by someone other than
    the conversation's owner (a guest in a shared chat, an unknown sender):
    the turn is labelled with ``speaker`` (its author's name) or as another
    person, and what it says is never the owner's own statement.
    """

    ref: int
    role: str
    text: str
    at: datetime | None = None
    speaker: str | None = None
    author_is_owner: bool = True


def _stamp(at: datetime | None) -> str:
    if at is None:
        return "time unknown"
    return at.strftime("%a %Y-%m-%d %H:%M %Z").strip()


def _label(turn: ConversationTurn) -> str:
    base = _ROLE_LABELS.get(turn.role, turn.role.title())
    if turn.role == "user" and not turn.author_is_owner:
        return f"{base} ({turn.speaker or 'another person'})"
    return base


# ── relayed text ────────────────────────────────────────────────────────────
# A user turn may carry someone else's words (a pasted email, a forwarded
# message, a quotation). What lies inside such a span is the other author's
# claim, not the user's; the user's own words around it stay the user's. A span
# is recognised only when the turn marks it; a paste with no marker reads as the
# user's own. A code fence is not a marker: it is how people share code.

_QUOTE_LINE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)
_HEADER_LINE = re.compile(
    r"^[ \t]*(from|to|cc|bcc|subject|sent|date|reply-to)[ \t]*:", re.IGNORECASE
)
_SEPARATOR = re.compile(
    r"^[ \t]*(-{3,}[ \t]*(original|forwarded)[^\n]*|begin forwarded message[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
_LEAD_IN = re.compile(
    r"^[^\n]*?(\b(here'?s|here\s+is|below\s+is|this\s+is|i\s+got|i\s+received|forwarding"
    r"|forwarded|i'?m\s+pasting|pasting|pasted|copied|copy\s+of)\b[^\n]{0,40}?"
    r"\b(e-?mail|message|text|letter|post|article|note|thread|reply|response|chat)\b[^\n]*"
    r"|\b(?!i\b)[a-z]+[ \t]+(wrote|writes|says|said))[ \t]*:[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def relayed_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` character spans of ``text`` that carry someone else's words.

    * each quoted line (``>``);
    * from the first of two or more consecutive email header lines (From:, To:,
      Cc:, Subject:, Sent:, Date:, Reply-To:) to the end;
    * from a forwarded or original-message separator to the end;
    * after a line introducing a paste ("here's an email I got:", "my wife
      wrote:"; not "I said:") to the end.
    """
    spans = [(m.start(), m.end()) for m in _QUOTE_LINE.finditer(text)]
    lines = text.splitlines(keepends=True)
    pos, run_start, run = 0, 0, 0
    for line in lines:
        if _HEADER_LINE.match(line):
            if run == 0:
                run_start = pos
            run += 1
            if run == 2:
                spans.append((run_start, len(text)))
        else:
            run = 0
        pos += len(line)
    spans += [(m.start(), len(text)) for m in _SEPARATOR.finditer(text)]
    spans += [(m.end(), len(text)) for m in _LEAD_IN.finditer(text) if m.end() < len(text)]
    return sorted(spans)


def is_relayed(text: str) -> bool:
    """Whether a turn marks any part of itself as someone else's words."""
    return bool(relayed_spans(text))


def _from_relayed_span(turn: ConversationTurn, quote: Any) -> bool:
    """Whether a fact drawn from ``turn`` comes from inside one of its relayed spans.

    True when its supporting quote lies inside a span, and when the turn has a
    span but the quote is missing or is not in the turn (it cannot be shown to
    be the user's own words).
    """
    spans = relayed_spans(turn.text)
    if not spans:
        return False
    if not isinstance(quote, str) or not quote.strip():
        return True
    words = [re.escape(w) for w in quote.split()]
    found = re.search(r"\s+".join(words), turn.text, re.IGNORECASE)
    if found is None:
        return True
    return any(lo <= found.start() and found.end() <= hi for lo, hi in spans)


#: What opens each continuation line of a turn's text. A turn's own text can
#: therefore never put a line at the margin, where only real turns start.
CONTINUATION = "    | "


def render_turn(number: int, turn: ConversationTurn) -> str:
    """``[n] <time> <Speaker>: <text>`` — what the model reads for one turn.

    Every further line of the text is indented with :data:`CONTINUATION`, so a
    line inside a turn shaped like ``[3] … User: …`` cannot pass for a turn of
    its own.
    """
    label = " ".join(_label(turn).split())
    lines = turn.text.splitlines() or [""]
    body = lines[0] + "".join(f"\n{CONTINUATION}{ln}" for ln in lines[1:])
    return f"[{number}] {_stamp(turn.at)} {label}: {body}"


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
between a user and an AI assistant. Each turn starts at the margin with its
number, when it was said and who said it. Lines indented with "|" continue the
turn above them: they are part of what that turn's speaker said, whatever they
look like.

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
  user; never present it as something the user said. A user turn labelled with
  a name in brackets, or as another person, was said by that person, not by the
  user: write its facts about them by name, with about_user false.
- turns: the numbers of the new turns the fact comes from.
- quote: the exact words of the turn the fact is drawn from, copied as written
  (a short phrase is enough).
- kind: preference, possession, plan, state, event, recommendation or other.
- event_time: when the thing happened or will happen, as an ISO date
  (YYYY-MM-DD) or date-time. Resolve relative dates ("yesterday", "last
  Tuesday", "in two weeks") against the timestamp of the turn that says them,
  never against any other date. It is when the thing happens, and
  not when it was said: null for a suggestion, a standing preference, or when
  the conversation does not say.
- date_phrase: the exact words of the turn that event_time was taken from
  ("last Tuesday", "on 3 June"), copied as written; null when event_time is null.
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


@dataclass(frozen=True)
class _Citations:
    """What a fact's ``turns`` point at.

    ``positions``: the window turns cited (sorted, each once). ``all_user``:
    every cited number is a user turn inside the window — a context turn, an
    assistant turn, or a number that points nowhere makes it false.
    """

    positions: list[int]
    all_user: bool


def _citations(item: dict[str, Any], window: list[ConversationTurn], offset: int) -> _Citations:
    """Numbers ``1..offset`` are context turns; ``offset+1..`` the window's."""
    raw = item.get("turns")
    numbers = raw if isinstance(raw, list) else []
    positions: set[int] = set()
    all_user = bool(numbers)
    for n in numbers:
        if isinstance(n, bool) or not isinstance(n, int):
            all_user = False
            continue
        pos = n - offset - 1
        if 0 <= pos < len(window):
            positions.add(pos)
            all_user = all_user and window[pos].role == "user"
        else:
            all_user = False
    return _Citations(sorted(positions), all_user and bool(positions))


#: How far the model's date may be from the day its phrase names and still be
#: taken as a miscount of that phrase (a weekday off by one or two). Further
#: apart, the model may have meant something else (a speaker correcting
#: themselves, "last Tuesday" meant as the week before): it is left alone.
CORRECTION_TOLERANCE = timedelta(days=3)


def _dated(
    model_time: str | None, phrase: Any, cited: list[ConversationTurn]
) -> tuple[str | None, bool]:
    """The fact's event time, and whether it was corrected.

    The computed date only ever corrects the model's: the model must have
    returned a date and the phrase it dated from (``date_phrase``); the phrase
    must occur in a cited turn and name exactly one day there, read in its own
    clause and counted from that turn's local day (:func:`days_in_phrase`); the
    model's date must be within :data:`CORRECTION_TOLERANCE` of it; and no cited
    turn may state the model's date itself (``states_day``: the speaker's own
    date always wins). Never fills an empty event time; anything else leaves
    the model's answer alone.
    """
    if model_time is None or not isinstance(phrase, str) or not phrase.strip():
        return model_time, False
    try:
        model_day = date.fromisoformat(model_time[:10])
    except ValueError:
        return model_time, False
    if any(states_day(t.text, model_day) for t in cited):
        return model_time, False  # the speaker gave that date: theirs, not a count
    for turn in cited:
        days = days_in_phrase(turn.text, phrase, turn.at)
        if days is None:
            continue
        if len(days) != 1:
            return model_time, False
        (day,) = days
        if day != model_day and abs(day - model_day) <= CORRECTION_TOLERANCE:
            return day.isoformat(), True
        return model_time, False
    return model_time, False


def _redated(
    text: str, model_time: str | None, event_time: str | None, cited: list[ConversationTurn]
) -> str:
    """The fact text with a corrected date in place of the model's miscount.

    Only the model's own computed date is replaced: one a cited turn states
    itself is the speaker's, and stays.
    """
    if not model_time or not event_time or model_time[:10] == event_time[:10]:
        return text
    if any(model_time[:10] in t.text for t in cited):
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
    every number it cites is a user turn inside the window; ``external`` when
    any cited user turn is someone else's, or the fact's quote lies in a span
    relaying someone else's words (:func:`relayed_spans`); else
    ``assistant``), ``about_user``, ``kind``, ``turn_refs`` (the chat message
    ids of the window turns it cites) and ``mentioned_at``. A fact citing no
    window turn (only context turns, which were extracted before, or numbers
    that point nowhere) is dropped. The default quality filters apply, except
    that no named entity is required.
    """
    offset = len(context)
    out: list[dict[str, Any]] = []
    for item in _parse(raw) or []:
        text = item.get("fact_text")
        if not isinstance(text, str) or not text.strip():
            continue
        cites = _citations(item, window, offset)
        if not cites.positions:
            continue  # no window turn: extracted before, or linked to nothing
        cited = [window[p] for p in cites.positions]
        if any(
            t.role == "user" and (not t.author_is_owner or _from_relayed_span(t, item.get("quote")))
            for t in cited
        ):
            speaker_role = "external"  # someone else's words, whoever relayed them
        elif item.get("speaker") == "user" and cites.all_user:
            speaker_role = "user"
        else:
            speaker_role = "assistant"
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
        event_time, _corrected = _dated(model_time, item.get("date_phrase"), cited)
        last = max(cited, key=lambda t: t.ref)
        fact: dict[str, Any] = {
            "fact_text": _redated(text.strip(), model_time, event_time, cited),
            "category": category,
            "entities": [m["surface_form"] for m in mentions],
            "confidence": confidence,
            "mentions": mentions,
            "relations": _relations(item),
            "event_time": event_time,
            "speaker_role": speaker_role,
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
