"""What a vision model is allowed to answer, and how the answer is checked.

Extracted from :mod:`robothor.engine.vision_batch` when ``choices`` arrived:
that module fans calls out, bounds them and budgets the result, and "is this
string one of the labels the caller offered" is a different question with its
own rules and its own way of being got wrong. Keeping it here means the safety
property can be read in one screen rather than found among semaphores.

The property, stated once
-------------------------
**Matching is EQUALITY on a normalised form. Never a prefix, never a
substring, never a distance.** A fuzzy match makes ``["cat", "category"]``
unanswerable and turns "the model said something vaguely like one of these"
into a label the agent acts on — and acting on a label it could not audit is
the measured failure ``choices`` exists to fix. Anything that is not an exact
normalised match is off-list: re-asked once by the caller, then reported as
that row's error, and never passed through as a label.

The normalisation (case folded, surrounding typography stripped, inner
whitespace collapsed) is applied to both sides from :func:`_match_key`, so a
label containing anything it strips still matches itself. Nothing here reads
the truncation mark, or any other marker, as a signal.

The reply contract
------------------
Every reply is asked for as two lines — ``ANSWER:`` and ``WHY:``. The second is
the active ingredient. ``{"answer": "1"}`` is a bare token an agent talks
itself out of believing when a filename disagrees with it; ``{"choice": "1",
"reason": "horizontal bar chart of aid flow from Sweden"}`` is not. A backend
that ignores the contract is still parsed for whatever it did say, because a
thin answer beats no answer — the caller flags the gap rather than inventing
one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: The smallest and largest ``choices`` list. Two is the smallest list that
#: constrains anything. Twenty is where "pick one of these" has become a
#: taxonomy: the list is repeated into every image's prompt, so a hundred
#: labels is a hundred labels paid for two hundred times, and a model asked to
#: hold that many in one reply starts inventing neighbours of them.
MIN_CHOICES = 2
MAX_CHOICES = 20

#: Longest one label may be. A label is a label; a sentence here is a second
#: question wearing a label's clothes, and 200 rows of one defeats the inline
#: budget in ``vision_batch``.
MAX_CHOICE_CHARS = 80

#: How much of a rejected reply is quoted back, to the model in the re-ask and
#: to the agent in the error. Enough to recognise what went wrong, bounded
#: because a model that ignored the contract may have ignored it at length.
MAX_QUOTED_REPLY_CHARS = 80

#: Characters stripped from both ends before two labels are compared. A model
#: told to copy a label back writes ``**Chart**.`` about as often as ``chart``,
#: and the difference is typography, not an answer.
_MATCH_STRIP = " \t\r\n\"'`*_.,;:!?()[]{}<>"


def _match_key(text: str) -> str:
    """The form two labels are compared in — an EQUALITY, never a prefix.

    See the module docstring: this is the whole safety property, and the
    normalisation is applied to both sides from here so that a label
    containing anything it strips still matches itself.
    """
    return " ".join(text.strip().strip(_MATCH_STRIP).casefold().split())


@dataclass(frozen=True)
class Contract:
    """What the model is allowed to answer, and how the answer is checked.

    Empty ``labels`` means the caller passed no ``choices`` and the answer is
    free text — the behaviour that shipped in #581, unchanged.
    """

    labels: tuple[str, ...] = ()
    table: dict[str, str] | None = None
    listed: str = ""

    def match(self, stated: str) -> str | None:
        """The caller's own spelling of *stated*, or None when it is off-list."""
        if not self.table:
            return None
        return self.table.get(_match_key(stated))


def build_contract(raw: Any) -> tuple[Contract, str]:
    """``(contract, refusal)`` for a requested ``choices`` list.

    Refuses at the boundary rather than per row: a malformed list is a
    malformed call, and discovering that two hundred images later — after two
    hundred vision calls have been paid for — is the expensive way to find out.

    Two entries that normalise to the same key are refused rather than merged.
    A result whose rows say ``Chart`` when the caller also offered ``chart.``
    cannot answer "which one did it pick", and picking one silently is the
    coercion this parameter exists to remove.
    """
    if raw is None:
        return Contract(), ""
    if not isinstance(raw, (list, tuple)):
        return Contract(), (
            "refused: choices must be a list of labels, one of which the model must "
            "answer with. Leave it out to ask an open question."
        )
    if not MIN_CHOICES <= len(raw) <= MAX_CHOICES:
        return Contract(), (
            f"refused: choices takes {MIN_CHOICES}-{MAX_CHOICES} labels and got "
            f"{len(raw)}. One label is not a question; more than {MAX_CHOICES} is a "
            "taxonomy — ask a coarser question first and split the result."
        )
    labels: list[str] = []
    table: dict[str, str] = {}
    for entry in raw:
        refusal = _reject_entry(entry, table)
        if refusal:
            return Contract(), refusal
        label = str(entry).strip()
        table[_match_key(label)] = label
        labels.append(label)
    return Contract(tuple(labels), table, short_list(labels)), ""


