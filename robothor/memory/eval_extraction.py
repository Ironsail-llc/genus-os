"""How accurate conversation fact extraction is, on a labelled set.

Conversation ingest (``robothor.memory.conversation_ingest``) keeps what a
person says about themselves as facts, each with a trust label, a date and the
turns it came from. This harness runs the real extraction
(:func:`robothor.memory.conversation_facts.extract_conversation_facts`) on hand
labelled conversations (``docs/benchmarks/memory/conversation-facts.yaml``) and
scores what comes back:

    conversation -> extract_conversation_facts -> extracted facts
    expected facts x extracted facts -> judge -> matches -> metrics

Matching uses the memory eval's judge (``robothor.memory.eval_answer``):
deterministic rules first, then its fixed LLM judge prompt for what the rules
cannot settle. The rules: the expected sentence itself is a match; a sentence
naming none of the expected fact's ``keys`` is not. Before any extraction runs
the judge is validated on planted matches built from the labels (each fact's
hand-written paraphrase must match, its hand-written decoy and the other facts
of its conversation and category must not), and the run aborts unless every
planted kind clears ``VALIDATION_THRESHOLD``, as the answer arm does.

A label is ``required`` (counted for recall) or allowed (a true thing worth
keeping that the extraction may leave out: neither missed nor a false
positive). Metrics: precision, recall, trust-label, speaker, source-turn, date
and detail accuracy over matched facts, facts per conversation, duplicates,
extraction errors, and third-party content wrongly stored as the operator's own
(a label with trust ``external`` extracted as ``user_stated``).

No database: the extraction is a model call and a parse.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from robothor.memory.conversation_facts import ConversationTurn, extract_conversation_facts
from robothor.memory.eval import EvalPreconditionError
from robothor.memory.eval_answer import (
    VALIDATION_THRESHOLD,
    Answer,
    JudgeValidation,
    LlmJudge,
    Verdict,
    _contains,
    llm_judge,
    normalise,
)
from robothor.memory.facts import Extraction

logger = logging.getLogger(__name__)

SUITE_ID = "conversation-facts-v1"
DEFAULT_FIXTURES = "docs/benchmarks/memory/conversation-facts.yaml"

#: What each conversation in the labelled set exercises.
CATEGORIES: tuple[str, ...] = (
    "preference",
    "purchase",
    "plan",
    "state_change",
    "dated_absolute",
    "dated_relative",
    "recommendation",
    "pasted",
    "guests",
    "filler",
)

TRUST_LABELS = ("user_stated", "agent_generated", "external")

#: The extraction's speaker role -> the origin the write path stores.
TRUST_OF_ROLE = {"user": "user_stated", "assistant": "agent_generated", "external": "external"}

#: The question the LLM judge is shown for every pair (its prompt is fixed). It
#: names the subject: without that the local judge matched "The user is allergic
#: to peanuts" to "Bob is allergic to peanuts" and failed validation on decoys.
MATCH_QUESTION = (
    "What fact does the conversation record? The answer is correct only if it is about "
    "the same person (the user, the assistant, or the named person) as the reference and "
    "says the same thing about them; the same claim about a different person is wrong."
)

#: At most this many planted matches per kind.
PLANTED_PER_KIND = 200

ExtractFn = Callable[..., Awaitable[Extraction]]
FactJudge = Callable[["ExpectedFact", str], Awaitable[Verdict]]


# ── the labelled set ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExpectedFact:
    """One labelled fact. ``turns`` are 1-based turn numbers it may cite;
    ``dates`` the acceptable event days (``None`` = no date); ``details`` the
    values the fact must keep, each a tuple of acceptable spellings."""

    id: str
    fact: str
    keys: tuple[str, ...]
    speaker: str
    trust: str
    turns: tuple[int, ...]
    dates: tuple[str | None, ...]
    paraphrase: str
    decoy: str
    required: bool = True
    details: tuple[tuple[str, ...], ...] = ()
    #: Every acceptable trust label and speaker (the first is ``trust`` /
    #: ``speaker``): a pasted email the assistant restated may be cited from
    #: the paste or from the restatement.
    trusts: tuple[str, ...] = ()
    speakers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Conversation:
    id: str
    category: str
    turns: tuple[ConversationTurn, ...]
    facts: tuple[ExpectedFact, ...]
    #: Turns that carry someone else's words (a paste, a forward).
    third_party_turns: tuple[int, ...] = ()


def _strs(value: Any, where: str, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{where}: {name} must be a list of non-empty strings")
    return tuple(value)


def _date(value: Any, where: str) -> str | None:
    if value is None:
        return None
    text = str(value)
    try:
        if len(text) != 10:
            raise ValueError(text)
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{where}: date {text!r} is not YYYY-MM-DD or null") from exc
    return text


def _fact(raw: dict[str, Any], where: str, n_turns: int, speakers: set[str]) -> ExpectedFact:
    for name in ("fact", "paraphrase", "decoy"):
        if not isinstance(raw.get(name), str) or not raw[name].strip():
            raise ValueError(f"{where}: {name} is required")
    keys = _strs(raw.get("keys"), where, "keys") if raw.get("keys") else ()
    if not keys:
        raise ValueError(f"{where}: keys must name at least one term")
    raw_trust = raw.get("trust")
    trusts = tuple(raw_trust) if isinstance(raw_trust, list) else (raw_trust,)
    if not trusts or not all(t in TRUST_LABELS for t in trusts):
        raise ValueError(f"{where}: trust {raw_trust!r} is not one of {TRUST_LABELS}")
    raw_speaker = raw.get("speaker")
    who = tuple(str(w) for w in (raw_speaker if isinstance(raw_speaker, list) else [raw_speaker]))
    if not who:
        raise ValueError(f"{where}: speaker must name at least one speaker")
    for w in who:
        if w not in speakers:
            raise ValueError(f"{where}: speaker {w!r} says none of this conversation's turns")
    turns = raw.get("turns")
    if (
        not isinstance(turns, list)
        or not turns
        or not all(isinstance(t, int) and 1 <= t <= n_turns for t in turns)
    ):
        raise ValueError(f"{where}: turns must be turn numbers 1..{n_turns}")
    raw_dates = raw.get("date")
    dates = tuple(
        _date(d, where) for d in (raw_dates if isinstance(raw_dates, list) else [raw_dates])
    )
    details = tuple(_strs(d, where, "details") for d in raw.get("details") or [])
    return ExpectedFact(
        id=where,
        fact=raw["fact"].strip(),
        keys=keys,
        speaker=who[0],
        trust=str(trusts[0]),
        turns=tuple(turns),
        dates=dates or (None,),
        paraphrase=raw["paraphrase"].strip(),
        decoy=raw["decoy"].strip(),
        required=raw.get("required", True) is not False,
        details=details,
        trusts=tuple(str(t) for t in trusts),
        speakers=who,
    )


def turn_speaker(turn: ConversationTurn) -> str:
    """``user`` (the owner), ``assistant``, or a guest's own name."""
    if turn.role == "assistant":
        return "assistant"
    if not turn.author_is_owner:
        return turn.speaker or "another person"
    return "user"


