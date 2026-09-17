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

__all__ = ["blocks", "heading_subject"]

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


def heading_subject(block: str) -> str:
    """The item this block's own heading is ABOUT, or ``""``.

    A heading that leads with an identifier names the item the block decides;
    any further identifier in it is a reference to somebody else's item.
    ``### 4. msg_3104 — duplicate of msg_3101`` is a verdict about msg_3104,
    and reading it as one about msg_3101 too filed an item decided Critical in
    its own section under a second verdict it never received (review, round 4).

    The block may arrive with its inherited scope line in front of it, so the
    block's own heading is the LAST of the leading heading lines.
    """
    heading = ""
    for line in block.splitlines():
        if line.startswith("#"):
            heading = line
        elif line.strip():
            break
    spans = item_id_spans(heading)
    return spans[0][1] if spans else ""
