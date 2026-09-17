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

import json
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
    """The one re-ask. Quotes what was rejected, and SHOWS the shape once.

    One, not three. Each re-ask is another whole vision call with the image
    attached, so a batch of 200 that retried twice would cost three times what
    the tool's "cheap enough to call a hundred times" claim rests on. One catches
    the formatting slip that is the common failure; a model that has been shown
    the list twice and answered outside it is telling the agent something, and
    reporting that as the row's error is more useful than a third attempt.

    It shows a worked example using a REAL label rather than restating the
    two-line contract a second time. Repeating an instruction a model has
    already read once and not followed is the least likely thing to change its
    mind; showing it the exact bytes expected, with the shapes it must not use
    named, is the most. What the parser now forgives (a fence, an inline
    marker, JSON) does not need a re-ask at all, so this is what is left.
    """
    quoted = rejected[:MAX_QUOTED_REPLY_CHARS].replace("\n", " ")
    example = contract.labels[0] if contract.labels else "your answer"
    return (
        f"\n\nYour previous reply, {quoted!r}, is not one of the allowed answers. Look at "
        "the image again and reply in exactly this shape — two lines, nothing before or "
        "after them:\n"
        f"ANSWER: {example}\n"
        "WHY: one short sentence naming what you SEE in this image.\n"
        "The ANSWER line must hold one of these and nothing else, copied character for "
        "character: " + " | ".join(contract.labels)
    )


#: A model that has been asked for two labelled lines writes them with markdown
#: bold, a bullet, a heading hash or a full-width colon about as often as
#: plainly. ``REASON`` is accepted beside ``WHY`` because it is what a model
#: reaches for when the question already contains the word "why".
_ANSWER_LINE = re.compile(r"(?im)^[\s*_#>\-]*answer[\s*_]*[:：]\s*(.+?)\s*$")
_WHY_LINE = re.compile(r"(?im)^[\s*_#>\-]*(?:why|reason)[\s*_]*[:：]\s*(.+?)\s*$")

#: The same two markers where they appear MID-LINE. A model that was asked for
#: two lines writes them on one often enough that treating it as a wrong answer
#: turned whole batches into errors — at twice the cost, because the re-ask
#: restated the contract to a model whose habit was the problem.
#: The boundary is spelled out rather than left to ``\b``: underscore is a WORD
#: character, so ``\b`` never fires between the ``__`` of ``__WHY:__`` and the
#: marker, and underscore bold slipped through the one pattern meant to catch
#: emphasis. Requiring a real separator in front also keeps ``somewhy: x`` from
#: being split in half, which is what ``\b`` was there for.
_INLINE_WHY = re.compile(r"(?:^|[\s*_#>\-])[\s*_#>\-]*(?:why|reason)[\s*_]*[:：]\s*", re.IGNORECASE)

#: A reasoning model's scratchpad, and a fenced block. Both wrap a perfectly
#: good reply in characters the markers are then looked for inside.
_THINK_BLOCK = re.compile(r"(?is)<(think|thinking|reasoning)>.*?</\1>")
_FENCE = re.compile(r"(?s)\A\s*```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*```\s*\Z")

#: A gloss the model could not resist adding after the label it was asked for:
#: ``chart (a bar chart with a titled axis)``. Only read when the head is a
#: real label — see :func:`parse_reply`.
_TRAILING_PARENTHETICAL = re.compile(r"(?s)\A(.*?)\s*[(（]([^()（）]*)[)）]\s*\Z")

#: Keys a model reaches for when it decides the reply should be JSON.
_JSON_ANSWER_KEYS = ("answer", "choice", "label", "category", "classification")
_JSON_REASON_KEYS = ("why", "reason", "because", "justification", "explanation")


@dataclass(frozen=True)
class Reply:
    """One parsed reply. ``rejected`` is set only when a choice was required
    and the model named none of them — then ``answer`` is empty, because an
    unmatched string must never travel as a label."""

    answer: str
    reason: str
    rejected: str = ""


def _unwrap(text: str) -> str:
    """The reply with a reasoning scratchpad and a code fence taken off.

    Both are wrappers a model adds around a reply that is otherwise exactly
    what was asked for, and both used to hide the markers from the
    line-anchored patterns below.
    """
    return _FENCE.sub(r"\1", _THINK_BLOCK.sub("", text).strip()).strip()


