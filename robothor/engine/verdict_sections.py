"""A deliverable as a tree of sections, so a verdict reaches its items.

Extracted from ``verdict_shapes`` on the second measurement of the same
failure. That module answers "what does a verdict look like on the page";
this one answers "how far does one reach", which is a question about the
document's structure and nothing else. Keeping them together meant the file
that grows with every new *shape* also grew with the *tree*, and the two have
no vocabulary in common.

MEASURED 2026-09-17: the control read a real triage deliverable, found the
planted provenance marker and produced nothing, because the cut was made at
every heading level:

    ## Critical
    ### 1. <item>

The severity heading was a block with no item, the item was a block with no
severity, and a report that plainly assigned a verdict to every one of its
items read as assigning none at all.

The repair is a scope, and the whole of its design is in what does NOT become
one:

* only a heading that IS the label. ``## Critical`` is a section; ``# Critical
  Incident Review — Week 38`` is a title, and inheriting from it filed every
  item in the report under *critical* as well as its own severity — two
  verdicts, a contradiction the report never made, invented by the repair
  itself (review of the first cut). The verdict has to be the whole heading,
  give or take filler like *Issues* or a count;
* and only ONE label. ``## Critical / High priority items`` is an index of two
  categories, not a decision about the items under it, and reading it as a
  scope filed every one of them under two verdicts;
* only the NEAREST one. A `## Low` section under a `# Critical …` title
  resolves to *low*, not to both;
* not at all when the block states its own verdict, so ``### 3. … — upgraded
  to Critical`` under ``## High`` stays one decision;
* and it reaches the block as the LABEL it is, never as the heading's own
  words. Prepending the heading verbatim put its every word — a marker field,
  an identifier, an override phrase — into the input of every other detector
  for every item in the section.
"""

from __future__ import annotations

import re

from robothor.engine.verdict_shapes import VERDICTS, item_id_spans, verdicts_in

__all__ = ["block_subject", "blocks", "claim_owners"]

_HAS_HEADING = re.compile(r"^#{1,6}\s", re.MULTILINE)
_AT_HEADING = re.compile(r"^(?=#{1,6}\s)", re.MULTILINE)
_HEADING = re.compile(r"^(#{1,6})\s[^\n]*")
_PARAGRAPH = re.compile(r"\n\s*\n")

#: What may sit beside the verdict in a heading that is still just the label.
#: A count, an enumerator and these nouns; anything else is a heading that
#: talks about something, which is a title and not a section.
_FILLER = frozenset(
    {
        "issue",
        "issues",
        "item",
        "items",
        "message",
        "messages",
        "ticket",
        "tickets",
        "alert",
        "alerts",
        "incident",
        "incidents",
        "finding",
        "findings",
        "priority",
        "severity",
        "section",
        "bucket",
        "level",
        "group",
        "category",
        "list",
        "only",
    }
)
_WORD = re.compile(r"[a-z]+")

#: Where one item's entry in a list begins. A recap is written one item per
#: bullet, so a bullet is the unit a claim inside it belongs to.
_BULLET = re.compile(r"^[ \t]*(?:[-*+]|\d{1,3}[.)])[ \t]+", re.MULTILINE)

#: How a block names ITSELF when its heading is a title: an identity field,
#: in field position, whose value is one identifier and nothing else. The key
#: list is closed and short for the same reason every other vocabulary here is
#: — `**Routed to:** msg_2210` and `**Duplicate of:** msg_3101` name somebody
#: else, and a key that admitted them would move the defect rather than fix it.
_ID_FIELD = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?\|?[ \t]*\**[ \t]*"
    r"(?:message[ \t_-]*id|msg[ \t_-]*id|item[ \t_-]*id|ticket[ \t_-]*id|"
    r"id|item|message|ticket)"
    r"\**[ \t]*[:=|][ \t]*\**[ \t]*"
    r"(\b[a-z][a-z0-9]{1,12}_\d{2,}\b|\B#\d{1,5}\b|(?<![-/])\b[A-Z]{2,6}-\d{1,6}\b)"
    # A parenthetical after the id is still that id's field:
    # `**Message ID:** msg_2205 (follow-up: msg_2212)` is a block about
    # msg_2205 that says where the thread went. What is still refused is a
    # bare list — `**Message IDs:** msg_2202 / msg_2210` names two items, and
    # picking one of them would be the heading rule's known limit with none of
    # its excuse, so it falls back to no subject at all.
    r"[ \t]*\**[ \t]*(?:\([^)\n]{0,80}\))?[ \t]*\**[ \t]*\|?[ \t]*$",
    re.IGNORECASE,
)


