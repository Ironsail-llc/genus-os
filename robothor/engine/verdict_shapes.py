"""What a verdict looks like on the page, and what a retraction of one looks like.

Extracted from ``verdict_commitment`` so that module can stay the LADDER — the
task gate, the re-ask, the guardrail row — while the reading of a document
lives here. The split is the one the ratchet asks for: this file grows when a
new document shape is recognised, and the ladder does not grow at all. Two of
its neighbours came out of it on the same rule: ``verdict_sections`` for how
far a verdict reaches from the heading that assigns it, ``override_reasons``
for whether an override names what outranks a marker. Each is one question with
its own vocabulary; what is left here is the vocabulary itself.

Everything here is deliberately structural. A verdict is read only from a
LABEL position (a heading, a bolded lead, a ``Severity:`` field) because prose
that happens to contain "moderate" is discussion, not a decision. A retraction
is read only from a conditional that questions WHAT THE ITEM IS, because a
report that reaches a verdict and adds a note about follow-up has decided.

The distinction this file exists to draw, measured three times on the same
deliverable: *"Treated as a real incident given the severity of the reported
impact. If this is a test artefact, please confirm with the owning team."* is a
verdict followed by its own withdrawal. The reader is left holding exactly the
decision the report was written to contain. *"Resolved; monitor for
recurrence"* is a verdict with a genuine caveat about the future, and it is
none of this module's business.
"""

from __future__ import annotations

import re

from robothor.engine.override_reasons import names_a_reason

__all__ = [
    "MAX_SCAN_CHARS",
    "VERDICTS",
    "hands_the_verdict_back",
    "hedges_the_verdict",
    "item_id_spans",
    "item_ids",
    "overrides_a_marker",
    "verdicts_in",
]

#: How much of a task or a deliverable is scanned. Bounded for the same reason
#: `deliverable_extract` bounds its own scan: a 3 MB file must cost a bounded
#: amount of work, not an unbounded one.
MAX_SCAN_CHARS = 64 * 1024

#: What an enumerated item looks like. Three shapes, all of them explicit
#: identifiers rather than anything inferred: `msg_2209`, `#12`, `TASK-4`.
#:
#: The ticket-key shape refuses a match that continues a longer code. A
#: reference number in a marker footer — `Ref: Q1-2026-RT-003` — ends in
#: something that reads exactly like `RT-003`, and the measured runs each
#: produced a phantom finding against that non-existent item alongside the real
#: one. A ticket key is a whole token, not the tail of one.
_ITEM_ID = re.compile(
    r"\b[a-z][a-z0-9]{1,12}_\d{2,}\b|\B#\d{1,5}\b|(?<![-/])\b[A-Z]{2,6}-\d{1,6}\b"
)

#: The verdict vocabulary. A closed list, because an open one would read a
#: paragraph's adjectives as verdicts. Each entry is a label a triage
#: deliverable puts at the head of a section.
#:
#: Every alternative is fenced by ``(?<![-\w])`` / ``(?![-\w])`` rather than by
#: ``\b``, because a hyphen is a word boundary and a compound is one word.
#: ``## High-level findings`` read as a *high* section and filed every item
#: under it a second time (review, round 3); ``non-critical``, ``lower-priority``
#: and ``high-touch`` are the same mistake waiting.
VERDICTS: dict[str, re.Pattern[str]] = {
    "critical": re.compile(
        r"(?<![-\w])(?:critical|p0|sev\s*0|sev\s*1|highest)(?![-\w])", re.IGNORECASE
    ),
    # `high[-\s]priority`, not `high\s+priority`: the fence is about compounds
    # that mean something else, and `High-priority` is the same label spelled
    # with a dash. It cannot re-open `High-level`, because the optional group
    # only matches when the word after the hyphen is `priority`.
    "high": re.compile(r"(?<![-\w])(?:high(?:[-\s]priority)?|p1|urgent)(?![-\w])", re.IGNORECASE),
    "medium": re.compile(
        r"(?<![-\w])(?:medium(?:[-\s]priority)?|moderate|p2)(?![-\w])", re.IGNORECASE
    ),
    "low": re.compile(r"(?<![-\w])(?:low(?:[-\s]priority)?|p3|p4|minor)(?![-\w])", re.IGNORECASE),
    # Every alternative here is a PHRASE, not a word. The bare word `test`
    # used to be one, so `**Priority: High** — this is a test-infrastructure
    # item` read as two verdicts, High and no-action (hostile review I7). A
    # vocabulary that fires on an ordinary English word inside a label is a
    # vocabulary that reports noise into the one table this flag's promotion
    # depends on.
    "no-action": re.compile(
        r"(?<![-\w])(?:no\s+action(?:\s+required)?|not\s+escalated|false\s+positive|"
        r"routing\s+test|test\s+message|automated\s+test|"
        r"dismissed|duplicate|drill)(?![-\w])",
        re.IGNORECASE,
    ),
}