def _from_json(text: str) -> tuple[str, str] | None:
    """``(answer, reason)`` when the whole reply is a JSON OBJECT, else None.

    An object is the only shape that carries named keys. A bare string, a
    number or a list is just a reply and is read as text — trying to be clever
    about a list would be guessing which element was meant.
    """
    if not text.startswith("{"):
        return None
    try:
        loaded = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(loaded, dict):
        return None
    folded = {str(key).strip().casefold(): value for key, value in loaded.items()}

    def first(keys: tuple[str, ...]) -> str:
        for key in keys:
            value = folded.get(key)
            if isinstance(value, (str, int, float, bool)):
                return str(value).strip()
        return ""

    answer = first(_JSON_ANSWER_KEYS)
    return (answer, first(_JSON_REASON_KEYS)) if answer else None


#: Markdown emphasis left over once a marker has been found. ``**ANSWER:**``
#: closes its bold AFTER the colon, so the marker patterns cannot eat it and it
#: used to ride into the row: ``'** chart'``, ``'** bars'``. The matcher strips
#: these on both sides so a CHOICE still matched, which is exactly why it went
#: unnoticed — the damage was to free-text answers and to every reason.
_EMPHASIS = " \t*_`"


def _strip_emphasis(text: str) -> str:
    """One captured field with its leftover markdown taken off both ends."""
    return text.strip().strip(_EMPHASIS).strip()


def _from_markers(text: str) -> tuple[str, str]:
    """``(answer, reason)`` from whatever ``ANSWER:``/``WHY:`` markers are there.

    The LAST marked line wins: a model that restates the instruction before
    obeying it puts the instruction first. An ``ANSWER:`` line is split at an
    inline ``WHY:``/``REASON:``, so both-on-one-line reads the same as the two
    lines that were asked for. With no marker at all the whole reply is the
    answer, which is the #581 behaviour and the reason a weak backend that
    ignores the contract still returns something usable.
    """
    answers = _ANSWER_LINE.findall(text)
    reasons = _WHY_LINE.findall(text)
    inline = ""
    if answers:
        parts = _INLINE_WHY.split(answers[-1].strip(), maxsplit=1)
        stated = _strip_emphasis(parts[0])
        inline = _strip_emphasis(parts[1]) if len(parts) > 1 else ""
    else:
        # A reply that is nothing but a WHY line used to answer with the marker
        # still stuck to it — the sentence twice, once spoiled.
        stated = _WHY_LINE.sub("", text).strip()
    reason = _strip_emphasis(reasons[-1]) if reasons else inline
    return (stated or reason or text.strip()), reason


def parse_reply(text: str, contract: Contract) -> Reply:
    """Pull the answer and the evidence out of whatever came back.

    Three layers, each of which only ever RE-SHAPES the reply: the wrappers
    come off, a JSON object is read by its keys, and the markers are found
    wherever they sit. What falls out still has to pass :meth:`Contract.match`
    unchanged, so none of this loosens the equality rule — a reply that names
    no label is still off-list, still re-asked once, still an error.

    The last resort, and only when a choice is required, is a trailing
    parenthetical: ``chart (a bar chart)`` becomes the label plus its reason,
    but ONLY if the head is a real label. It is not a licence to trim until
    something matches — ``banana (a chart)`` names nothing and stays off-list.
    Free text keeps its parentheses, because there is no way to tell a gloss
    from part of the answer and nothing is lost by leaving it alone.

    On the FREE-TEXT path the JSON reader is gated on shape, and that gate is
    the difference between reading a reply and eating an answer. "Transcribe
    the JSON on this screen" is an ordinary question, and its honest answer is
    an object — one that may well carry a key called ``answer``, ``label`` or
    ``category``, because real payloads do. Reading one key out of it returned
    a fragment with nothing to say it was a fragment: two near-identical
    screenshots transcribed differently depending on a key name, which is not
    a property an agent can reason about. So with no ``choices`` the object is
    accepted only when it looks like a reply to the two-line contract — an
    answer key AND a reason key — and otherwise the whole reply comes back as
    text. ``{"answer": "42", "why": "the big number"}`` is still read as the
    reply it is. On the ``choices`` path there is nothing to gate: whatever is
    extracted still has to BE a label, and a data object simply is not one.
    """
    body = _unwrap(text)
    parsed = _from_json(body)
    if parsed is not None and not contract.labels and not parsed[1]:
        parsed = None  # data, not an answer to the question we asked
    stated, reason = parsed if parsed is not None else _from_markers(body)
    if not contract.labels:
        return Reply(stated, reason)
    matched = contract.match(stated)
    if matched is None:
        gloss = _TRAILING_PARENTHETICAL.match(stated)
        if gloss:
            head = contract.match(gloss.group(1))
            if head is not None:
                return Reply(head, reason or gloss.group(2).strip())
        return Reply("", reason, text.strip() or "(an empty reply)")
    return Reply(matched, reason)
