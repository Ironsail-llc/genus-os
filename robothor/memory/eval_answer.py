"""The memory eval's answer-and-judge arm.

Scoring "the gold string appears in a retrieved row" cannot measure
abstention, cannot compare against a full-context baseline, and flatters a
system that retrieves a lot. This arm scores answers instead:

    system.retrieve(case)  ->  ContextBundle at a shared token budget
    answer(question, bundle) ->  Answer(text, abstained, cited_ids)
    judge(case, answer)      ->  Verdict(correct, abstained, confident_wrong)

The judge is deterministic first: a normalised whole-word match against the
case's gold list, the other tenant's values (always wrong), abstention rules,
and any other value the world knows about (wrong). Only an answer those rules
cannot settle — a free-text paraphrase — goes to an LLM judge whose prompt is
fixed here. The judge is validated on planted answers generated from the world
before any case runs, and the run aborts if it marks fewer than 95% of planted
wrong answers wrong, or fewer than 95% of planted correct answers right.

Metrics per system and stratum: accuracy with a Wilson interval,
confident-wrong rate, abstention precision and recall, context tokens,
retrieval and answer latency, run-to-run spread over ``repeats``, and the
minimum detectable effect at the stratum's n. One deciding comparison per
report: pooled hard strata, system against a named reference, through the
existing McNemar ``promotion_verdict`` plus a 5-point non-inferiority check.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from robothor.memory import eval_stats as st
from robothor.memory.eval import EvalPreconditionError
from robothor.memory.eval_systems import ContextBundle, WorldHandle, make_systems
from robothor.memory.eval_world_seed import (
    WORLD_TENANT,
    _require_eval_tenants,
    cleanup_world,
    seed_world,
    shadow_tenant_for,
)

if TYPE_CHECKING:
    from robothor.memory.eval_world import Case, World

logger = logging.getLogger(__name__)

ANSWERS_SUITE_ID = "memory-world-answers-v1"

#: The strata the deciding comparison pools. cross_tenant and long_tenure are
#: reported but do not decide: one is a safety property, the other repeats
#: knowledge_update at different lengths.
HARD_STRATA: tuple[str, ...] = (
    "multi_hop",
    "entity_confusion",
    "point_in_time",
    "knowledge_update",
    "abstention",
    "raw_detail",
)

VALIDATION_THRESHOLD = 0.95
#: Bumped whenever a judge rule changes, so a progress file written under
#: the old rules is refused instead of mixing two judges' verdicts.
JUDGE_VERSION = 3
DEFAULT_REFERENCE = "keyword_only"
NON_INFERIORITY_MARGIN = 0.05
DEFAULT_TOKEN_BUDGET = 2048
_PLANTED_PER_KIND = 150

ChatFn = Callable[..., Awaitable[str]]


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Answer:
    text: str
    abstained: bool
    cited_ids: tuple[str, ...]
    latency_ms: float = 0.0
    model: str = ""


@dataclass(frozen=True)
class Verdict:
    correct: bool
    abstained: bool
    #: Answered (did not abstain) and was wrong.
    confident_wrong: bool
    reason: str
    #: ``rule``, ``llm`` or ``error``.
    method: str = "rule"


@dataclass(frozen=True)
class CaseRecord:
    system: str
    case_id: str
    stratum: str
    repeat: int
    correct: bool
    abstained: bool
    confident_wrong: bool
    tokens: int
    retrieval_ms: float
    answer_ms: float
    equal_budget: bool
    judge_method: str
    answer: str = ""
    cited_ids: tuple[str, ...] = ()
    reason: str = ""


# --------------------------------------------------------------------------- #
# Answering
# --------------------------------------------------------------------------- #

ANSWER_SYSTEM_PROMPT = (
    "You answer a question using ONLY the context items provided below. "
    "Do not use outside knowledge and do not guess. "
    'If the context does not contain the answer, reply with exactly "unknown" '
    "and set unknown to true. "
    "When items disagree, prefer the most recent one unless the question asks "
    "about a specific date. "
    "Give the shortest answer that fully answers the question (a name, place, "
    "organisation, title or value). "
    "Cite the ids of the context items you used. "
    'Reply as JSON: {"answer": "...", "unknown": true|false, "cited_ids": ["..."]}.'
)

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "unknown": {"type": "boolean"},
        "cited_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "unknown", "cited_ids"],
}


def build_answer_messages(
    question: str, bundle: ContextBundle, as_of: str | None = None
) -> list[dict[str, str]]:
    lines = [f"[{i.id}] ({i.kind}) {i.text}" for i in bundle.items]
    context = "\n".join(lines) if lines else "(no context items)"
    user = f"Context:\n{context}\n\nQuestion: {question}"
    if as_of:
        user += f"\nThe question asks about the state of things on {as_of}."
    return [
        {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalise(text: str) -> str:
    """Lower-case, punctuation to spaces, whitespace collapsed."""
    return _NON_WORD.sub(" ", (text or "").lower()).strip()


def _contains(haystack_norm: str, needle: str) -> bool:
    n = normalise(needle)
    return bool(n) and f" {n} " in f" {haystack_norm} "


def _says_unknown(text: str) -> bool:
    words = normalise(text).split()
    return "unknown" in words and len(words) <= 12


def parse_answer(raw: str) -> tuple[str, bool, tuple[str, ...]]:
    from robothor.memory.generation import strip_think_blocks

    cleaned = strip_think_blocks(raw or "").strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict):
        text = str(data.get("answer", "")).strip()
        abstained = bool(data.get("unknown")) or _says_unknown(text)
        cited = tuple(str(c) for c in data.get("cited_ids") or [])
        return text, abstained, cited
    return cleaned, _says_unknown(cleaned), ()


def _default_answer_model() -> str:
    from robothor.llm import ollama
    from robothor.memory import generation

    if generation._remote_enabled():
        return f"{generation._provider()}:{generation._remote_model()}"
    return ollama.GENERATION_MODEL


def resolve_eval_models(answer_model: str | None, judge_model: str | None) -> tuple[str, str]:
    """The local models that will answer and judge, named before anything runs.

    An explicit model always runs on the local Ollama client: the memory
    generation module ignores ``model`` on its remote path, and silently falls
    back to local per call when the remote fails, so through it a run could be
    labelled with one model and answered by another. With remote memory
    generation on, both models must therefore be given explicitly.
    """
    from robothor.llm import ollama
    from robothor.memory import generation

    if generation._remote_enabled() and (answer_model is None or judge_model is None):
        raise EvalPreconditionError(
            "remote memory generation is enabled "
            f"({generation._provider()}:{generation._remote_model()}); pass both "
            "--answer-model and --judge-model so the eval runs on named local models "
            "instead of a provider that may fall back to local per call"
        )
    default = ollama.GENERATION_MODEL
    return (answer_model or default, judge_model or default)


def _chat_for(model: str | None) -> ChatFn:
    """Local Ollama for an explicit model; the generation module otherwise."""
    if model:
        from robothor.llm import ollama

        return ollama.chat
    from robothor.memory import generation

    return generation.chat


async def answer(
    question: str,
    bundle: ContextBundle,
    *,
    as_of: str | None = None,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 256,
    chat: ChatFn | None = None,
) -> Answer:
    """Answer ``question`` from ``bundle`` alone, through the generation module."""
    if bundle.budget is not None and bundle.tokens > bundle.budget:
        raise ValueError(f"context of {bundle.tokens} tokens exceeds its budget of {bundle.budget}")
    if chat is None:
        chat = _chat_for(model)
    started = time.perf_counter()
    raw = await chat(
        build_answer_messages(question, bundle, as_of),
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        think=False,
        format=ANSWER_SCHEMA,
    )
    text, abstained, cited = parse_answer(raw)
    return Answer(
        text=text,
        abstained=abstained,
        cited_ids=cited,
        latency_ms=(time.perf_counter() - started) * 1000,
        model=model or _default_answer_model(),
    )


# --------------------------------------------------------------------------- #
# Judging
# --------------------------------------------------------------------------- #


def known_values(world: World) -> frozenset[str]:
    """Every answer-shaped value in the world and its shadow tenant."""
    out: set[str] = set()
    for w in (world, world.shadow):
        if w is None:
            continue
        out |= {o.name for o in w.organisations}
        out |= {loc.name for loc in w.locations}
        out |= {p.name for p in w.projects}
        out |= {e.name for e in w.events}
        out |= {p.full_name for p in w.people} | {p.email for p in w.people}
        out |= {e.role for e in w.employments}
        out |= {r.value for r in w.raw_details}
    return frozenset(out)


def known_kinds(world: World) -> dict[str, str]:
    """What kind of thing each known value is (organisation, city, person, ...)."""
    out: dict[str, str] = {}
    for w in (world, world.shadow):
        if w is None:
            continue
        for kind, values in (
            ("organisation", [o.name for o in w.organisations]),
            ("city", [loc.name for loc in w.locations]),
            ("project", [p.name for p in w.projects]),
            ("event", [e.name for e in w.events]),
            ("person", [p.full_name for p in w.people] + [p.email for p in w.people]),
            ("role", [e.role for e in w.employments]),
            ("detail", [r.value for r in w.raw_details]),
        ):
            for v in values:
                out.setdefault(v, kind)
    return out


def judge_rules(
    case: Case,
    ans: Answer,
    known: frozenset[str],
    kinds: dict[str, str] | None = None,
) -> Verdict | None:
    """Settle what can be settled without a model; ``None`` otherwise.

    A gold match counts only when the answer names no other known value of the
    gold's own kind. "Kelvaro Systems or Brivane Labs" is a hedge, or a
    superseded value stated beside the current one, and is confident-wrong: the
    rubric says an answer that hedges between several things is wrong. This is
    settled by rule because the local LLM judge accepted 55 of 150 planted
    hedges when they were left to it. "Kelvaro Systems, in Velmere" names a
    city beside an organisation and stays correct. Without ``kinds`` every
    known value counts as the same kind, which is the cautious reading.
    """
    if case.stratum == "abstention":
        if ans.abstained:
            return Verdict(True, True, False, "abstained where nothing is known")
        return Verdict(False, False, True, "answered a question with no supporting memory")
    if ans.abstained:
        return Verdict(False, True, False, "abstained on an answerable question")
    text = normalise(ans.text)
    for value in case.forbidden:
        if _contains(text, value):
            return Verdict(False, False, True, f"forbidden value from another tenant: {value!r}")
    golds = {normalise(g) for g in case.gold}
    for gold in case.gold:
        if _contains(text, gold):
            gold_kinds = {kinds.get(g) for g in case.gold} if kinds else set()
            for value in sorted(known):
                v = normalise(value)
                if v in golds or any(v in g for g in golds) or not _contains(text, value):
                    continue
                if kinds is None or kinds.get(value) in gold_kinds:
                    return Verdict(
                        False, False, True, f"hedge: names {gold!r} beside the rival {value!r}"
                    )
            return Verdict(True, False, False, f"matches gold {gold!r}")
    for value in sorted(known):
        if normalise(value) not in golds and _contains(text, value):
            return Verdict(False, False, True, f"names a different value: {value!r}")
    return None


JUDGE_PROMPT = (
    "You grade one answer to a question against the reference answer(s).\n"
    "Question: {question}\n"
    "Reference answer(s), any one is correct: {gold}\n"
    "Answer to grade: {answer}\n\n"
    "The answer is correct only if it names the same thing as a reference answer, "
    "possibly in different words. It is wrong if it names something else, hedges "
    "between several things, or adds a claim that contradicts the reference. "
    'Reply as JSON: {{"correct": true|false, "reason": "..."}}.'
)

_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"correct": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["correct", "reason"],
}

LlmJudge = Callable[["Case", Answer], Awaitable[tuple[bool, str]]]
JudgeFn = Callable[["Case", Answer], Awaitable[Verdict]]


async def llm_judge(
    case: Case, ans: Answer, *, model: str | None = None, chat: ChatFn | None = None
) -> tuple[bool, str]:
    """The model judge, for answers the rules cannot settle."""
    if chat is None:
        chat = _chat_for(model)
    prompt = JUDGE_PROMPT.format(
        question=case.question, gold=" | ".join(case.gold), answer=ans.text
    )
    raw = await chat(
        [{"role": "user", "content": prompt}],
        model=model,
        temperature=0.0,
        max_tokens=200,
        think=False,
        format=_JUDGE_SCHEMA,
    )
    from robothor.memory.generation import strip_think_blocks

    try:
        data = json.loads(strip_think_blocks(raw).strip())
        return bool(data.get("correct")), f"llm: {data.get('reason', '')}"[:300]
    except (json.JSONDecodeError, AttributeError) as exc:
        raise RuntimeError(f"LLM judge returned unparseable output: {raw[:200]!r}") from exc


async def judge(
    case: Case,
    ans: Answer,
    *,
    known_values: frozenset[str] = frozenset(),
    kinds: dict[str, str] | None = None,
    llm: LlmJudge | None = None,
) -> Verdict:
    verdict = judge_rules(case, ans, known_values, kinds)
    if verdict is not None:
        return verdict
    if llm is None:
        return Verdict(False, False, True, "unsettled: no gold match and no LLM judge", "rule")
    ok, reason = await llm(case, ans)
    return Verdict(ok, False, not ok, reason, "llm")


def make_judge(world: World, *, llm: LlmJudge | None) -> JudgeFn:
    known = known_values(world)
    kinds = known_kinds(world)

    async def _judge(case: Case, ans: Answer) -> Verdict:
        return await judge(case, ans, known_values=known, kinds=kinds, llm=llm)

    return _judge


# --------------------------------------------------------------------------- #
# Judge validation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlantedAnswer:
    case: Case
    answer: Answer
    expect_correct: bool
    #: confuser | superseded | other_tenant | random_entity | abstention_answered
    #: | paraphrase | abstained
    kind: str


@dataclass(frozen=True)
class JudgeValidation:
    n_wrong: int
    n_right: int
    wrong_marked_wrong: float
    right_marked_right: float
    passed: bool
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    failures: tuple[str, ...] = ()


_WRONG_TEMPLATES = (
    "{v}",
    "It is {v}.",
    "I believe the answer is {v}.",
    "{v}, according to the records.",
)
_RIGHT_TEMPLATES = (
    "{v}",
    "The answer is {v}.",
    "{upper}",
    "That would be {v}, as far as the notes go.",
    "{lower}",
)


def planted_answers(world: World) -> list[PlantedAnswer]:
    """Planted wrong and right answers, generated deterministically from the world."""
    rng = random.Random(world.seed * 7 + 3)
    people = {p.id: p for p in world.people}
    jobs: dict[str, list[Any]] = {}
    for e in world.employments:
        jobs.setdefault(e.person_id, []).append(e)
    homes: dict[str, list[Any]] = {}
    for r in world.residences:
        homes.setdefault(r.person_id, []).append(r)
    managers = {r.person_id: r.manager_id for r in world.reporting_lines if r.end is None}
    orgs = sorted(o.name for o in world.organisations)
    cities = sorted(loc.name for loc in world.locations)
    names = sorted(p.full_name for p in world.people)
    roles = sorted({e.role for e in world.employments})
    org_names, city_names, person_names = set(orgs), set(cities), set(names)

    def _clean(case: Case, value: str) -> bool:
        v = normalise(value)
        return bool(v) and not any(normalise(g) in v or v in normalise(g) for g in case.gold)

    out: dict[str, list[PlantedAnswer]] = {}

    def _plant(kind: str, case: Case, value: str, correct: bool, n: int) -> None:
        if not correct and not _clean(case, value):
            return
        templates = _RIGHT_TEMPLATES if correct else _WRONG_TEMPLATES
        text = templates[n % len(templates)].format(
            v=value, upper=value.upper(), lower=value.lower()
        )
        out.setdefault(kind, []).append(PlantedAnswer(case, Answer(text, False, ()), correct, kind))

    for n, case in enumerate(world.cases):
        subject = people.get(case.subject_id)
        if case.stratum == "abstention":
            out.setdefault("abstained", []).append(
                PlantedAnswer(case, Answer("unknown", True, ()), True, "abstained")
            )
            _plant("abstention_answered", case, rng.choice(roles), False, n)
            continue
        _plant("paraphrase", case, case.gold[0], True, n)
        if case.stratum == "entity_confusion" and case.confuser_id:
            if case.id.endswith(":manager"):
                boss = managers.get(case.confuser_id)
                if boss:
                    _plant("confuser", case, people[boss].full_name, False, n)
            else:
                _plant("confuser", case, jobs[case.confuser_id][-1].role, False, n)
        if case.stratum == "knowledge_update" and subject:
            _plant("superseded", case, orgs_of(jobs[subject.id][-2].org_id, world), False, n)
        if case.stratum == "long_tenure" and subject and len(homes[subject.id]) >= 2:
            prev = homes[subject.id][-2].location_id
            _plant(
                "superseded",
                case,
                next(loc.name for loc in world.locations if loc.id == prev),
                False,
                n,
            )
        if case.stratum == "cross_tenant" and case.forbidden:
            _plant("other_tenant", case, case.forbidden[0], False, n)
        gold = case.gold[0]
        pool = (
            orgs
            if gold in org_names
            else cities
            if gold in city_names
            else names
            if gold in person_names
            else roles
        )
        others = [v for v in pool if _clean(case, v)]
        if others:
            _plant("random_entity", case, rng.choice(others), False, n)
            # A hedge: the gold beside a rival of its own kind. Expected wrong.
            rival = rng.choice(others)
            text = (f"{gold} or {rival}", f"{rival} (previously {gold})")[n % 2]
            out.setdefault("hedge", []).append(
                PlantedAnswer(case, Answer(text, False, ()), False, "hedge")
            )
        # Forms only a model can grade: "Last, First" for a person and the bare
        # root of an organisation's name. Neither contains a gold string or a
        # known value, so they reach the LLM judge and test it.
        short = _llm_form(gold, org_names, person_names)
        if short:
            out.setdefault("paraphrase_llm", []).append(
                PlantedAnswer(case, Answer(short, False, ()), True, "paraphrase_llm")
            )
            gold_words = {w for g in case.gold for w in normalise(g).split()}
            wrong_pool = [
                w
                for v in others
                if (w := _llm_form(v, org_names, person_names))
                and _clean(case, w)
                and not _near_any(normalise(w).split(), gold_words)
            ]
            if wrong_pool:
                out.setdefault("wrong_llm", []).append(
                    PlantedAnswer(
                        case, Answer(rng.choice(wrong_pool), False, ()), False, "wrong_llm"
                    )
                )

    planted: list[PlantedAnswer] = []
    for kind in sorted(out):
        planted.extend(out[kind][:_PLANTED_PER_KIND])
    return planted


def _near_any(words: Sequence[str], gold_words: set[str]) -> bool:
    """True when any word equals or nearly spells a gold word.

    Invented roots can differ by one letter (Drodrel / Drotrel), and a planted
    wrong answer that is a near-spelling of the gold is not wrong to a careful
    reader; the judge must not be failed for agreeing.
    """
    from difflib import SequenceMatcher

    return any(SequenceMatcher(None, w, g).ratio() >= 0.75 for w in words for g in gold_words)


def _llm_form(value: str, org_names: set[str], person_names: set[str]) -> str | None:
    words = value.split()
    if value in person_names and len(words) == 2:
        return f"{words[1]}, {words[0]}"
    if value in org_names and len(words) >= 2:
        return words[0]
    return None


def orgs_of(org_id: str, world: World) -> str:
    return next(o.name for o in world.organisations if o.id == org_id)


async def validate_judge(world: World, judge_fn: JudgeFn) -> JudgeValidation:
    planted = planted_answers(world)
    by_kind: dict[str, dict[str, int]] = {}
    failures: list[str] = []
    wrong_ok = right_ok = n_wrong = n_right = 0
    for p in planted:
        try:
            verdict = await judge_fn(p.case, p.answer)
        except Exception as exc:  # noqa: BLE001 — a judge that errors has failed this case
            verdict = Verdict(
                not p.expect_correct, False, False, f"judge raised {type(exc).__name__}: {exc}"
            )
        ok = verdict.correct == p.expect_correct
        agg = by_kind.setdefault(p.kind, {"n": 0, "ok": 0})
        agg["n"] += 1
        agg["ok"] += int(ok)
        if p.expect_correct:
            n_right += 1
            right_ok += int(ok)
        else:
            n_wrong += 1
            wrong_ok += int(ok)
        if not ok and len(failures) < 20:
            failures.append(f"{p.kind} {p.case.id}: {p.answer.text!r} -> {verdict.reason}")
    wrong_rate = wrong_ok / n_wrong if n_wrong else 0.0
    right_rate = right_ok / n_right if n_right else 0.0
    return JudgeValidation(
        n_wrong=n_wrong,
        n_right=n_right,
        wrong_marked_wrong=wrong_rate,
        right_marked_right=right_rate,
        # Every planted kind must clear the bar on its own: most planted
        # answers are settled by rules, and pooled alone they would carry a
        # weak model judge through.
        passed=wrong_rate >= VALIDATION_THRESHOLD
        and right_rate >= VALIDATION_THRESHOLD
        and all(v["ok"] / v["n"] >= VALIDATION_THRESHOLD for v in by_kind.values() if v["n"]),
        by_kind=by_kind,
        failures=tuple(failures),
    )


async def require_valid_judge(world: World, judge_fn: JudgeFn) -> JudgeValidation:
    result = await validate_judge(world, judge_fn)
    if not result.passed:
        by_kind = ", ".join(f"{k} {v['ok']}/{v['n']}" for k, v in sorted(result.by_kind.items()))
        raise EvalPreconditionError(
            "judge failed validation: "
            f"{result.wrong_marked_wrong:.1%} of {result.n_wrong} planted wrong answers marked "
            f"wrong and {result.right_marked_right:.1%} of {result.n_right} planted correct "
            f"answers marked right (both, and every planted kind, must be >= "
            f"{VALIDATION_THRESHOLD:.0%}; by kind: {by_kind}). "
            f"First failures: {list(result.failures[:5])}"
        )
    return result


# --------------------------------------------------------------------------- #
# Case selection and metrics
# --------------------------------------------------------------------------- #


def select_cases(world: World, split: str, limit_per_stratum: int | None) -> list[Case]:
    from robothor.memory.eval_world import STRATA, world_cases

    chosen: list[Case] = []
    for stratum in STRATA:
        cases = [c for c in world_cases(world, split) if c.stratum == stratum]
        if limit_per_stratum is not None:
            cases = sorted(cases, key=lambda c: hashlib.sha256(c.id.encode()).hexdigest())
            cases = cases[: max(0, limit_per_stratum)]
        chosen.extend(sorted(cases, key=lambda c: c.id))
    return chosen


def case_outcomes(recs: Sequence[CaseRecord]) -> dict[str, bool]:
    """One outcome per case: the majority over its repeats, ties to the first.

    Repeats at temperature 0 are near-copies. Counting each as its own
    observation would multiply the evidence by the number of repeats and make
    every interval and test look sharper than the cases allow.
    """
    by_case: dict[str, list[CaseRecord]] = {}
    for r in recs:
        by_case.setdefault(r.case_id, []).append(r)
    out: dict[str, bool] = {}
    for cid, rs in by_case.items():
        rs = sorted(rs, key=lambda r: r.repeat)
        hits = sum(r.correct for r in rs)
        out[cid] = rs[0].correct if hits * 2 == len(rs) else hits * 2 > len(rs)
    return out


def _metrics(recs: list[CaseRecord]) -> dict[str, Any]:
    records = len(recs)
    outcomes = case_outcomes(recs)
    n = len(outcomes)
    correct = sum(outcomes.values())
    acc = correct / n if n else 0.0
    abstained = [r for r in recs if r.abstained]
    abst_cases = [r for r in recs if r.stratum == "abstention"]
    by_repeat: dict[int, list[CaseRecord]] = {}
    for r in recs:
        by_repeat.setdefault(r.repeat, []).append(r)
    per_run = [sum(x.correct for x in by_repeat[k]) / len(by_repeat[k]) for k in sorted(by_repeat)]
    retr = [r.retrieval_ms for r in recs]
    ans = [r.answer_ms for r in recs]
    lo, hi = st.wilson_interval(correct, n)
    # Accuracy, its interval and the MDE are per case. The rates below are per
    # answer (records = cases x repeats); with one repeat the two agree.
    return {
        "n": n,
        "cases": n,
        "runs": len(by_repeat),
        "records": records,
        "correct": correct,
        "accuracy": acc,
        "wilson": [lo, hi],
        "confident_wrong_rate": (
            (sum(r.confident_wrong for r in recs) / records) if records else 0.0
        ),
        "abstained_rate": (len(abstained) / records) if records else 0.0,
        "abstention_precision": (
            sum(r.stratum == "abstention" for r in abstained) / len(abstained)
            if abstained
            else None
        ),
        "abstention_recall": (
            sum(r.abstained for r in abst_cases) / len(abst_cases) if abst_cases else None
        ),
        "mean_tokens": (sum(r.tokens for r in recs) / records) if records else 0.0,
        "retrieval_p50_ms": st.percentile(retr, 50),
        "retrieval_p95_ms": st.percentile(retr, 95),
        "answer_p50_ms": st.percentile(ans, 50),
        "answer_p95_ms": st.percentile(ans, 95),
        "answer_mean_ms": (sum(ans) / records) if records else 0.0,
        "llm_judged": sum(r.judge_method == "llm" for r in recs),
        "errors": sum(r.judge_method == "error" for r in recs),
        "mde": st.minimum_detectable_effect(n, p=acc),
        "spread": st.run_spread(per_run),
    }


def summarise(records: Sequence[CaseRecord]) -> dict[str, dict[str, Any]]:
    """Metrics per system, per stratum, plus ``all`` and ``hard`` pools."""
    out: dict[str, dict[str, Any]] = {}
    systems = sorted({r.system for r in records})
    for system in systems:
        recs = [r for r in records if r.system == system]
        strata: dict[str, Any] = {}
        for stratum in sorted({r.stratum for r in recs}):
            strata[stratum] = _metrics([r for r in recs if r.stratum == stratum])
        strata["all"] = _metrics(recs)
        strata["hard"] = _metrics([r for r in recs if r.stratum in HARD_STRATA])
        out[system] = strata
    return out


def compare(records: Sequence[CaseRecord], *, system: str, reference: str) -> dict[str, Any]:
    """The deciding comparison: pooled hard strata, ``system`` vs ``reference``."""

    def _arm(name: str) -> dict[str, bool]:
        # One pair per case, never per repeat: see case_outcomes.
        return case_outcomes([r for r in records if r.system == name and r.stratum in HARD_STRATA])

    before, after = _arm(reference), _arm(system)
    strata = {r.case_id: r.stratum for r in records if r.stratum in HARD_STRATA}
    paired = st.paired_report(before, after, strata)
    promote, verdict = st.promotion_verdict(paired)
    return {
        "system": system,
        "reference": reference,
        "strata": list(HARD_STRATA),
        "paired": paired,
        "promote": promote,
        "verdict": verdict,
        "non_inferiority": st.non_inferiority(before, after, margin=NON_INFERIORITY_MARGIN),
    }


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #

AnswerFn = Callable[..., Awaitable[Answer]]


async def run_answers(
    seed: int,
    size: str,
    split: str,
    *,
    systems: Sequence[str] = (
        "recall",
        "recall_budget",
        "keyword_only",
        "raw_verbatim",
        "full_context",
    ),
    reference: str | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    repeats: int = 1,
    answer_model: str | None = None,
    judge_model: str | None = None,
    answer_temperature: float = 0.0,
    limit_per_stratum: int | None = None,
    tenant_id: str = WORLD_TENANT,
    concurrency: int = 4,
    cache_dir: str | None = None,
    cleanup: bool = True,
    answer_fn: AnswerFn | None = None,
    embed_batch: Callable[[list[str]], Awaitable[list[list[float]]]] | None = None,
    progress_path: str | None = None,
) -> dict[str, Any]:
    """Seed the world once, answer and judge every selected case per system.

    With ``progress_path``, every judged case is appended to that JSONL as it
    finishes, and a restart with the same parameters reuses the cases already
    there instead of answering them again. ``--limit-per-stratum`` selects a
    prefix of a fixed per-stratum order, so a larger limit extends a smaller
    run. Errored cases are not written, so a restart retries them.
    """
    shadow = shadow_tenant_for(tenant_id or "")
    _require_eval_tenants([tenant_id, shadow])
    names = list(systems)
    built = make_systems(names)
    ref = reference or (DEFAULT_REFERENCE if DEFAULT_REFERENCE in names else names[-1])
    if ref not in names:
        raise ValueError(f"reference system {ref!r} is not among the systems run ({names})")
    subject = names[0]

    from pathlib import Path

    from robothor.llm import ollama
    from robothor.memory.eval_embed_cache import CachedEmbedder
    from robothor.memory.eval_world import WORLD_SCHEMA_VERSION, generate_world

    answer_model, judge_model = resolve_eval_models(answer_model, judge_model)

    started = time.perf_counter()
    world = generate_world(seed, size)
    cases = select_cases(world, split, limit_per_stratum)

    llm = functools.partial(llm_judge, model=judge_model)
    judge_fn = make_judge(world, llm=llm)
    validation = await require_valid_judge(world, judge_fn)

    embedding_model = ollama._embedding_model()
    if embed_batch is None:
        cache = CachedEmbedder(
            ollama.get_embeddings_batch_async,
            model=embedding_model,
            seed=seed,
            size=size,
            schema_version=WORLD_SCHEMA_VERSION,
            cache_dir=Path(cache_dir) if cache_dir else None,
        )
        embed: Callable[[list[str]], Awaitable[list[list[float]]]] = cache
    else:
        cache = None
        embed = embed_batch

    async def embed_one(text: str) -> list[float]:
        return (await embed([text]))[0]

    if answer_fn is None:
        answer_fn = functools.partial(answer, model=answer_model, temperature=answer_temperature)

    fingerprint = {
        "kind": "header",
        "suite_id": ANSWERS_SUITE_ID,
        "schema_version": WORLD_SCHEMA_VERSION,
        "seed": seed,
        "size": size,
        "split": split,
        "token_budget": token_budget,
        "answer_model": answer_model,
        "answer_temperature": answer_temperature,
        "judge_model": judge_model,
        "judge_version": JUDGE_VERSION,
        "embedding_model": embedding_model,
    }
    done = _load_progress(progress_path, fingerprint)
    write_lock = asyncio.Lock()
    resumed = 0

    tenants = [tenant_id, shadow]
    records: list[CaseRecord] = []
    errors: list[str] = []
    seed_s = run_s = 0.0
    if cleanup:
        cleanup_world(tenants)
    try:
        t0 = time.perf_counter()
        seeded = await seed_world(world, tenant_id, shadow_tenant_id=shadow, embed_batch=embed)
        if cache is not None:
            cache.save()
        seed_s = time.perf_counter() - t0
        handle = WorldHandle(world, seeded, tenant_id, shadow, embed_one)
        gate = asyncio.Semaphore(max(1, concurrency))

        async def _one(system: Any, case: Case, repeat: int) -> CaseRecord:
            nonlocal resumed
            prior = done.get((system.name, case.id, repeat))
            if prior is not None:
                resumed += 1
                return prior
            async with gate:
                bundle = ContextBundle(
                    system.name, (), token_budget, system.equal_budget, True, 0.0
                )
                try:
                    bundle = await system.retrieve(
                        case, world_handle=handle, token_budget=token_budget
                    )
                    ans = await answer_fn(case.question, bundle, as_of=case.as_of)
                    verdict = await judge_fn(case, ans)
                except Exception as exc:  # noqa: BLE001 — recorded and counted, never hidden
                    logger.warning(
                        "retrieve/answer/judge failed for %s on %s: %s", system.name, case.id, exc
                    )
                    errors.append(f"{system.name} {case.id}: {type(exc).__name__}: {exc}")
                    ans = Answer("", False, ())
                    verdict = Verdict(False, False, False, f"error: {exc}", "error")
                record = CaseRecord(
                    system=system.name,
                    case_id=case.id,
                    stratum=case.stratum,
                    repeat=repeat,
                    correct=verdict.correct,
                    abstained=verdict.abstained,
                    confident_wrong=verdict.confident_wrong,
                    tokens=bundle.tokens,
                    retrieval_ms=bundle.retrieval_ms,
                    answer_ms=ans.latency_ms,
                    equal_budget=bundle.equal_budget,
                    judge_method=verdict.method,
                    answer=ans.text,
                    cited_ids=ans.cited_ids,
                    reason=verdict.reason,
                )
            if progress_path and record.judge_method != "error":
                async with write_lock:
                    _append_progress(progress_path, record)
            return record

        t0 = time.perf_counter()
        for repeat in range(max(1, repeats)):
            for system in built:
                records.extend(await asyncio.gather(*(_one(system, c, repeat) for c in cases)))
        run_s = time.perf_counter() - t0
    finally:
        if cleanup:
            cleanup_world(tenants)
        if cache is not None:
            cache.save()

    return {
        "suite_id": ANSWERS_SUITE_ID,
        "params": {
            "seed": seed,
            "size": size,
            "split": split,
            "systems": names,
            "system_under_test": subject,
            "reference": ref,
            "token_budget": token_budget,
            "repeats": max(1, repeats),
            "answer_model": answer_model,
            "answer_provider": "ollama",
            "answer_temperature": answer_temperature,
            "judge_model": judge_model,
            "judge_version": JUDGE_VERSION,
            "embedding_model": embedding_model,
            "limit_per_stratum": limit_per_stratum,
            "concurrency": concurrency,
            "cases": len(cases),
            "resumed": resumed,
            "progress_path": progress_path,
        },
        "judge_validation": dataclasses.asdict(validation),
        "systems": summarise(records),
        "comparison": compare(records, system=subject, reference=ref),
        "timings": {
            "seed_s": round(seed_s, 3),
            "answer_s": round(run_s, 3),
            "total_s": round(time.perf_counter() - started, 3),
            "embedding_cache": (
                {"hits": cache.hits, "misses": cache.misses} if cache is not None else None
            ),
        },
        "errors": errors,
        "records": [dataclasses.asdict(r) for r in records],
    }


def _load_progress(
    path: str | None, fingerprint: dict[str, Any]
) -> dict[tuple[str, str, int], CaseRecord]:
    """Records already in the progress file; writes the header for a new file."""
    if not path:
        return {}
    from pathlib import Path

    file = Path(path)
    if not file.exists() or not file.read_text(encoding="utf-8").strip():
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(fingerprint, sort_keys=True) + "\n", encoding="utf-8")
        return {}
    lines = file.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    if header != fingerprint:
        diff = sorted(k for k in fingerprint if header.get(k) != fingerprint[k])
        raise ValueError(
            f"progress file {path} was written with different parameters ({', '.join(diff)}); "
            "use another --progress path or delete it"
        )
    out: dict[tuple[str, str, int], CaseRecord] = {}
    fields = {f.name for f in dataclasses.fields(CaseRecord)}
    for line in lines[1:]:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line cut off by a kill: that case simply runs again
        data = {k: v for k, v in row.items() if k in fields}
        data["cited_ids"] = tuple(data.get("cited_ids") or ())
        rec = CaseRecord(**data)
        out[(rec.system, rec.case_id, rec.repeat)] = rec
    return out


def _append_progress(path: str, record: CaseRecord) -> None:
    from pathlib import Path

    with Path(path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dataclasses.asdict(record), default=str) + "\n")
        fh.flush()


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x:.1%}"


def format_answers_markdown(report: dict[str, Any]) -> str:
    p = report.get("params", {})
    jv = report.get("judge_validation", {})
    lines = [
        "# Memory eval: answer-and-judge comparison",
        "",
        f"World seed {p.get('seed')}, size {p.get('size')}, split {p.get('split')}, "
        f"{p.get('cases')} cases"
        + (f" (at most {p['limit_per_stratum']} per stratum)" if p.get("limit_per_stratum") else "")
        + f", token budget {p.get('token_budget')}, repeats {p.get('repeats')}.",
        f"Answer model `{p.get('answer_model')}` (temperature {p.get('answer_temperature')}), "
        f"judge model `{p.get('judge_model')}`, embedding model `{p.get('embedding_model')}`.",
        "",
        f"Judge validation: {jv.get('wrong_marked_wrong', 0):.1%} of {jv.get('n_wrong')} planted "
        f"wrong answers marked wrong, {jv.get('right_marked_right', 0):.1%} of {jv.get('n_right')} "
        "planted correct answers marked right.",
        "",
        "| System | Stratum | n | Accuracy [95% CI] | Confident-wrong | Abstain P / R | "
        "Tokens | Retrieval p50 / p95 ms | Answer p50 ms | Run spread | MDE |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for system, strata in report.get("systems", {}).items():
        order = [s for s in strata if s not in ("all", "hard")]
        order += [s for s in ("hard", "all") if s in strata]
        for stratum in order:
            m = strata[stratum]
            lo, hi = m.get("wilson", (0.0, 0.0))
            spread = m.get("spread", {})
            lines.append(
                f"| {system} | {stratum} | {m.get('n', 0)} | {m.get('accuracy', 0.0):.1%} "
                f"[{lo:.0%}–{hi:.0%}] | {m.get('confident_wrong_rate', 0.0):.1%} | "
                f"{_pct(m.get('abstention_precision'))} / {_pct(m.get('abstention_recall'))} | "
                f"{m.get('mean_tokens', 0.0):.0f} | {m.get('retrieval_p50_ms', 0.0):.0f} / "
                f"{m.get('retrieval_p95_ms', 0.0):.0f} | {m.get('answer_p50_ms', 0.0):.0f} | "
                f"{spread.get('range', 0.0):.1%} over {spread.get('runs', 0)} | "
                f"{m.get('mde', 1.0):.1%} |"
            )
    c = report.get("comparison", {})
    if c:
        ni = c.get("non_inferiority", {})
        pr = c.get("paired", {})
        lines += [
            "",
            f"Deciding comparison (pooled hard strata): `{c.get('system')}` vs "
            f"`{c.get('reference')}`: {pr.get('improved')} improved, {pr.get('regressed')} "
            f"regressed of {pr.get('n')}; promote: {c.get('promote')} ({c.get('verdict')}). "
            f"Non-inferior at a {ni.get('margin', 0):.0%} margin: {ni.get('non_inferior')} "
            f"(difference {ni.get('diff', 0):+.1%}, 95% CI {ni.get('lower', 0):+.1%} to "
            f"{ni.get('upper', 0):+.1%}).",
        ]
    t = report.get("timings", {})
    if t:
        lines += [
            "",
            f"Time: seeding {t.get('seed_s')} s, answering {t.get('answer_s')} s, "
            f"total {t.get('total_s')} s.",
        ]
    if report.get("errors"):
        lines += ["", f"Errors: {len(report['errors'])} (recorded as wrong; see the JSON)."]
    return "\n".join(lines) + "\n"