def _time(value: Any, where: str) -> datetime:
    try:
        at = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{where}: at {value!r} is not an ISO date-time") from exc
    if at.tzinfo is None:
        raise ValueError(f"{where}: at must carry a UTC offset")
    return at


def _conversation(raw: Any, n: int) -> Conversation:
    if not isinstance(raw, dict):
        raise ValueError(f"conversation {n} is not a mapping: {raw!r}")
    cid = str(raw.get("id") or "")
    if not cid:
        raise ValueError(f"conversation {n} has no id")
    category = raw.get("category")
    if category not in CATEGORIES:
        raise ValueError(f"{cid}: category {category!r} is not one of {CATEGORIES}")
    if "at" not in raw:
        raise ValueError(f"{cid}: at (when the conversation starts) is required")
    start = _time(raw["at"], cid)
    raw_turns = raw.get("turns") or []
    if not isinstance(raw_turns, list):
        raise ValueError(f"{cid}: turns must be a list")
    turns: list[ConversationTurn] = []
    for n, t in enumerate(raw_turns, start=1):
        if not isinstance(t, dict):
            raise ValueError(f"{cid} turn {n}: not a mapping: {t!r}")
        if t.get("role") not in ("user", "assistant") or not str(t.get("text") or "").strip():
            raise ValueError(f"{cid} turn {n}: role user/assistant and text are required")
        at = _time(t["at"], f"{cid} turn {n}") if t.get("at") else start + timedelta(minutes=n)
        owner = t.get("owner", True) is not False
        turns.append(
            ConversationTurn(
                ref=n,
                role=t["role"],
                text=str(t["text"]).rstrip("\n"),
                at=at,
                speaker=t.get("speaker") if not owner else None,
                author_is_owner=owner,
            )
        )
    if not turns:
        raise ValueError(f"{cid}: no turns")
    speakers = {turn_speaker(t) for t in turns}
    raw_facts = raw.get("facts") or []
    if not isinstance(raw_facts, list) or not all(isinstance(f, dict) for f in raw_facts):
        raise ValueError(f"{cid}: facts must be a list of mappings")
    facts = tuple(
        _fact(f, f"{cid}#{i}", len(turns), speakers) for i, f in enumerate(raw_facts, start=1)
    )
    raw_third = raw.get("third_party_turns") or []
    if not isinstance(raw_third, list) or not all(
        isinstance(t, int) and not isinstance(t, bool) and 1 <= t <= len(turns) for t in raw_third
    ):
        raise ValueError(f"{cid}: third_party_turns must be turn numbers 1..{len(turns)}")
    third = tuple(raw_third)
    return Conversation(cid, category, tuple(turns), facts, third)


