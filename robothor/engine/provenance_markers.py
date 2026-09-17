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
import json
import re

from robothor.engine.verdict_shapes import item_id_spans

__all__ = [
    "MAX_RESULT_CHARS",
    "decoded_result",
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
#:
#: The escaped-newline alternative is not decoration. A tool result reaches
#: ``session.messages`` through ``json.dumps`` (``session.py``), so in a live
#: run every newline in the payload is the two characters backslash-n, a ``^``
#: anchor matches at offset 0 and nowhere else, and the "key opens a line" rule
#: — the first and most important rule in this module — was dead on exactly the
#: data it was written for (hostile review, Critical 1). :func:`decoded_result`
#: undoes the escaping wherever the payload still parses as JSON; this
#: alternative covers what is left, an offloaded or wrapped result that does
#: not.
#:
#: ``category`` and ``source`` are deliberately NOT keys. They are topical
#: fields: measured against ten realistic non-provenance values they turned a
#: sales lead from a demo request, a ticket about a "Sandbox API" product and a
#: customer on a demo plan into markers.
_MARKER_FIELD = re.compile(
    r"(?:^|\\n|[|;,{])[ \t]*[\"']?"
    r"(classification|origin|sender[- _]type|message[- _]type|content[- _]type|"
    r"environment|env|generated[- _]by|produced[- _]by|"
    r"validation(?:[- _]cycle)?|routing[- _]metadata|x-[a-z0-9-]{1,30})"
    r"[\"']?[ \t]*[:=][ \t]*[\"']?"
    # A backslash ends a value. In a stored result the next field begins after
    # an escape sequence, and a greedy value swallowed it: `Origin:` ate the
    # 80 characters after it, `Validation cycle:` never matched, and the one
    # field carrying the marker token was invisible even once the field
    # position was right.
    r"([^\n|;,\"'}\\]{1,80})",
    re.IGNORECASE | re.MULTILINE,
)

#: Regions of a result whose text is CONTENT, not metadata: a fenced block —
#: the customer who pastes the YAML they deployed is showing you their config,
#: not declaring what their ticket is — and a quoted line, which is somebody
#: else's header inside a forward. Both are blanked before the scan, padded to
#: the same length so every item offset still means what it meant.
_CONTENT_REGION = re.compile(r"```.*?```|~~~.*?~~~|^[ \t]*>[^\n]*$", re.DOTALL | re.MULTILINE)

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
        # `staging` and `canary` left the list for the same reason: a staging
        # outage blocking the release train, and a failing canary rollout, are
        # real incidents about a real environment.
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


#: How deep and how wide a decoded payload is walked. A tool result is a
#: message list, not a document tree.
_MAX_DEPTH = 6
_MAX_ITEMS = 500

#: What ``session.py`` wraps an external tool's result in before storing it.
_UNTRUSTED_WRAPPER = re.compile(
    r"\A<untrusted_content[^>]*>\n?|\n?</untrusted_content>\Z", re.DOTALL
)


def _flatten(payload: object, depth: int = 0) -> str:
    """A decoded payload as text a field scan can read.

    A dict becomes ``key: value`` lines — which is what a JSON tool result's
    own keys ARE — and a string leaf comes back with its real newlines, so a
    footer inside a message body opens lines again.
    """
    if depth > _MAX_DEPTH:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, bool) or payload is None or isinstance(payload, (int, float)):
        return str(payload)
    if isinstance(payload, list):
        return "\n".join(_flatten(item, depth + 1) for item in payload[:_MAX_ITEMS])
    if isinstance(payload, dict):
        return "\n".join(
            f"{key}: {_flatten(value, depth + 1)}"
            for key, value in list(payload.items())[:_MAX_ITEMS]
        )
    return ""


def decoded_result(content: str) -> str:
    """One stored tool result, as the tool actually returned it.

    ``session.py`` stores every tool result as ``json.dumps(tool_output)``, and
    wraps an external tool's in ``<untrusted_content>``. Scanning what is
    stored means scanning a single line with every newline escaped; this undoes
    both. A payload that no longer parses — an offloaded result, a truncation
    marker — is returned unchanged, and the escaped-newline alternative in
    :data:`_MARKER_FIELD` is what reads it.
    """
    stripped = _UNTRUSTED_WRAPPER.sub("", content).strip()
    try:
        return _flatten(json.loads(stripped))
    except (ValueError, TypeError, RecursionError):
        return content


def _content_blanked(text: str) -> str:
    """The same text with fenced and quoted regions replaced by spaces."""
    return _CONTENT_REGION.sub(lambda match: " " * (match.end() - match.start()), text)


def markers_by_item(results_text: str | None) -> dict[str, str]:
    """``item id -> "key: value"`` for every item whose metadata says it is not real.

    One marker per item — the first one found, which in a footer is the one
    the sender put first. The binding rule is positional and structural: a
    field belongs to the last item identifier introduced before it, within
    :data:`MARKER_REACH` characters. That is what a listing of items looks
    like, and it is what keeps one item's footer off the item above it.

    The text is decoded first, then fenced and quoted regions are blanked, so
    what is scanned is what the tool returned and only the parts of it that are
    the item's own metadata.
    """
    text = _content_blanked(decoded_result((results_text or "")[: 2 * MAX_RESULT_CHARS]))[
        :MAX_RESULT_CHARS
    ]
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
        chunk = decoded_result(content[:MAX_PER_RESULT_CHARS])
        parts.append(chunk)
        total += len(chunk)
        if total >= MAX_RESULT_CHARS:
            break
    return "\n".join(parts)[:MAX_RESULT_CHARS]