#: Where a verdict is ASSIGNED rather than merely mentioned: a heading, a
#: bolded lead, or a named field. Each alternative is anchored and bounded, so
#: the whole thing stays linear on a hostile document.
_LABEL = re.compile(
    r"^#{1,6}[ \t]*([^\n]{0,200})$"
    r"|^[ \t]*(?:[-*+][ \t]+)?\*\*([^*\n]{0,80})\*\*"
    r"|^[ \t]*\|?[ \t]*(?:severity|priority|verdict|status|classification|disposition|action)"
    r"[ \t]*[:=|][ \t]*([^\n|]{0,60})",
    re.IGNORECASE | re.MULTILINE,
)

#: The decision handed back to the reader. Narrow and literal: this is the
#: sentence the measured run appended, and variants of it.
_HANDBACK = re.compile(
    r"\b(?:please|you\s+(?:should|may\s+wish\s+to)|the\s+(?:reader|operator|team)\s+should)\s+"
    r"(?:verify|confirm|decide|determine|check|establish)\s+(?:whether|if)\b"
    r"|\bit\s+is\s+(?:unclear|ambiguous|uncertain)\s+whether\b"
    r"|\b(?:someone|a\s+human)\s+(?:should|must)\s+decide\b",
    re.IGNORECASE,
)

#: …and the hand-back has to be ABOUT THE VERDICT. "Please confirm whether the
#: three remaining endpoints are in scope" is a question about the work, asked
#: alongside a verdict that was reached; the first cut read it as a refusal to
#: decide (hostile review I7). What follows the "whether" has to be the
#: classification itself: a severity word, an escalation, or whether the thing
#: is real at all.
_ABOUT_THE_VERDICT = re.compile(
    r"\b(?:escalat\w*|severit\w*|priorit\w*|incident|genuine|legitimate|real|"
    r"critical|urgent|p0|p1|false\s+positive|test|drill|routing)\b",
    re.IGNORECASE,
)

#: How far after the hand-back phrase to look for what it is about. One
#: sentence: beyond that the words belong to a different claim.
_HANDBACK_SCOPE = 200

#: A verdict taken back by a condition. Every alternative is a hinge the
#: sentence turns on, not a word that happens to appear: the conditional forms
#: (``if this is``, ``unless``, ``assuming``), the explicit wait
#: (``pending confirmation``), and the modal that names an alternative identity
#: (``may be a …``). What none of them is, on its own, is enough — see
#: :data:`_WHAT_IT_IS`.
_HEDGE = re.compile(
    r"\b(?:if|should)\s+(?:this|it|that|the\s+\w{1,20})\s+"
    r"(?:is|was|were|be|turns?\s+out|proves?\s+to\s+be)\b"
    r"|\bunless\s+(?:this|it|that|the\s+\w{1,20})\b"
    r"|\bassuming\s+(?:this|it|that|the\s+\w{1,20})\b"
    r"|\bpending\s+(?:confirmation|verification|validation)\s+(?:that|whether)\b"
    r"|\b(?:may|might|could)\s+(?:well\s+)?be\s+(?:an?\s+)?",
    re.IGNORECASE,
)

#: …and what the condition has to be ABOUT: the identity of the item, not the
#: work around it. This is the whole false-positive defence. "Unless otherwise
#: specified, all timestamps are UTC" and "pending confirmation of the account
#: tier" are conditions about the report; "if this is a routing drill" is a
#: condition about whether the verdict above it holds.
_WHAT_IT_IS = re.compile(
    r"\b(?:test|tests|testing|drill|exercise|rehearsal|simulation|simulated|"
    r"synthetic|sandbox|staging|automated|automation|duplicate|artefact|artifact|"
    r"false\s+(?:alarm|positive)|genuine|legitimate|real|live|actual)\b",
    re.IGNORECASE,
)

