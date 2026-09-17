"""An item that says what it is, in its own metadata.

MEASURED 2026-09-17, three runs of the same task. One inbound message carried a
footer of routing-test fields — a `Classification:` naming a test cycle, an
`Origin:` naming an automation account, a line saying which validation cycle it
belonged to. All three runs READ the footer, quoted it in the report, and filed
the message as the top escalation of the day anyway, with the decision about
what it actually was handed to the reader underneath.

An item's own metadata is evidence about the item. It is not proof — a real
outage can be reported by an automated monitor, and a marker can be stale or
wrong — but a verdict that contradicts it silently is a verdict that ignored
the cheapest evidence available. This module finds those contradictions.

The whole design is about NOT firing on the word "test":

* the marker has to be in a FIELD — ``key: value`` at the start of a line, or
  after a ``|``, or as a JSON key. A customer writing *"THIS IS NOT A TEST"* in
  the body of their message is prose and is invisible here;
* the KEY has to be one that states provenance or classification. ``subject:``,
  ``from:`` and the body are not on the list, so a product whose name contains
  "Test" cannot become a marker by being the subject of a ticket;
* the VALUE has to carry a token that means non-production, and a token
  followed by a capitalised word is read as part of a name rather than as a
  marker — ``category: Test Kitchen`` is a product, ``classification:
  routing-test`` is a marker;
* ``automated``, ``internal`` and their relatives are WEAK. A real incident is
  usually reported by an automated internal monitor, so they qualify a marker
  and never make one.

And a contradiction needs a verdict to contradict: the deliverable has to have
classified the item as live work. An item the report files as no-action has
honoured its marker, and nothing fires.
"""

from __future__ import annotations

import bisect
import re

from robothor.engine.verdict_shapes import item_id_spans

__all__ = [
    "MAX_RESULT_CHARS",
    "markers_by_item",
    "tool_result_text",
]

#: How much of the run's tool output is scanned, and how much of one result.
#: Bounded for the same reason every other scan in this cluster is: a run that
#: fetched a 40 MB page must cost a bounded amount of work.
MAX_RESULT_CHARS = 256 * 1024
MAX_PER_RESULT_CHARS = 64 * 1024

#: How far back from a field the owning identifier may be. One item's metadata
#: block, generously: beyond this the nearest identifier is somebody else's.
MARKER_REACH = 4000

#: A metadata FIELD, in field position. The key may open a line, follow a
#: pipe or a comma in a footer, or be a JSON key — what it may not be is a word
#: in the middle of a sentence, which is the entire free-prose defence.
_MARKER_FIELD = re.compile(
    r"(?:^|[|;,{])[ \t]*[\"']?"
    r"(classification|origin|source|sender[- _]type|message[- _]type|content[- _]type|"
    r"category|environment|env|generated[- _]by|produced[- _]by|"
    r"validation(?:[- _]cycle)?|routing[- _]metadata|x-[a-z0-9-]{1,30})"
    r"[\"']?[ \t]*[:=][ \t]*[\"']?"
    r"([^\n|;,\"'}]{1,80})",
    re.IGNORECASE | re.MULTILINE,
)

#: Tokens that mean "this item is not a report of a real-world event". A closed
#: list, and every entry is a word whose ordinary use inside a provenance field
#: is to say exactly that.
_STRONG_TOKENS = frozenset(
    {
        "test",
        "tests",
        "testing",
        "drill",
        "rehearsal",
        "simulation",
        "simulated",
        "synthetic",
        "sandbox",
        "staging",
        "canary",
        "fixture",
        "dummy",
        "placeholder",
        "demo",
        "mock",
        "noop",
        "dryrun",
        # DELIBERATELY ABSENT, and the list is as load-bearing as the one
        # above: `automated`, `automation`, `bot`, `internal`, `noreply`,
        # `system`. A genuine outage is reported by an automated internal
        # monitor. If any of those were a contradiction on its own, this
        # control would fire on most of the alerting in a working fleet.
    }
)

#: Multi-word markers, matched against the value with its separators
#: normalised, so ``do not escalate`` and ``do-not-escalate`` are one thing.
_STRONG_PHRASES = (
    "do-not-escalate",
    "not-for-escalation",
    "dry-run",
    "non-production",
    "false-positive",
    "not-real",
)


def _is_a_name(value: str, token: str) -> bool:
    """True when this token is part of a proper noun rather than a marker.

    ``category: Test Kitchen`` names a product; ``classification: routing-test``
    states what the item is. The discriminator is a capitalised word directly
    after the token — a marker code continues with a hyphen, a digit or
    nothing at all.
    """
    return bool(re.search(rf"\b(?i:{re.escape(token)})\s+[A-Z][a-z]", value))


def _marker_in(value: str) -> str:
    """The non-production token this field's value carries, or ``""``."""
    normalised = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    for phrase in _STRONG_PHRASES:
        if phrase in normalised:
            return phrase
    for token in normalised.split("-"):
        if token in _STRONG_TOKENS and not _is_a_name(value, token):
            return token
    return ""


def markers_by_item(results_text: str | None) -> dict[str, str]:
    """``item id -> "key: value"`` for every item whose metadata says it is not real.

    One marker per item — the first one found, which in a footer is the one
    the sender put first. The binding rule is positional and structural: a
    field belongs to the last item identifier introduced before it, within
    :data:`MARKER_REACH` characters. That is what a listing of items looks
    like, and it is what keeps one item's footer off the item above it.
    """
    text = (results_text or "")[:MAX_RESULT_CHARS]
    if not text:
        return {}
    spans = item_id_spans(text)
    if not spans:
        return {}
    offsets = [offset for offset, _item in spans]

    markers: dict[str, str] = {}
    for field in _MARKER_FIELD.finditer(text):
        key, value = field.group(1), field.group(2).strip()
        if not _marker_in(value):
            continue
        index = bisect.bisect_right(offsets, field.start(1)) - 1
        if index < 0:
            continue
        offset, item = spans[index]
        if field.start(1) - offset > MARKER_REACH:
            continue
        markers.setdefault(item, f"{key.lower()}: {value}"[:120])
    return markers


def tool_result_text(session: object) -> str:
    """Everything this run's tools returned, bounded, newest last.

    Best effort by construction: compaction evicts old tool results, so a long
    run's early metadata may simply not be in the transcript any more. A marker
    that is gone is a marker this control cannot see, and that is a quiet
    false negative rather than a wrong finding.
    """
    messages = getattr(session, "messages", None)
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    total = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content:
            continue
        chunk = content[:MAX_PER_RESULT_CHARS]
        parts.append(chunk)
        total += len(chunk)
        if total >= MAX_RESULT_CHARS:
            break
    return "\n".join(parts)[:MAX_RESULT_CHARS]
