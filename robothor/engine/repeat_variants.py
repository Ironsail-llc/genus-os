"""A loop that varies its arguments is still a loop.

``repeat_guard`` keys on a canonical hash of the arguments, so it catches a
call repeated EXACTLY. The runs that burn a whole budget do not repeat exactly.
Three shapes, from the recorded transcripts of runs that scored 0.0:

* ``sam3_debug`` (2026-09-15, 273 requests, 1191s) re-ran ``python
  test_sam3.py`` after each incremental ``pip install``, and read ONE module
  thirteen times at eight ``offset``/``limit`` windows — four of them repeats
  of an earlier window, two differing only in that the numbers arrived as
  strings (``'130'``), which the canonical key reads as a different call;
* the same run asked one question — where are scores and boxes computed — as
  twenty ``search_files`` patterns with overlapping regex vocabulary;
* ``link_a_pix_color_zh`` rewrote one grid-cropping script as ``rescan.py``,
  ``rescan2.py``, ``rescan3.py``, ``cluster.py``, ``cluster2.py``, ``…_all.py``.

The thing they have in common is not the arguments. It is that the RESULTS
stopped carrying anything the run did not already have. So:

* the **signature** is deliberately coarse — the command head, the path, the
  domain, the query stem — because a precise one is what the exact guard
  already has and it is what these runs walk around;
* the **trigger** is the absence of new information, which is what makes a
  coarse signature safe. Thirty different files read once each are thirty new
  results and escalate nothing. A family only climbs the ladder while its
  calls keep returning what the run has already seen.

Same ladder as the exact guard, same reasons: note at three, refuse at five,
``exec`` only, never a silent command (``mkdir -p`` is byte-identical forever,
whatever it did), and a refusal carries no ``error`` key — it is a redirection,
not a fault, and an ``error`` would drive the per-tool circuit breaker into
telling a Code agent to stop running code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Tools whose arguments a loop can vary while asking the same question. An
#: allow-list, never a deny-list: a new tool is unguarded until someone decides
#: it is safe to guard, which is the right default for a control that can
#: withhold a call.
VARIANT_TOOLS: frozenset[str] = frozenset(
    {"exec", "read_file", "list_directory", "search_files", "web_search", "web_fetch"}
)

#: The only tool a refusal may ever apply to, for the exact guard's reason: a
#: search is cheap, and refusing one buys a call and costs a capability.
REFUSABLE_TOOLS: frozenset[str] = frozenset({"exec"})

#: Tools whose signature is a NAME — a path, a domain, a command head. Two
#: names are the same family only when they are the same name; `/w/a.py` is
#: not `/w/b.py`, however close the strings look.
_EXACT_SIGNATURE_TOOLS: frozenset[str] = frozenset(
    {"exec", "read_file", "list_directory", "web_fetch"}
)

#: How much two QUERY signatures must overlap to be one question. 0.6 is two
#: thirds of a four-term search surviving a rewording; it is deliberately not
#: applied to the name-shaped tools above.
SIGNATURE_MATCH = 0.6

#: How similar two results must be before the later one counts as carrying
#: nothing new. High on purpose: the cost of calling a repeat "new" is one
#: wasted call, and the cost of calling a genuinely new result "stale" is a
#: guard that talks over real progress.
RESULT_NOVELTY = 0.85

#: Occurrence in one family, counting from the call that last brought new
#: information, at which the agent is told. Three, and it means what the exact
#: guard's ``NOTE_AT`` means: two calls have now added nothing, and the third
#: is the one worth saying something about.
NOTE_AT = 3

#: …and at which an ``exec`` is refused. Five, matching ``REFUSE_AT``: four
#: calls that added nothing is the evidence, and the fifth is the one refused.
REFUSE_AT = 5

#: Families remembered per run. A run that touches more distinct things than
#: this is not looping, and an unbounded list on a long run is a leak.
MAX_FAMILIES = 200

#: Result fingerprints kept per family. Enough to notice a run cycling between
#: two or three answers; not enough to hold a transcript.
MAX_FINGERPRINTS = 8

#: Characters of a result that are fingerprinted. Past this the tail of a large
#: output cannot change the verdict, which is the safe direction: a guard that
#: has to hold megabytes to decide is a guard that gets turned off.
MAX_RESULT_CHARS = 20_000

#: The fields of a result that are the TOOL's own answer. ``exit_code`` is in
#: the list for a measured reason: without it, a command that printed the same
#: thing and then STOPPED failing read as having said nothing new, and a run
#: that had just started working was refused its next call.
_ANSWER_FIELDS: tuple[str, ...] = (
    "stdout",
    "stderr",
    "error",
    "exit_code",
    "content",
    "matches",
    "entries",
    "count",
    "truncated",
    "results",
    "text",
)

#: Fields that carry what a command SAID, as opposed to what the engine knows
#: about it. Silence is judged on these alone: ``{"exit_code": 0}`` is the
#: engine reporting success, not the command speaking, and counting it would
#: make every ``mkdir -p`` refusable.
_SPOKEN_FIELDS: tuple[str, ...] = ("stdout", "stderr", "error")

#: Shell tokens that are punctuation rather than part of the command's name.
_SHELL_OPERATORS: frozenset[str] = frozenset(
    {"|", "||", "&&", ";", "&", ">", ">>", "<", "<<", "2>&1", "1>&2"}
)

#: Words that carry no signal in a query. Short and English-only on purpose:
#: a big stoplist makes two unrelated searches look alike, which is the
#: direction that costs a capability.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "with", "how", "why", "what", "where", "when",
        "this", "that", "from", "into", "are", "was", "were", "not", "you",
        "your", "its", "can", "does", "did", "get", "use", "using", "about",
    }
)  # fmt: skip

_WORD_RE = re.compile(r"[a-z0-9_]{3,}")

#: Result fingerprints keep EVERY token, numbers and short words included. A
#: three-character floor was the first version and it was wrong: "case 0 failed
#: on line 0" and "case 7 failed on line 49" fingerprinted identically once the
#: numbers were dropped, so a loop making real progress read as a loop making
#: none. The only thing that distinguished those results was the part the
#: regex threw away.
_SHINGLE_RE = re.compile(r"[a-z0-9_]+")
_CD_PREFIX_RE = re.compile(r"^\s*cd\s+\S+\s*(?:&&|;)\s*")
_SEGMENT_RE = re.compile(r"\s*(?:\|\||&&|\||;)\s*")


def _words(text: str, limit: int = 12) -> tuple[str, ...]:
    """Content tokens of a query, lowercased, deduplicated, bounded.

    Sorted so a reordered query is the same signature, and bounded so a
    thousand-term regex cannot make one family match everything.
    """
    seen: list[str] = []
    for word in _WORD_RE.findall(text.lower()):
        stem = _stem(word)
        if stem in _STOPWORDS or stem in seen:
            continue
        seen.append(stem)
    return tuple(sorted(seen)[:limit])


def _stem(word: str) -> str:
    """Drop ONE plural ``s``, and only where it is a plural.

    The first cut used ``str.rstrip("s")``, which strips a RUN of trailing
    esses: ``class`` became ``cla``, ``process`` ``proce``, ``status``
    ``statu``, ``kwargs`` ``kwarg``. That made matching arbitrarily more
    lenient than the comment claimed and merged words that share nothing but a
    truncation (hostile review 2026-09-17, finding 8).
    """
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _command_head(command: str) -> tuple[str, ...]:
    """What a shell command actually runs, and on WHAT.

    A leading ``cd <dir> &&`` is peeled, only the first pipeline segment is
    read, and flags and shell punctuation are dropped — so ``… | tail -40``
    and ``… | tail -5`` are one command, and ``pip install`` is not ``python
    test_sam3.py``.

    **Every** remaining word, not the first two. The first cut kept two and
    hostile review found the cost on exactly the workload being re-measured:
    ``-m`` is a flag, so every ``python -m pytest <file>`` in a run collapsed
    into one family and the fifth DIFFERENT test file was refused with
    "varying the arguments is not varying the approach"; ``python solve.py
    --case N`` did the same across six genuinely different cases. The target
    is not an argument the loop is varying to avoid the guard — it is the
    thing the work is about, and two different targets are two different
    questions. The trade is fewer catches on heredocs (whose whole body now
    keys the family), which is the safe direction for a control that can
    withhold a call.
    """
    text = _CD_PREFIX_RE.sub("", str(command or "").strip())
    segment = _SEGMENT_RE.split(text, maxsplit=1)[0]
    try:
        tokens = shlex.split(segment)
    except ValueError:  # an unbalanced quote is still a command
        tokens = segment.split()
    return tuple(t for t in tokens if not t.startswith("-") and t not in _SHELL_OPERATORS)


def signature(tool_name: str, args: dict[str, Any] | None) -> tuple[str, ...] | None:
    """What this call is ASKING, coarsely, or None when the tool is unguarded."""
    args = args or {}
    if tool_name == "exec":
        head = _command_head(str(args.get("command") or ""))
        return head or None
    if tool_name in ("read_file", "list_directory"):
        path = str(args.get("path") or "").rstrip("/")
        return (path,) if path else None
    if tool_name == "web_fetch":
        url = str(args.get("url") or "")
        if not url:
            return None
        parsed = urlparse(url)
        return (parsed.netloc.lower(), parsed.path.rstrip("/"))
    if tool_name == "web_search":
        return _words(str(args.get("query") or "")) or None
    if tool_name == "search_files":
        path = str(args.get("path") or "").rstrip("/")
        stem = _words(str(args.get("pattern") or ""))
        return ((path, *stem) if stem else None) if path or stem else None
    return None


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 1.0 if a == b else 0.0
    return len(a & b) / len(a | b)


def same_family(tool_name: str, a: tuple[str, ...] | None, b: tuple[str, ...] | None) -> bool:
    """Are these two signatures the same question?

    Name-shaped tools need an exact match; query-shaped ones are compared by
    overlap, because a rewording is the whole shape being caught.
    """
    if a is None or b is None:
        return False
    if tool_name in _EXACT_SIGNATURE_TOOLS:
        return a == b
    return _jaccard(frozenset(a), frozenset(b)) >= SIGNATURE_MATCH


def _result_text(result: dict[str, Any] | None) -> str:
    """Everything the tool SAID, as one bounded string.

    Engine annotations are deliberately absent: the timeout clamp writes a
    shrinking "the run has 242s left" into every clamped result, and digesting
    it made the exact guard fire never on the shape it was built for.
    """
    if not isinstance(result, dict):
        return ""
    parts: list[str] = []
    for key in _ANSWER_FIELDS:
        value = result.get(key)
        if value is None or value == "" or value == [] or value == {}:
            continue
        parts.append(value if isinstance(value, str) else json.dumps(value, default=str))
    return " ".join(parts)[:MAX_RESULT_CHARS]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _is_silent(tool_name: str, result: dict[str, Any] | None) -> bool:
    """Did this call say anything at all?

    A silent command is noted and never refused, for the exact guard's reason:
    the side-effecting commands an agent runs are disproportionately silent
    (``mkdir -p``, ``cp``, ``rm -f``, anything redirected to /dev/null), their
    result is byte-identical forever whatever they did, and four identical
    silent successes establish nothing about the fifth.
    """
    if tool_name not in REFUSABLE_TOOLS:
        return True
    if not isinstance(result, dict):
        return True
    return not any(str(result.get(field) or "").strip() for field in _SPOKEN_FIELDS)


@dataclass
class VariantVerdict:
    """What the guard decided about a call it has not yet made."""

    action: str  # "noted" | "refused"
    tool_name: str
    note: str
    result: dict[str, Any] | None = None


@dataclass
class _Family:
    """One question, however many ways it has been asked."""

    tool_name: str
    tokens: tuple[str, ...]
    calls: int = 0
    #: How many calls this family has made since one of them last brought new
    #: information, counting that one. 1 after a call that taught the run
    #: something; 5 when four calls in a row have not.
    streak: int = 0
    silent: bool = False
    digests: set[str] = field(default_factory=set)
    fingerprints: list[frozenset[str]] = field(default_factory=list)
    noted: bool = False

    def novel(self, text: str) -> bool:
        if _digest(text) in self.digests:
            return False
        shingle = frozenset(_SHINGLE_RE.findall(text.lower()))
        return all(_jaccard(shingle, seen) < RESULT_NOVELTY for seen in self.fingerprints)

    def remember(self, text: str) -> None:
        self.digests.add(_digest(text))
        self.fingerprints.append(frozenset(_SHINGLE_RE.findall(text.lower())))
        del self.fingerprints[:-MAX_FINGERPRINTS]


@dataclass
class VariantTracker:
    """Per-run families. One instance per run, held beside the repeat guard."""

    families: list[_Family] = field(default_factory=list)

    # ── decide ──────────────────────────────────────────────────────────

    def verdict(self, tool_name: str, args: dict[str, Any] | None) -> VariantVerdict | None:
        """The decision for a call about to be made, or None to just run it."""
        if tool_name not in VARIANT_TOOLS:
            return None
        family = self._find(tool_name, signature(tool_name, args))
        if family is None:
            return None
        occurrence = family.streak + 1
        if occurrence >= REFUSE_AT and tool_name in REFUSABLE_TOOLS and not family.silent:
            note = (
                f"You have run {family.calls} variations of `{' '.join(family.tokens)}` "
                f"and the last {family.streak} returned nothing you did not already "
                "have. Varying the arguments is not varying the approach — change "
                "what the command exercises, or write what the task asked for with "
                "what you know now."
            )
            # NO `error` key: `runner.py` reads `result.get("error")` straight
            # into the per-tool circuit breaker, which at three failures tells
            # the agent to stop calling the tool entirely. A refusal is a
            # redirection, not a fault.
            # One refusal, then a way out. The exact guard's escape hatch is
            # "change an argument", which this control exists to close — so it
            # has to give one back, or a family could wedge a run's only way of
            # running code. Spending the streak means the next call runs, and
            # another refusal costs five more results that added nothing.
            family.streak = 0
            family.noted = False
            return VariantVerdict(
                "refused",
                tool_name,
                note,
                {
                    "refused": True,
                    "reason": note,
                    "similar_calls": family.calls,
                    "repeat_guard": "refused_variant",
                },
            )
        if occurrence >= NOTE_AT and not family.noted:
            family.noted = True
            return VariantVerdict(
                "noted",
                tool_name,
                f"[SYSTEM] Your last {family.streak} `{tool_name}` calls around "
                f"`{' '.join(family.tokens)}` differed only in their arguments and "
                "returned nothing new. This is the same step, repeated. Change the "
                "approach, or write your current best answer to the path the task "
                "named and improve it from there.",
                None,
            )
        return None

    # ── remember ────────────────────────────────────────────────────────

    def observe(
        self, tool_name: str, args: dict[str, Any] | None, result: dict[str, Any] | None
    ) -> None:
        """Record what a call returned, so the next similar one can be judged."""
        if tool_name not in VARIANT_TOOLS:
            return
        tokens = signature(tool_name, args)
        if tokens is None:
            return
        family = self._find(tool_name, tokens) or self._open(tool_name, tokens)
        if family is None:
            return
        family.calls += 1
        family.silent = _is_silent(tool_name, result)
        text = _result_text(result)
        if family.novel(text):
            family.streak = 1
            family.noted = False
            family.remember(text)
        else:
            family.streak += 1

    def spend(self, tool_name: str, args: dict[str, Any] | None) -> None:
        """Forget this family's streak because the agent was just told.

        Called when the EXACT guard refuses a call: the model has had the
        message, and letting this control repeat it on the next changed
        argument would turn two controls into a wall.
        """
        family = self._find(tool_name, signature(tool_name, args))
        if family is not None:
            family.streak = 0
            family.noted = False

    # ── bookkeeping ─────────────────────────────────────────────────────

    def _find(self, tool_name: str, tokens: tuple[str, ...] | None) -> _Family | None:
        if tokens is None:
            return None
        for family in self.families:
            if family.tool_name == tool_name and same_family(tool_name, family.tokens, tokens):
                return family
        return None

    def _open(self, tool_name: str, tokens: tuple[str, ...]) -> _Family | None:
        if len(self.families) >= MAX_FAMILIES:
            # Drop the least-used family rather than stop tracking: a run that
            # has touched two hundred things and starts looping is exactly the
            # run this exists for.
            self.families.remove(min(self.families, key=lambda f: f.calls))
        family = _Family(tool_name=tool_name, tokens=tokens)
        self.families.append(family)
        return family