def load_fixtures(path: str | Path) -> list[Conversation]:
    """The labelled conversations. Raises ``ValueError`` naming the bad label."""
    import yaml

    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: must be a mapping with version and conversations")
    if data.get("version") != 1:
        raise ValueError(f"{path}: version must be 1")
    raw = data.get("conversations") or []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: conversations must be a list")
    out = [_conversation(c, n) for n, c in enumerate(raw, start=1)]
    seen: set[str] = set()
    for conv in out:
        if conv.id in seen:
            raise ValueError(f"duplicate conversation id {conv.id!r}")
        seen.add(conv.id)
    return out


# ── the judge ──────────────────────────────────────────────────────────────


def trust_of(fact: dict[str, Any]) -> str:
    return TRUST_OF_ROLE.get(str(fact.get("speaker_role")), "agent_generated")


def rule_match(expected: ExpectedFact, text: str) -> Verdict | None:
    """Settle a pair without a model when it can be; ``None`` otherwise."""
    norm = normalise(text)
    if norm == normalise(expected.fact):
        return Verdict(True, False, False, "the expected sentence itself")
    if not any(_contains(norm, k) for k in expected.keys):
        return Verdict(False, False, True, "names none of the fact's keys")
    return None


def _case(expected: ExpectedFact) -> Any:
    from robothor.memory.eval_world import Case

    return Case(
        id=expected.id,
        stratum="extraction",
        question=MATCH_QUESTION,
        gold=(expected.fact,),
        supporting_ids=(),
        as_of=None,
        split="all",
        subject_id="",
    )


def make_fact_judge(llm: LlmJudge | None) -> FactJudge:
    """Rules first, then ``llm`` (the answer arm's judge) for the rest."""

    async def _judge(expected: ExpectedFact, text: str) -> Verdict:
        verdict = rule_match(expected, text)
        if verdict is not None:
            return verdict
        if llm is None:
            return Verdict(False, False, True, "unsettled: no LLM judge", "rule")
        ok, reason = await llm(_case(expected), Answer(text, False, ()))
        return Verdict(ok, False, not ok, reason, "llm")

    return _judge


def local_llm_judge(model: str) -> LlmJudge:
    """The answer arm's LLM judge on a named local model."""

    async def _llm(case: Any, ans: Answer) -> tuple[bool, str]:
        return await llm_judge(case, ans, model=model)

    return _llm


@dataclass(frozen=True)
class PlantedMatch:
    fact: ExpectedFact
    text: str
    expect_match: bool
    #: paraphrase (must match) | decoy | sibling | neighbour (must not)
    kind: str