def _reject_entry(entry: Any, table: dict[str, str]) -> str:
    """Why *entry* cannot be a choice beside the ones already in *table*."""
    if not isinstance(entry, str):
        return (
            f"refused: every entry in choices must be a string, and {entry!r} is "
            f"a {type(entry).__name__}."
        )
    label = entry.strip()
    if not label:
        return "refused: choices cannot contain a blank label."
    if len(label) > MAX_CHOICE_CHARS:
        return (
            f"refused: {label[:40]!r}… is longer than the {MAX_CHOICE_CHARS}-character "
            "limit for one choice. A choice is a label, not a sentence."
        )
    key = _match_key(label)
    if not key:
        return f"refused: {label!r} is punctuation only, so no reply could ever match it."
    if key in table:
        return (
            f"refused: {label!r} and {table[key]!r} are the same label once case and "
            "surrounding punctuation are set aside, so a row naming one of them would "
            "not say which. Give each choice a distinct name."
        )
    return ""


def short_list(labels: list[str] | tuple[str, ...], limit: int = 120) -> str:
    """The choices, named for a human, bounded. Repeated on every error row."""
    text = ", ".join(labels)
    return text if len(text) <= limit else text[:limit].rstrip(", ") + ", …"


#: The two lines every reply must carry.
_CONTRACT_TEMPLATE = (
    "\n\nReply in exactly two lines and nothing else:\n"
    "ANSWER: {answer_line}\n"
    "WHY: one short sentence naming what you SEE in this image that decides it."
)


def contract_suffix(contract: Contract) -> str:
    """What is appended to the agent's question before the model sees it.

    Appended, never prepended: the agent's own words are the first thing the
    model reads, and a tool that put its own framing in front of them would be
    answering a question nobody asked.
    """
    if contract.labels:
        return _CONTRACT_TEMPLATE.format(
            answer_line=(
                "exactly one of these, copied character for character — "
                + " | ".join(contract.labels)
            )
        )
    return _CONTRACT_TEMPLATE.format(answer_line="your answer to the question above")


def correction_suffix(contract: Contract, rejected: str) -> str:
    """The one re-ask. Quotes what was rejected, and says what was wrong.

    One, not three. Each re-ask is another whole vision call with the image
    attached, so a batch of 200 that retried twice would cost three times what
    the tool's "cheap enough to call a hundred times" claim rests on. One catches
    the formatting slip that is the common failure; a model that has been shown
    the list twice and answered outside it is telling the agent something, and
    reporting that as the row's error is more useful than a third attempt.
    """
    quoted = rejected[:MAX_QUOTED_REPLY_CHARS].replace("\n", " ")
    return (
        f"\n\nYour previous reply, {quoted!r}, is not one of the allowed answers. Look "
        "at the image again and answer with one of these, copied character for "
        "character — "
        + " | ".join(contract.labels)
        + _CONTRACT_TEMPLATE.format(answer_line="exactly one of the labels above")
    )


#: A model that has been asked for two labelled lines writes them with markdown
#: bold, a bullet, a heading hash or a full-width colon about as often as
#: plainly. ``REASON`` is accepted beside ``WHY`` because it is what a model
#: reaches for when the question already contains the word "why".
_ANSWER_LINE = re.compile(r"(?im)^[\s*_#>\-]*answer[\s*_]*[:：]\s*(.+?)\s*$")
_WHY_LINE = re.compile(r"(?im)^[\s*_#>\-]*(?:why|reason)[\s*_]*[:：]\s*(.+?)\s*$")


@dataclass(frozen=True)
class Reply:
    """One parsed reply. ``rejected`` is set only when a choice was required
    and the model named none of them — then ``answer`` is empty, because an
    unmatched string must never travel as a label."""

    answer: str
    reason: str
    rejected: str = ""


def parse_reply(text: str, contract: Contract) -> Reply:
    """Pull the answer and the evidence out of whatever came back.

    The LAST marked line wins: a model that restates the instruction before
    obeying it puts the instruction first. With no marker at all the whole
    reply is the answer, which is the #581 behaviour and the reason a weak
    backend that ignores the contract still returns something usable.
    """
    answers = _ANSWER_LINE.findall(text)
    reasons = _WHY_LINE.findall(text)
    if answers:
        stated = answers[-1].strip()
    else:
        stated = _WHY_LINE.sub("", text).strip() or text.strip()
    reason = reasons[-1].strip() if reasons else ""
    if not contract.labels:
        return Reply(stated, reason)
    matched = contract.match(stated)
    if matched is None:
        return Reply("", reason, text.strip() or "(an empty reply)")
    return Reply(matched, reason)