#: How far after the hedge hinge to look for what it questions. Shorter than
#: the hand-back window: a conditional binds tighter than a request does.
_HEDGE_SCOPE = 160

#: An override stated outright. Paired with :data:`_PROVENANCE_WORD` below,
#: because "regardless" on its own is an adverb and this has to be a claim
#: about the marker.
_OVERRIDE = re.compile(
    r"\b(?:overrid\w+|disregard\w*|discount\w*|set\s+aside|notwithstanding|"
    r"despite|even\s+though|regardless)\b",
    re.IGNORECASE,
)

#: What the override has to be about.
_PROVENANCE_WORD = re.compile(
    r"\b(?:metadata|marker|footer|header|provenance|classification|origin|"
    r"routing\s+test|self-declared)\b",
    re.IGNORECASE,
)


def item_id_spans(chunk: str) -> list[tuple[int, str]]:
    """``(offset, identifier)`` for every explicit identifier, in order.

    The offsets are what binds a metadata field to the item it belongs to:
    a field belongs to the last identifier introduced before it.
    """
    return [(match.start(), match.group(0)) for match in _ITEM_ID.finditer(chunk)]


def item_ids(chunk: str) -> set[str]:
    """Every explicit identifier in this text."""
    return {item for _offset, item in item_id_spans(chunk)}


def verdicts_in(chunk: str) -> set[str]:
    """The verdicts this block ASSIGNS, read only from label positions.

    Scanning the whole block was the first cut and it was wrong in both
    directions, caught by its own tests: "Confidence is moderate" in a sentence
    read as a *medium* verdict, and the measured report's own hedge read as a
    second verdict rather than as a hand-back. A verdict is something a triage
    document puts in a heading, a bolded lead or a ``Severity:`` field —
    prose that happens to contain the word is discussion, not a decision.
    """
    labels = " | ".join(part for match in _LABEL.finditer(chunk) for part in match.groups() if part)
    return {name for name, pattern in VERDICTS.items() if pattern.search(labels)}


def hands_the_verdict_back(chunk: str) -> bool:
    """True when this block asks the reader to make the CLASSIFICATION.

    Both halves required: the hand-back phrasing, and — within the sentence
    that follows it — something that is actually the verdict. A report that
    reaches a verdict and separately asks a question about the work has not
    handed its decision to anybody.
    """
    for match in _HANDBACK.finditer(chunk):
        window = chunk[match.end() : match.end() + _HANDBACK_SCOPE]
        if _ABOUT_THE_VERDICT.search(window):
            return True
    return False


def hedges_the_verdict(chunk: str) -> str:
    """The retraction this block appends to its own verdict, or ``""``.

    Same two-halves shape as :func:`hands_the_verdict_back`, and for the same
    reason: the hinge word alone is far too common in an honest report. The
    return value is the quoted text, because the whole point of the enforce
    rung is that the model is shown the sentence it has to replace.
    """
    for match in _HEDGE.finditer(chunk):
        window = chunk[match.end() : match.end() + _HEDGE_SCOPE]
        if _WHAT_IT_IS.search(window):
            quoted = " ".join(chunk[match.start() : match.end() + _HEDGE_SCOPE].split())
            return quoted[:120]
    return ""


def overrides_a_marker(chunk: str) -> bool:
    """True when this block overrides a marker AND names what outranks it.

    Deliberately NOT satisfied by mentioning the marker. All three measured
    runs quoted the footer at length and then asked the reader what to do with
    it; quoting is not deciding. An override is a claim — "I am overriding that
    marker, because …" — and the caller additionally requires the block not to
    hedge, so "escalated regardless, but please confirm whether…" stays a
    finding.

    Nor is it satisfied by the claim alone. The fourth measured run wrote
    *"the metadata was disregarded for routing"* and named nothing whatever,
    and that sentence — the one rule 20 exists to forbid — was what exempted
    it. What counts as naming a reason, and where it may be said, is
    ``override_reasons.names_a_reason``.
    """
    for match in _OVERRIDE.finditer(chunk):
        window = chunk[max(0, match.start() - 200) : match.end() + 200]
        if _PROVENANCE_WORD.search(window) and names_a_reason(chunk, match.end()):
            return True
    return False