def planted_matches(conversations: Sequence[Conversation]) -> list[PlantedMatch]:
    """Deterministic planted pairs from the labels themselves."""
    out: dict[str, list[PlantedMatch]] = {}
    by_category: dict[str, list[Conversation]] = {}
    for conv in conversations:
        by_category.setdefault(conv.category, []).append(conv)
    for conv in conversations:
        peers = by_category[conv.category]
        later = peers[(peers.index(conv) + 1) % len(peers)]
        for fact in conv.facts:
            out.setdefault("paraphrase", []).append(
                PlantedMatch(fact, fact.paraphrase, True, "paraphrase")
            )
            out.setdefault("decoy", []).append(PlantedMatch(fact, fact.decoy, False, "decoy"))
            for other in conv.facts:
                if other.id != fact.id:
                    out.setdefault("sibling", []).append(
                        PlantedMatch(fact, other.fact, False, "sibling")
                    )
            if later is not conv:
                for other in later.facts:
                    out.setdefault("neighbour", []).append(
                        PlantedMatch(fact, other.fact, False, "neighbour")
                    )
    return [p for kind in sorted(out) for p in out[kind][:PLANTED_PER_KIND]]


async def validate_fact_judge(
    conversations: Sequence[Conversation], judge: FactJudge
) -> JudgeValidation:
    """Every planted kind, and right and wrong pooled, must clear the bar."""
    by_kind: dict[str, dict[str, int]] = {}
    failures: list[str] = []
    right_ok = wrong_ok = n_right = n_wrong = 0
    for p in planted_matches(conversations):
        try:
            verdict = await judge(p.fact, p.text)
        except Exception as exc:  # noqa: BLE001 — a judge that errors has failed this case
            verdict = Verdict(not p.expect_match, False, False, f"judge raised {exc}", "error")
        ok = verdict.correct == p.expect_match
        agg = by_kind.setdefault(p.kind, {"n": 0, "ok": 0, "llm": 0})
        agg["n"] += 1
        agg["ok"] += int(ok)
        agg["llm"] += int(verdict.method != "rule")
        if p.expect_match:
            n_right += 1
            right_ok += int(ok)
        else:
            n_wrong += 1
            wrong_ok += int(ok)
        if not ok and len(failures) < 20:
            failures.append(f"{p.kind} {p.fact.id}: {p.text!r} -> {verdict.reason}")
    right = right_ok / n_right if n_right else 0.0
    wrong = wrong_ok / n_wrong if n_wrong else 0.0
    return JudgeValidation(
        n_wrong=n_wrong,
        n_right=n_right,
        wrong_marked_wrong=wrong,
        right_marked_right=right,
        passed=right >= VALIDATION_THRESHOLD
        and wrong >= VALIDATION_THRESHOLD
        and all(v["ok"] / v["n"] >= VALIDATION_THRESHOLD for v in by_kind.values() if v["n"]),
        by_kind=by_kind,
        failures=tuple(failures),
    )


async def require_valid_fact_judge(
    conversations: Sequence[Conversation], judge: FactJudge
) -> JudgeValidation:
    result = await validate_fact_judge(conversations, judge)
    if not result.passed:
        kinds = ", ".join(f"{k} {v['ok']}/{v['n']}" for k, v in sorted(result.by_kind.items()))
        raise EvalPreconditionError(
            "judge failed validation: "
            f"{result.wrong_marked_wrong:.1%} of {result.n_wrong} planted wrong matches marked "
            f"wrong and {result.right_marked_right:.1%} of {result.n_right} planted right "
            f"matches marked right (both, and every kind, must be >= "
            f"{VALIDATION_THRESHOLD:.0%}; by kind: {kinds}). "
            f"First failures: {list(result.failures[:5])}"
        )
    return result


# ── scoring ────────────────────────────────────────────────────────────────


@dataclass
class ExpectedOutcome:
    id: str
    required: bool
    trust: str
    #: Someone else's claim: storing it as the operator's is a misattribution.
    external: bool = False
    matched: bool = False
    #: The first extracted fact that matched, by its position.
    by: int | None = None
    trust_ok: bool | None = None
    speaker_ok: bool | None = None
    turns_ok: bool | None = None
    date_ok: bool | None = None
    dated: bool = False
    details_ok: bool | None = None
    misattributed: bool = False
    got_trust: str | None = None
    got_date: str | None = None
    got_speakers: list[str] = field(default_factory=list)


@dataclass
class ExtractedOutcome:
    text: str
    trust: str
    turns: list[int]
    event_time: str | None
    matches: list[str] = field(default_factory=list)
    correct: bool = False
    duplicate: bool = False
    #: Stored as the user's, matched no label, and cites a turn carrying
    #: someone else's words: a possible misattribution, for a reader to check.
    from_third_party_turn: bool = False


