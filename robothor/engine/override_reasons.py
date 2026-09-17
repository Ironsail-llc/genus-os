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

What this module enforces is DISCLOSURE, not soundness and not polarity. It
asks whether the report named an external reason, never whether the reason is
a good one, and it cannot tell "the dashboard showed the service down" from
"the dashboard showed the service healthy" — evidence cited *against* the
override exempts as readily as evidence for it. That is deliberate. Judging
the argument would put this control in the business of second-guessing a
decision the operator can now see and weigh; judging its absence keeps it to
the one thing a detector can be right about, which is that a reader was left
with nothing to weigh at all.

KNOWN LIMITS, measured, and left alone on purpose (see the tests marked as
such — a later round "fixing" any of them buys a fabricated finding, which
costs more than the miss):

* *"; the ticket is INC-4412."* — a source beside a referent, with nothing
  claimed about either. The pointing-at rule cannot tell a citation from a
  mention;
* *"because it was escalated at 14:02."* and *"because I opened it at 14:02."*
  — the agent narrating its own action. The verb and the time are there and no
  source outside the report is;
* *"because the message id is msg_2209."* — the item's own identifier restated
  as though it were corroboration;
* the polarity case above: *"although the dashboard showed the service healthy
  at 14:02"* overrides a marker by citing evidence against itself, and exempts.

Every one of them is a sentence a reader can see and argue with, which is what
this control is for. Telling them from a real reason needs the semantics this
module deliberately does not have.

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

#: A source a reader could go and look at. Deliberately NOT here: `report`,
#: `record`, `system`, `team`, `user` and their relatives. Each of those names
#: the writer's own side of the page rather than a source outside it, and each
#: of them turned a sentence that named nothing into an exemption.
_SOURCE = re.compile(
    r"\b(?:message|messages|ticket|tickets|email|emails|thread|threads|channel|channels|"
    r"call|calls|log|logs|dashboard|dashboards|alert|alerts|monitor|monitors|monitoring|"
    r"telemetry|metric|metrics|status\s+page|feed|feeds|screenshot|screenshots|"
    r"customer|customers|client|clients|sender|caller|on-call|engineer|responder|"
    r"invoice|contract|transcript|recording)\b",
    re.IGNORECASE,
)

#: A referent concrete enough to find: a handle, an address, a time, a date, a
#: link, an item id. A source with one of these attached has been pointed at,
#: whatever the verb does — "three monitors are red at 14:02" names its
#: evidence as plainly as "three monitors confirmed it".
_REFERENT = re.compile(
    r"@[A-Za-z0-9][A-Za-z0-9._-]{1,30}"
    r"|[A-Za-z0-9._%+-]{1,40}@[A-Za-z0-9.-]{1,40}\.[A-Za-z]{2,}"
    r"|\b\d{1,2}:\d{2}\b|\b\d{4}-\d{2}-\d{2}\b|https?://\S{3,}"
    r"|\b[a-z][a-z0-9]{1,12}_\d{2,}\b|(?<![-/])\b[A-Z]{2,6}-\d{1,6}\b",
    re.IGNORECASE,
)

#: …or a count of them. "40 messages", "three replies": a quantity says how
#: much there is to look at. On its own beside a source it is NOT a reason —
#: "because three customers exist" counts something and claims nothing — so it
#: only ever qualifies the possessive below.
_QUANTITY = re.compile(
    r"\b(?:\d{1,6}|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"several|dozens?|hundreds?|thousands?)\b",
    re.IGNORECASE,
)

#: The possessive, which is a verb only when it says what is possessed. "The
#: incident channel HAS 40 messages about it" points at something; "the
#: customer has a point", "the engineer has seniority" and "the sender has
#: priority" are the same word doing nothing (review, round 4).
_POSSESSIVE = re.compile(r"\b(?:has|have|had)\b", re.IGNORECASE)

#: How far after a possessive its object may be.
_POSSESSED_REACH = 40

#: …and what a source may be DOING. Round 3 took only the strong reporting
#: verbs and so read "three monitors are red at 14:02" and "the incident
#: channel has 40 messages about it" as naming nothing, which is ordinary
#: English for naming something (review, round 3). Eventive and possessive
#: forms are here too; the bare copula is not — "the report is about a genuine
#: customer impact" is the sentence this whole module exists to catch, and
#: `is` alone cannot be what separates them. A copular claim reaches the
#: exemption through its referent or its count instead.
_CORROBORATION = re.compile(
    r"\b(?:confirm\w*|corroborat\w*|verif\w+|validat\w+|attest\w*|witness\w*|observ\w+|"
    # `reported`, never the bare `report` or `reports`: "the report is about a
    # genuine customer impact" is a noun phrase, and reading it as a verb
    # exempted the sentence that names nothing (round 4, caught by its own test).
    r"reported|shows?|showed|showing|appears?|appeared|opened|raised|fired|"
    r"escalated|emailed|messaged|paged|phoned|called|replied|posted|said|says|wrote|"
    r"logged|recorded|matched|matches|traced|reproduced)\b",
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


def _near(spans: list[int], others: list[int], reach: int = PAIR_REACH) -> bool:
    """True when something in one list sits within ``reach`` of the other."""
    return any(abs(one - other) <= reach for one in spans for other in others)


def _acts(window: str) -> list[int]:
    """Where something is CLAIMED in this window, possessives qualified."""
    acts = [match.start() for match in _CORROBORATION.finditer(window)]
    for match in _POSSESSIVE.finditer(window):
        after = window[match.end() : match.end() + _POSSESSED_REACH]
        if _QUANTITY.search(after) or _REFERENT.search(after):
            acts.append(match.start())
    return acts


def names_a_reason(chunk: str, start: int) -> bool:
    """True when what follows ``start`` names evidence that outranks a marker.

    Two ways to name evidence, and a handle is one of them: something CLAIMED
    about a source or about a referent (``@owner-a confirmed it``, ``INC-4412
    was opened at 14:02``, ``https://… showed a live outage``), or a source
    that has been pointed at by a referent (``the incident channel at 14:02``).
    Requiring a source NOUN first made the most checkable reason a report can
    give — an id, a link, an address — no reason at all (review, round 4).

    What is never enough: a verb with nothing to attach to, a referent nobody
    says anything about, or a source with neither.
    """
    window = _reach(chunk, start)
    sources = [match.start() for match in _SOURCE.finditer(window)]
    referents = [match.start() for match in _REFERENT.finditer(window)]
    if not sources and not referents:
        return False
    return _near(sources + referents, _acts(window)) or _near(sources, referents)
