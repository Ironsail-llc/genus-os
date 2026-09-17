"""Whether an override NAMES what outranks the item's own metadata.

Extracted from ``verdict_shapes`` beside ``verdict_sections``, and for the same
reason: this is one question with its own vocabulary, and the module it grew in
is the one that grows with every new document shape.

Fleet rule 20 asks a verdict that ignores a provenance marker to say what
overrides it. MEASURED 2026-09-17: the report said *"the metadata was
disregarded"* — the assertion with the reason left out, which is the exact
sentence the rule exists to forbid — and that sentence bought the exemption,
because the detector checked for the claim and never for the reason.

A reason is a CLAIM ABOUT EVIDENCE, and the two halves are the whole design:

* a source that could be gone and looked at — a message, a ticket, a channel, a
  monitor, a customer, a handle, a timestamp, a link;
* and that source DOING something: confirming, showing, paging, opening a
  ticket. Without it every generic noun is a reason ("because the report is
  about a genuine customer impact", "since the system requires escalation"),
  which is the first cut's own loophole, found in review. For the same reason a
  quotation counts only through its attribution — *because it "seemed wrong"*
  quotes nothing but the writer.

Where it may be said is as much of the rule as what it says:

* AFTER the override phrase. A reason found before it is usually the marker
  being described, which is how "contained trailing test-harness metadata … was
  disregarded" would talk its way out of the finding it is;
* in that sentence or the next one — an override stated in one sentence and
  explained in the next, or in the bullet under it, is the ordinary way to
  write one;
* and not past a blank line, where the words belong to a different claim.
"""

from __future__ import annotations

import re

__all__ = ["REASON_SCOPE", "names_a_reason"]

#: How far past the override phrase its reason may be.
REASON_SCOPE = 320

#: How far apart the source and what it did may be and still be one claim.
PAIR_REACH = 90

#: A sentence ends at a full stop FOLLOWED BY SPACE — `example.com` and `14:02`
#: are not sentence ends.
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")

#: A blank line ends the reach outright.
_PARAGRAPH = re.compile(r"\n[ \t]*\n")

#: Something a reader could go and look at. Deliberately NOT here: `report`,
#: `record`, `system`, `team`, `user` and their relatives. Each of those names
#: the writer's own side of the page rather than a source outside it, and each
#: of them turned a sentence that named nothing into an exemption.
_SOURCE = re.compile(
    r"\b(?:message|messages|ticket|tickets|email|emails|thread|threads|channel|channels|"
    r"call|calls|log|logs|dashboard|dashboards|alert|alerts|monitor|monitors|monitoring|"
    r"telemetry|metric|metrics|status\s+page|feed|feeds|screenshot|screenshots|"
    r"customer|customers|client|clients|sender|caller|on-call|engineer|responder|"
    r"invoice|contract|transcript|recording)\b"
    r"|@[A-Za-z0-9][A-Za-z0-9._-]{1,30}"
    r"|[A-Za-z0-9._%+-]{1,40}@[A-Za-z0-9.-]{1,40}\.[A-Za-z]{2,}"
    r"|\b\d{1,2}:\d{2}\b|\b\d{4}-\d{2}-\d{2}\b|https?://\S{3,}"
    r"|\b[a-z][a-z0-9]{1,12}_\d{2,}\b|(?<![-/])\b[A-Z]{2,6}-\d{1,6}\b",
    re.IGNORECASE,
)

#: …and what that source has to be doing. A source that merely appears in the
#: sentence is a noun; a source that confirmed, showed, paged or opened
#: something is evidence.
_CORROBORATION = re.compile(
    r"\b(?:confirm\w*|corroborat\w*|verif\w+|validat\w+|attest\w*|witness\w*|observ\w+|"
    r"reported|reports|shows?|showed|appears?|appeared|opened|raised|paged|phoned|"
    r"called|said|says|wrote|logged|recorded|matched|matches|traced|reproduced)\b",
    re.IGNORECASE,
)


def _reach(chunk: str, start: int) -> str:
    """What this override may point at: its own sentence and the next one."""
    window = chunk[start : start + REASON_SCOPE]
    paragraph = _PARAGRAPH.search(window)
    if paragraph:
        window = window[: paragraph.start()]
    ends = [match.end() for match in _SENTENCE_END.finditer(window)]
    return window[: ends[1]] if len(ends) > 1 else window


def names_a_reason(chunk: str, start: int) -> bool:
    """True when what follows ``start`` names evidence that outranks a marker."""
    window = _reach(chunk, start)
    sources = [match.start() for match in _SOURCE.finditer(window)]
    if not sources:
        return False
    return any(
        any(abs(source - act.start()) <= PAIR_REACH for source in sources)
        for act in _CORROBORATION.finditer(window)
    )