@dataclass
class ConversationScore:
    id: str
    category: str
    repeat: int = 0
    error: str | None = None
    expected: list[ExpectedOutcome] = field(default_factory=list)
    extracted: list[ExtractedOutcome] = field(default_factory=list)
    judge_errors: int = 0

    @property
    def misattributed(self) -> int:
        return sum(e.misattributed for e in self.expected)


def _date_ok(expected: ExpectedFact, event_time: Any) -> bool:
    got = str(event_time)[:10] if event_time else None
    return got in expected.dates


async def score_conversation(
    conv: Conversation,
    facts: list[dict[str, Any]],
    judge: FactJudge,
    *,
    repeat: int = 0,
    error: str | None = None,
) -> ConversationScore:
    """Match one conversation's extracted facts to its labels and score each."""
    by_turn = {t.ref: t for t in conv.turns}
    score = ConversationScore(conv.id, conv.category, repeat, error)
    for f in facts:
        score.extracted.append(
            ExtractedOutcome(
                text=str(f.get("fact_text") or ""),
                trust=trust_of(f),
                turns=sorted(int(r) for r in f.get("turn_refs") or []),
                event_time=f.get("event_time"),
            )
        )
    for exp in conv.facts:
        outcome = ExpectedOutcome(
            exp.id,
            exp.required,
            exp.trust,
            external="external" in exp.trusts and "user_stated" not in exp.trusts,
            dated=exp.dates != (None,),
        )
        for i, got in enumerate(score.extracted):
            try:
                verdict = await judge(exp, got.text)
            except Exception as exc:  # noqa: BLE001 — a judge error is no match, and counted
                score.judge_errors += 1
                logger.warning(
                    "extraction judge raised on %s against %r: %s: %s",
                    exp.id,
                    got.text[:120],
                    type(exc).__name__,
                    exc,
                )
                continue
            if not verdict.correct:
                continue
            got.matches.append(exp.id)
            if outcome.matched:
                continue
            speakers = sorted({turn_speaker(by_turn[r]) for r in got.turns if r in by_turn})
            outcome.matched, outcome.by = True, i
            outcome.got_trust, outcome.got_date = got.trust, got.event_time
            outcome.got_speakers = speakers
            outcome.trust_ok = got.trust in exp.trusts
            outcome.speaker_ok = bool(speakers) and set(speakers) <= set(exp.speakers)
            outcome.turns_ok = bool(got.turns) and set(got.turns) <= set(exp.turns)
            outcome.date_ok = _date_ok(exp, got.event_time)
            norm = normalise(got.text)
            outcome.details_ok = (
                all(any(_contains(norm, v) for v in d) for d in exp.details)
                if exp.details
                else None
            )
            outcome.misattributed = (
                "external" in exp.trusts
                and "user_stated" not in exp.trusts
                and got.trust == "user_stated"
            )
        score.expected.append(outcome)
    covered: set[str] = set()
    third = set(conv.third_party_turns)
    for got in score.extracted:
        got.correct = bool(got.matches)
        got.duplicate = got.correct and set(got.matches) <= covered
        covered |= set(got.matches)
        got.from_third_party_turn = (
            not got.correct and got.trust == "user_stated" and bool(third & set(got.turns))
        )
    return score


# ── metrics ────────────────────────────────────────────────────────────────


def _rate(ok: int, n: int) -> float | None:
    return ok / n if n else None