def _scope_line(heading: str) -> str:
    """The heading as the verdict it ASSIGNS, or ``""`` when it assigns none.

    The return value is a synthesised label line carrying the matched verdict
    text and nothing else, which is what keeps the heading's own words out of
    every detector downstream.
    """
    text = re.sub(r"^\d+[.)]\s*", "", re.sub(r"^#{1,6}\s*", "", heading).strip())
    labels = [match.group(0) for pattern in VERDICTS.values() if (match := pattern.search(text))]
    # Exactly one. `## Critical / High priority items` assigned two verdicts to
    # every item filed under it, which is the "appears under 2 verdicts"
    # finding this rule exists not to invent (review, round 3). A heading that
    # names two categories is an index of them, not a decision about anything.
    if len(labels) != 1:
        return ""
    remainder = text
    for pattern in VERDICTS.values():
        remainder = pattern.sub(" ", remainder)
    if any(word not in _FILLER for word in _WORD.findall(remainder.lower())):
        return ""
    return f"# {labels[0]}"


def blocks(text: str) -> list[str]:
    """The document cut into item-sized pieces, each one in its section.

    On markdown headings where there are any, on blank lines where there are
    not. The cut only decides how far a verdict reaches from its item; every
    detector re-anchors on the item id itself.
    """
    if not _HAS_HEADING.search(text):
        return [part for part in _PARAGRAPH.split(text) if part.strip()]
    out: list[str] = []
    ancestors: list[tuple[int, str]] = []
    for part in _AT_HEADING.split(text):
        if not part.strip():
            continue
        heading = _HEADING.match(part)
        if heading:
            while ancestors and ancestors[-1][0] >= len(heading.group(1)):
                ancestors.pop()
        scope = next((line for _level, line in reversed(ancestors) if line), "")
        if heading:
            ancestors.append((len(heading.group(1)), _scope_line(heading.group(0))))
        out.append(part if not scope or verdicts_in(part) else f"{scope}\n{part}")
    return out


def block_subject(block: str) -> str:
    """The item this block is ABOUT, or ``""`` when it names none.

    A block that names itself — in its heading, or in an identity field —
    decides that item; every further identifier in it is a reference to
    somebody else's. ``### 4. msg_3104 — duplicate of msg_3101`` is a verdict
    about msg_3104, and reading it as one about msg_3101 too filed an item
    decided Critical in its own section under a second verdict it never
    received (review, round 4).

    MEASURED 2026-09-17, the first verified `enforce` run: the same defect came
    through the other door. Every item in that report was titled rather than
    identified — ``### 9. Automated weekly ticket summary`` over
    ``- **Message ID:** msg_2208`` — so the heading named no subject, and the
    summary's own sentence about the week ("they correlate with the customer
    complaints in msg_2203 and msg_2207") filed two items decided High under
    *low* as well. Two of the run's three findings were invented, and at
    `enforce` the model was re-asked to fix them.

    So an identity FIELD counts as naming the block's subject, which is what
    the heading rule always meant. The heading wins when both are present; a
    block with neither — a severity section with a bullet per item, the flat
    layout — still assigns its verdict to every id in it.

    KNOWN LIMIT, pinned by a test marked as such: a heading that decides two
    items at once — ``## msg_2209 and msg_2210 — both outages`` over one
    ``**Severity: Critical**`` — attributes the verdict to the first alone, so
    a marker contradicting the second goes unreported. It fails CLOSED, which
    is the right side of this trade: the alternative is the cross-reference bug
    this rule exists to fix. Telling a conjunction from a reference needs
    vocabulary this module deliberately does not have.
    """
    heading = ""
    body: list[str] = []
    for line in block.splitlines():
        if not body and line.startswith("#"):
            heading = line
        elif line.strip():
            body.append(line)
    spans = item_id_spans(heading)
    if spans:
        return spans[0][1]
    for line in body:
        field = _ID_FIELD.match(line)
        if field:
            return field.group(1)
    return ""


def claim_owners(block: str, subject: str, ids: set[str], offset: int) -> set[str]:
    """The items a claim made at ``offset`` in this block is ABOUT.

    A hedge, a hand-back and an override are claims about an item, and the
    first cut wrote each of them into every identifier in its block. MEASURED
    2026-09-17: a `## Notes & Recommendations` recap named six items, said of
    ONE of them "if it is real, escalate immediately", and the control reported
    five items that sentence was never about — at `enforce`, five items the
    model was then asked to re-decide.

    So: the block's subject when it has one, else the items named in the claim's
    own BULLET or paragraph, else — a claim with nothing nearer to attach to —
    the block. The bullet is the unit rather than the line because prose wraps:
    the measured recap put the item id on one physical line and the sentence
    about it two lines later, in the same numbered item.
    """
    if subject:
        return {subject} & ids or {subject}
    if offset < 0:
        return ids
    return {item for _at, item in item_id_spans(_claim_unit(block, offset))} or ids


def _claim_unit(block: str, offset: int) -> str:
    """The bullet or paragraph the text at ``offset`` belongs to."""
    starts = [match.start() for match in _BULLET.finditer(block) if match.start() <= offset]
    paragraph = block.rfind("\n\n", 0, offset)
    start = max([0, *starts, paragraph + 2 if paragraph >= 0 else 0])
    ends = [match.start() for match in _BULLET.finditer(block) if match.start() > offset]
    paragraph = block.find("\n\n", offset)
    end = min([len(block), *ends, paragraph if paragraph >= 0 else len(block)])
    return block[start:end]