def _metrics(scores: Sequence[ConversationScore]) -> dict[str, Any]:
    required = [e for s in scores for e in s.expected if e.required]
    matched = [e for s in scores for e in s.expected if e.matched]
    extracted = [x for s in scores for x in s.extracted]

    def acc(name: str, rows: list[ExpectedOutcome]) -> tuple[int, int]:
        vals = [getattr(e, name) for e in rows if getattr(e, name) is not None]
        return sum(bool(v) for v in vals), len(vals)

    trust_ok, trust_n = acc("trust_ok", matched)
    speaker_ok, speaker_n = acc("speaker_ok", matched)
    turns_ok, turns_n = acc("turns_ok", matched)
    date_ok, date_n = acc("date_ok", matched)
    dated_ok, dated_n = acc("date_ok", [e for e in matched if e.dated])
    undated_ok, undated_n = acc("date_ok", [e for e in matched if not e.dated])
    details_ok, details_n = acc("details_ok", matched)
    hit = sum(e.matched for e in required)
    correct = sum(x.correct for x in extracted)
    external = [e for s in scores for e in s.expected if e.external and e.matched]
    return {
        "conversations": len(scores),
        "expected": len(required),
        "allowed": sum(not e.required for s in scores for e in s.expected),
        "extracted": len(extracted),
        "matched_expected": hit,
        "correct_extracted": correct,
        "recall": _rate(hit, len(required)) or 0.0,
        "precision": _rate(correct, len(extracted)),
        "trust_accuracy": _rate(trust_ok, trust_n),
        "speaker_accuracy": _rate(speaker_ok, speaker_n),
        "turn_accuracy": _rate(turns_ok, turns_n),
        "date_accuracy": _rate(date_ok, date_n),
        "dated_correct": [dated_ok, dated_n],
        "undated_left_undated": [undated_ok, undated_n],
        "details_kept": [details_ok, details_n],
        "matched_for_attributes": len(matched),
        "facts_per_conversation": len(extracted) / len(scores) if scores else 0.0,
        "filler_facts": sum(len(s.extracted) for s in scores if s.category == "filler"),
        "duplicates": sum(x.duplicate for x in extracted),
        "false_positives": len(extracted) - correct,
        "misattributed_to_operator": sum(s.misattributed for s in scores),
        "third_party_matched": len(external),
        "possible_misattributions": sum(x.from_third_party_turn for x in extracted),
        "extraction_errors": sum(s.error is not None for s in scores),
        "judge_errors": sum(s.judge_errors for s in scores),
    }


def _failures(
    scores: Sequence[ConversationScore], labels: dict[str, ExpectedFact]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in scores:
        if s.error:
            out.append({"kind": "extraction_error", "conversation": s.id, "error": s.error})
        for e in s.expected:
            label = labels[e.id]
            if e.required and not e.matched:
                out.append({"kind": "missed", "fact": e.id, "expected": label.fact})
            if not e.matched:
                continue
            got = s.extracted[e.by].text if e.by is not None else ""
            for name, flag, have, want in (
                ("trust", e.trust_ok, e.got_trust, list(label.trusts)),
                ("speaker", e.speaker_ok, e.got_speakers, list(label.speakers)),
                ("date", e.date_ok, e.got_date, list(label.dates)),
                ("details", e.details_ok, got, [list(d) for d in label.details]),
            ):
                if flag is False:
                    out.append(
                        {
                            "kind": f"wrong_{name}",
                            "fact": e.id,
                            "got": have,
                            "want": want,
                            "extracted": got,
                        }
                    )
            if e.turns_ok is False:
                out.append(
                    {
                        "kind": "wrong_turns",
                        "fact": e.id,
                        "extracted": got,
                        "want": list(label.turns),
                    }
                )
        for x in s.extracted:
            if not x.correct:
                out.append(
                    {
                        "kind": "false_positive",
                        "conversation": s.id,
                        "extracted": x.text,
                        "trust": x.trust,
                        "turns": x.turns,
                        "from_third_party_turn": x.from_third_party_turn,
                    }
                )
            elif x.duplicate:
                out.append({"kind": "duplicate", "conversation": s.id, "extracted": x.text})
    return out


# ── the run ────────────────────────────────────────────────────────────────


#: How long the one-token probe of a named local model may take (a cold load).
PROBE_TIMEOUT_S = 180.0

#: The benchmark marker the measurement runs under (``benchmark_run_scope``).
BENCHMARK_AGENT_ID = "memory-eval-extraction"


async def _probe_local_model(model: str) -> None:
    """One short call to a named local model, straight to Ollama.

    Through the generation module a model that does not answer fails inside
    ``_local_leg``, which pages the operator that memory generation is down
    and names the instance's own model, not this one. So the model is asked
    once here first, the way the answer arm talks to Ollama, and a model that
    does not answer stops the run with a message saying so.
    """
    from robothor.llm import ollama

    try:
        await asyncio.wait_for(
            ollama.chat(
                [{"role": "user", "content": "Reply with OK."}],
                model=model,
                temperature=0.0,
                max_tokens=4,
                think=False,
            ),
            timeout=PROBE_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — any failure means it cannot be measured
        raise EvalPreconditionError(
            f"the local model {model!r} did not answer ({type(exc).__name__}: {exc}); "
            f"check that Ollama is running and the model is pulled (ollama pull {model})"
        ) from exc


def _model_label(model: str | None) -> str:
    from robothor.llm import ollama
    from robothor.memory import generation

    if model:
        return model
    if generation._remote_enabled():
        return f"{generation._provider()}:{generation._remote_model()}"
    return ollama.GENERATION_MODEL


async def run_extraction_eval(
    path: str | Path,
    *,
    model: str | None = None,
    judge_model: str | None = None,
    repeats: int = 1,
    concurrency: int = 2,
    llm: LlmJudge | None = None,
    extract: ExtractFn | None = None,
) -> dict[str, Any]:
    """Validate the judge, extract every conversation ``repeats`` times, score.

    ``model`` runs the extraction on that local model; it is refused while
    remote memory generation is on, which would answer with its own model
    instead. Without it the generation module's model answers, as in ingest.
    The judge always runs on a named local model (``judge_model``, default the
    local generation model). Each named model is probed once before anything
    else (:func:`_probe_local_model`). Everything runs as benchmark traffic
    (``benchmark_run_scope``): an alert raised on the way, such as "memory
    generation is down", goes to the benchmark digest, never to the operator.
    ``llm`` and ``extract`` are for tests (an injected one is not probed).
    """
    from robothor.engine.run_context import benchmark_run_scope

    with benchmark_run_scope(True, agent_id=BENCHMARK_AGENT_ID):
        return await _run_extraction_eval(
            path,
            model=model,
            judge_model=judge_model,
            repeats=repeats,
            concurrency=concurrency,
            llm=llm,
            extract=extract,
        )


async def _run_extraction_eval(
    path: str | Path,
    *,
    model: str | None,
    judge_model: str | None,
    repeats: int,
    concurrency: int,
    llm: LlmJudge | None,
    extract: ExtractFn | None,
) -> dict[str, Any]:
    from robothor.llm import ollama
    from robothor.memory import generation

    if model and generation._remote_enabled():
        raise EvalPreconditionError(
            "remote memory generation is enabled; it ignores a named model, so "
            "--extraction-model cannot be measured. Unset the remote provider for this run, "
            "or leave --extraction-model out to measure the remote model"
        )
    conversations = load_fixtures(path)
    labels = {f.id: f for c in conversations for f in c.facts}
    judge_name = judge_model or ollama.GENERATION_MODEL
    if model and extract is None:
        await _probe_local_model(model)
    if llm is None:
        await _probe_local_model(judge_name)
    judge = make_fact_judge(llm if llm is not None else local_llm_judge(judge_name))
    started = time.perf_counter()
    validation = await require_valid_fact_judge(conversations, judge)
    validated_s = time.perf_counter() - started

    run_extract = extract or extract_conversation_facts
    gate = asyncio.Semaphore(max(1, concurrency))
    fallbacks_before = generation.remote_fallback_count

    async def one(conv: Conversation, repeat: int) -> ConversationScore:
        async with gate:
            result = await run_extract(list(conv.turns), model=model)
        return await score_conversation(
            conv, result.facts, judge, repeat=repeat, error=result.error
        )

    extract_started = time.perf_counter()
    scores = list(
        await asyncio.gather(*(one(c, r) for r in range(max(1, repeats)) for c in conversations))
    )
    extract_s = time.perf_counter() - extract_started

    by_category = {
        cat: _metrics([s for s in scores if s.category == cat])
        for cat in CATEGORIES
        if any(s.category == cat for s in scores)
    }
    per_repeat = [
        {
            k: v
            for k, v in _metrics([s for s in scores if s.repeat == r]).items()
            if k in ("recall", "precision", "trust_accuracy", "date_accuracy", "extracted")
        }
        for r in range(max(1, repeats))
    ]
    return {
        "suite_id": SUITE_ID,
        "fixtures": str(path),
        "model": _model_label(model),
        "judge_model": judge_name if llm is None else "injected",
        "conversations": len(conversations),
        "repeats": max(1, repeats),
        "metrics": _metrics(scores),
        "by_category": by_category,
        "per_repeat": per_repeat,
        "judge_validation": asdict(validation),
        "failures": _failures(scores, labels),
        "remote_fallbacks": generation.remote_fallback_count - fallbacks_before,
        "seconds": {"judge_validation": round(validated_s, 1), "extraction": round(extract_s, 1)},
        "scores": [asdict(s) for s in scores],
    }


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _pair(value: Sequence[int]) -> str:
    ok, n = value
    return f"{ok}/{n}" + (f" ({ok / n:.1%})" if n else "")


def format_extraction_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    v = report["judge_validation"]
    lines = [
        f"## Conversation fact extraction: {report['model']}",
        "",
        f"{report['conversations']} conversations x {report['repeats']} run(s); "
        f"{m['expected']} required and {m['allowed']} allowed labels; "
        f"{m['extracted']} facts extracted. Judge: {report['judge_model']}.",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Precision | {_pct(m['precision'])} ({m['correct_extracted']}/{m['extracted']}) |",
        f"| Recall | {_pct(m['recall'])} ({m['matched_expected']}/{m['expected']}) |",
        f"| Trust label accuracy | {_pct(m['trust_accuracy'])} |",
        f"| Speaker accuracy | {_pct(m['speaker_accuracy'])} |",
        f"| Source-turn accuracy | {_pct(m['turn_accuracy'])} |",
        f"| Date accuracy | {_pct(m['date_accuracy'])} |",
        f"| Dated facts dated right | {_pair(m['dated_correct'])} |",
        f"| Undated facts left undated | {_pair(m['undated_left_undated'])} |",
        f"| Details kept (lists, old values) | {_pair(m['details_kept'])} |",
        f"| Facts per conversation | {m['facts_per_conversation']:.2f} |",
        f"| Facts from filler conversations | {m['filler_facts']} |",
        f"| Duplicates | {m['duplicates']} |",
        f"| Third-party facts stored as the operator's | {m['misattributed_to_operator']} "
        f"of {m['third_party_matched']} matched |",
        f"| Unlabelled user-stated facts from relaying turns | {m['possible_misattributions']} |",
        f"| Extraction errors | {m['extraction_errors']} |",
        f"| Judge errors | {m['judge_errors']} |",
        "",
        "Attribute accuracies are over the "
        f"{m['matched_for_attributes']} labels that were matched.",
        "",
        "| Category | Recall | Precision | Trust | Date | Extracted |",
        "|----------|--------|-----------|-------|------|-----------|",
    ]
    for cat, c in report["by_category"].items():
        lines.append(
            f"| {cat} | {_pct(c['recall']) if c['expected'] else 'n/a'} | "
            f"{_pct(c['precision'])} | {_pct(c['trust_accuracy'])} | "
            f"{_pct(c['date_accuracy'])} | {c['extracted']} |"
        )
    lines += [
        "",
        f"Judge validation: {v['right_marked_right']:.1%} of {v['n_right']} planted right "
        f"matches marked right, {v['wrong_marked_wrong']:.1%} of {v['n_wrong']} planted wrong "
        "matches marked wrong.",
        "",
        "| Planted kind | Correct | Reached the model |",
        "|--------------|---------|-------------------|",
    ]
    for kind, k in sorted(v["by_kind"].items()):
        lines.append(f"| {kind} | {k['ok']}/{k['n']} | {k.get('llm', 0)} |")
    if len(report["per_repeat"]) > 1:
        lines += ["", "| Run | Recall | Precision | Trust | Date |", "|---|---|---|---|---|"]
        for i, r in enumerate(report["per_repeat"], start=1):
            lines.append(
                f"| {i} | {_pct(r['recall'])} | {_pct(r['precision'])} | "
                f"{_pct(r['trust_accuracy'])} | {_pct(r['date_accuracy'])} |"
            )
    return "\n".join(lines) + "\n"


__all__ = [
    "CATEGORIES",
    "DEFAULT_FIXTURES",
    "SUITE_ID",
    "Conversation",
    "ConversationScore",
    "ExpectedFact",
    "PlantedMatch",
    "format_extraction_markdown",
    "load_fixtures",
    "make_fact_judge",
    "planted_matches",
    "require_valid_fact_judge",
    "rule_match",
    "run_extraction_eval",
    "score_conversation",
    "trust_of",
    "turn_speaker",
    "validate_fact_judge",
]
