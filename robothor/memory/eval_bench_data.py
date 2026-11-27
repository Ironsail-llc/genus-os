"""Public long-memory benchmarks: their data, prompts and published scoring.

Pure code (no database, no model): it loads a benchmark file the operator
downloaded, and reproduces the benchmark's own reader prompt, judge prompt
and metric, so a number we report is scored the way the benchmark's
published numbers are. :mod:`robothor.memory.eval_bench` ingests and runs.

Supported:

    longmemeval  Wu et al., ICLR 2025, https://github.com/xiaowu0162/LongMemEval
                 (MIT). One history per question. Files from the Hugging Face
                 dataset ``xiaowu0162/longmemeval-cleaned``:
                 ``longmemeval_s_cleaned.json`` (variant "s"),
                 ``longmemeval_oracle.json`` ("oracle"), ``longmemeval_m_cleaned.json`` ("m").
                 Scored by ``src/evaluation/evaluate_qa.py`` (yes/no judge per
                 question type, ``_abs`` ids judged as abstention) and
                 ``src/evaluation/print_qa_metrics.py`` (overall, task-averaged
                 and abstention accuracy).
    locomo       Maharana et al., ACL 2024, https://github.com/snap-research/locomo
                 (data CC BY-NC 4.0). ``data/locomo10.json``: ten
                 conversations, their questions. Answered with the prompts of
                 ``task_eval/gpt_utils.py`` and scored by token F1 per category,
                 ``task_eval/evaluation.py`` ``eval_question_answering``.

Nothing here downloads anything; see ``docs/memory-system.md``.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import string
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

BENCHMARKS: tuple[str, ...] = ("longmemeval", "locomo")

#: Seconds between consecutive turns of one session. The benchmarks date
#: sessions, not turns; turns keep their order inside the session's minute
#: range, well inside the segmenter's 30-minute gap.
TURN_SPACING_SECONDS = 30

# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BenchTurn:
    #: ``user`` or ``assistant`` (LoCoMo: the first speaker is ``user``).
    role: str
    text: str
    at: datetime


@dataclass(frozen=True)
class BenchSession:
    id: str
    at: datetime
    turns: tuple[BenchTurn, ...]


@dataclass(frozen=True)
class BenchHistory:
    """One conversation history to ingest: every question on it shares it."""

    id: str
    sessions: tuple[BenchSession, ...]
    #: LoCoMo's two speakers; empty for a user/assistant chat.
    speakers: tuple[str, ...] = ()


@dataclass(frozen=True)
class BenchQuestion:
    id: str
    history_id: str
    question_type: str
    question: str
    #: The reference answer (LongMemEval: for an ``_abs`` question, the
    #: explanation; for a preference question, the rubric).
    answer: str
    #: The question's own date as the benchmark gives it (LongMemEval).
    as_of: str | None = None
    abstention: bool = False
    #: LoCoMo's numeric category.
    category: int | None = None
    #: The sessions holding the answer (LongMemEval ``answer_session_ids``).
    evidence_sessions: tuple[str, ...] = ()
    #: The text of every turn marked as holding the answer (LongMemEval
    #: ``has_answer``), in history order.
    evidence_turns: tuple[str, ...] = ()

    @property
    def stratum(self) -> str:
        return self.question_type


@dataclass(frozen=True)
class Benchmark:
    name: str
    variant: str
    histories: dict[str, BenchHistory]
    questions: tuple[BenchQuestion, ...]
    #: The file it was read from, with its size and SHA-256.
    source: str = ""
    sha256: str = ""


@dataclass(frozen=True)
class BenchRecord:
    """One system's answer to one question, scored."""

    system: str
    question_id: str
    question_type: str
    abstention: bool
    #: 1/0 for a judged benchmark; LoCoMo's token F1 in 0..1.
    score: float
    tokens: int
    retrieval_ms: float
    answer_ms: float
    judge_ms: float = 0.0
    answer: str = ""
    judge_output: str = ""
    error: str = ""
    #: Share of the question's evidence sessions in the context sent; None
    #: when there is none to find (an abstention question).
    evidence_recall: float | None = None
    #: Share of the question's answer-bearing turns whose text reached the
    #: context sent; None when none are labelled (or an abstention question).
    answer_turn_recall: float | None = None
    #: Every answer-bearing turn reached the context; None when not measured.
    answer_turns_complete: bool | None = None
    #: What the reranker did for this context: ``scored``, ``fell_back`` (a
    #: pair failed; fused order), ``unavailable`` (unscored rows),
    #: ``not_run``; empty in a record written before it was recorded.
    reranker: str = ""


@dataclass(frozen=True)
class PromptItem:
    """A context item as the reader prompt shows it."""

    kind: str
    text: str
    #: The session date, in the benchmark's own format, when known.
    date: str | None
    #: The session the item comes from (fragments of one session share it).
    session: str | None = None
    #: Where in the session it starts (the exchange index), for reading order.
    position: int = 0


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_longmemeval_date(value: str) -> datetime:
    """``2023/05/20 (Sat) 02:21`` -> 2023-05-20 02:21 (UTC)."""
    cleaned = re.sub(r"\s*\([A-Za-z]+\)\s*", " ", value.strip())
    return datetime.strptime(cleaned + " +0000", "%Y/%m/%d %H:%M %z").astimezone(UTC)


def parse_locomo_date(value: str) -> datetime:
    """``1:56 pm on 8 May, 2023`` -> 2023-05-08 13:56 (UTC)."""
    text = value.strip().replace(",", "")
    time_part, _, date_part = text.partition(" on ")
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            day = datetime.strptime(date_part.strip() + " +0000", fmt + " %z").astimezone(UTC)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"unreadable LoCoMo date {value!r}")
    clock = datetime.strptime(time_part.strip().upper() + " +0000", "%I:%M %p %z")
    return day.replace(hour=clock.hour, minute=clock.minute)


def _turns(session_at: datetime, raw: Iterable[tuple[str, str]]) -> tuple[BenchTurn, ...]:
    return tuple(
        BenchTurn(role, text, session_at + timedelta(seconds=TURN_SPACING_SECONDS * i))
        for i, (role, text) in enumerate(raw)
    )


def _longmemeval_variant(path: Path) -> str:
    name = path.name.lower()
    if "oracle" in name:
        return "oracle"
    if "_m" in name:
        return "m"
    return "s"


def load_longmemeval(path: str | Path) -> Benchmark:
    file = Path(path)
    data = json.loads(file.read_text(encoding="utf-8"))
    histories: dict[str, BenchHistory] = {}
    questions: list[BenchQuestion] = []
    for entry in data:
        qid = str(entry["question_id"])
        sessions = []
        for sid, date, turns in zip(
            entry["haystack_session_ids"],
            entry["haystack_dates"],
            entry["haystack_sessions"],
            strict=True,
        ):
            at = parse_longmemeval_date(date)
            raw = [(str(t["role"]), str(t.get("content") or "")) for t in turns]
            sessions.append(BenchSession(str(sid), at, _turns(at, raw)))
        # The haystack's own order is the clock's in the released files; sort
        # anyway (stable), since knowledge updates depend on it.
        sessions.sort(key=lambda s: s.at)
        histories[qid] = BenchHistory(qid, tuple(sessions))
        answer_turns = tuple(
            str(t.get("content") or "")
            for s in sorted(
                zip(entry["haystack_dates"], entry["haystack_sessions"], strict=True),
                key=lambda pair: parse_longmemeval_date(pair[0]),
            )
            for t in s[1]
            if t.get("has_answer")
        )
        questions.append(
            BenchQuestion(
                id=qid,
                history_id=qid,
                question_type=str(entry["question_type"]),
                question=str(entry["question"]),
                answer=str(entry["answer"]),
                as_of=str(entry.get("question_date") or "") or None,
                # evaluate_qa.py: abstention='_abs' in entry['question_id'].
                abstention="_abs" in qid,
                evidence_sessions=tuple(str(x) for x in entry.get("answer_session_ids") or ()),
                evidence_turns=answer_turns,
            )
        )
    return Benchmark(
        "longmemeval",
        _longmemeval_variant(file),
        histories,
        tuple(questions),
        source=f"{file.name} ({file.stat().st_size} bytes)",
        sha256=_file_digest(file),
    )


#: LoCoMo's categories. task_eval/evaluation.py scores 1 as multi-hop (split
#: answers), 2-4 as single answers and 5 as adversarial; the names are the
#: ones the published comparisons use for 1-4.
LOCOMO_CATEGORIES: dict[int, str] = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}
#: Dropped by convention in every published LoCoMo comparison: its reference
#: is a distractor, not an answer.
LOCOMO_DEFAULT_DROP: frozenset[str] = frozenset({"adversarial"})


def load_locomo(path: str | Path) -> Benchmark:
    file = Path(path)
    data = json.loads(file.read_text(encoding="utf-8"))
    histories: dict[str, BenchHistory] = {}
    questions: list[BenchQuestion] = []
    for sample in data:
        sid = str(sample["sample_id"])
        conv = sample["conversation"]
        a, b = str(conv["speaker_a"]), str(conv["speaker_b"])
        numbers = sorted(
            int(k.split("_")[1])
            for k in conv
            if re.fullmatch(r"session_\d+", k) and isinstance(conv[k], list) and conv[k]
        )
        sessions = []
        for n in numbers:
            at = parse_locomo_date(str(conv[f"session_{n}_date_time"]))
            raw = []
            for d in conv[f"session_{n}"]:
                text = f"{d['speaker']}: {d['text']}"
                # As get_input_context / get_conversation_lengths render an image.
                if d.get("img_url") and d.get("blip_caption"):
                    text += f" [shares {d['blip_caption']}]"
                raw.append(("user" if d["speaker"] == a else "assistant", text))
            sessions.append(BenchSession(f"{sid}:session_{n}", at, _turns(at, raw)))
        histories[sid] = BenchHistory(sid, tuple(sessions), speakers=(a, b))
        for i, qa in enumerate(sample["qa"]):
            cat = int(qa["category"])
            answer = qa.get("answer", qa.get("adversarial_answer", ""))
            if cat == 5 and "adversarial_answer" in qa:
                answer = qa["adversarial_answer"]
            questions.append(
                BenchQuestion(
                    id=f"{sid}:{i}",
                    history_id=sid,
                    question_type=LOCOMO_CATEGORIES.get(cat, str(cat)),
                    question=str(qa["question"]),
                    answer=str(answer),
                    category=cat,
                    abstention=cat == 5,
                )
            )
    return Benchmark(
        "locomo",
        "locomo10",
        histories,
        tuple(questions),
        source=f"{file.name} ({file.stat().st_size} bytes)",
        sha256=_file_digest(file),
    )


def load_benchmark(name: str, path: str | Path) -> Benchmark:
    if name == "longmemeval":
        return load_longmemeval(path)
    if name == "locomo":
        return load_locomo(path)
    raise ValueError(f"unknown benchmark {name!r} (supported: {', '.join(BENCHMARKS)})")


def _order_key(q: BenchQuestion) -> str:
    return hashlib.sha256(q.id.encode()).hexdigest()


def select_questions(
    questions: Sequence[BenchQuestion],
    *,
    limit: int | None = None,
    limit_per_type: int | None = None,
    drop_types: Iterable[str] = (),
    abstention_stratum: bool = False,
) -> list[BenchQuestion]:
    """A stratified, deterministic subset.

    Each question type is put in a fixed order (by a hash of the id), then
    types are taken round robin: ``limit`` questions overall, at most
    ``limit_per_type`` of each. A larger limit extends a smaller one, so a
    resumed run with a bigger limit reuses what it has. The result is in the
    benchmark's own order. With ``abstention_stratum`` the abstention
    questions are a stratum of their own, with their own quota.
    """
    dropped = set(drop_types)
    by_type: dict[str, list[BenchQuestion]] = {}
    for q in questions:
        if q.question_type not in dropped:
            stratum = "abstention" if (abstention_stratum and q.abstention) else q.question_type
            by_type.setdefault(stratum, []).append(q)
    for qs in by_type.values():
        qs.sort(key=_order_key)
        if limit_per_type is not None:
            del qs[max(0, limit_per_type) :]
    chosen: list[BenchQuestion] = []
    depth = 0
    types = sorted(by_type)
    while any(depth < len(by_type[t]) for t in types):
        chosen.extend(by_type[t][depth] for t in types if depth < len(by_type[t]))
        depth += 1
    if limit is not None:
        chosen = chosen[: max(0, limit)]
    keep = {q.id for q in chosen}
    return [q for q in questions if q.id in keep]


# --------------------------------------------------------------------------- #
# LongMemEval: reader prompt, as prepare_prompt in src/generation/run_generation.py
# --------------------------------------------------------------------------- #

_LME_CHATS = (
    "I will give you several history chats between you and a user. Please answer the question "
    "based on the relevant chat history.\n\n\nHistory Chats:\n\n{}\n\nCurrent Date: {}\n"
    "Question: {}\nAnswer:"
)
_LME_CHATS_AND_FACTS = (
    "I will give you several history chats between you and a user, as well as the relevant user "
    "facts extracted from the chat history. Please answer the question based on the relevant chat "
    "history and the user facts\n\n\nHistory Chats:\n\n{}\n\nCurrent Date: {}\nQuestion: {}\n"
    "Answer:"
)
_LME_FACTS = (
    "I will give you several facts extracted from history chats between you and a user. Please "
    "answer the question based on the relevant facts.\n\n\nHistory Chats:\n\n{}\n\n"
    "Current Date: {}\nQuestion: {}\nAnswer:"
)


#: Between two fragments of one session in the session layout.
FRAGMENT_GAP = "[… turns not recalled …]"


def render_sessions(items: Sequence[PromptItem]) -> str:
    """Context items in the official history layout, one block per session.

    A dated item is rendered as the official history string renders a
    session (``### Session n: / Session Date: / Session Content:``); an
    undated fact is one line. Items of one session (``session``) share one
    block, numbered where the session first appears in rank order, with the
    fragments in conversation order and :data:`FRAGMENT_GAP` between them;
    as separate blocks they read as separate sessions, and models counted
    them twice.
    """
    blocks: list[tuple[str, list[PromptItem]]] = []
    by_session: dict[str, list[PromptItem]] = {}
    for item in items:
        if item.date and item.session:
            if item.session not in by_session:
                by_session[item.session] = []
                blocks.append(("session", by_session[item.session]))
            by_session[item.session].append(item)
        else:
            blocks.append(("session" if item.date else "fact", [item]))
    parts: list[str] = []
    for n, (kind, group) in enumerate(blocks, start=1):
        if kind == "fact":
            parts.append(f"\n### Fact {n}:\n{group[0].text}\n")
            continue
        ordered = sorted(group, key=lambda i: i.position)
        content = f"\n{FRAGMENT_GAP}\n".join(i.text for i in ordered)
        parts.append(
            f"\n### Session {n}:\nSession Date: {group[0].date}\nSession Content:\n{content}\n"
        )
    return "".join(parts)


def longmemeval_answer_prompt(
    question: str,
    as_of: str | None,
    items: Sequence[PromptItem],
    *,
    history: str | None = None,
) -> str:
    """The official reader prompt (no chain of thought), for this context.

    The template is chosen as ``prepare_prompt`` chooses it: chats only,
    extracted facts only, or both. ``history`` is the context text as a
    renderer produced it; without it the items are rendered by
    :func:`render_sessions`.
    """
    kinds = {i.kind for i in items}
    has_facts = "fact" in kinds
    has_chats = bool(kinds - {"fact"})
    template = (
        _LME_CHATS_AND_FACTS if has_facts and has_chats else _LME_FACTS if has_facts else _LME_CHATS
    )
    text = render_sessions(items) if history is None else history
    return template.format(text or "\n(no history)\n", as_of or "", question)


# --------------------------------------------------------------------------- #
# LongMemEval: judge, as get_anscheck_prompt in src/evaluation/evaluate_qa.py
# --------------------------------------------------------------------------- #

LONGMEMEVAL_TYPES: tuple[str, ...] = (
    "single-session-user",
    "single-session-preference",
    "single-session-assistant",
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
)
#: The official judge's model and settings (evaluate_qa.py model_zoo,
#: chat_completions kwargs). A local run states its own judge beside these.
LONGMEMEVAL_OFFICIAL_JUDGE = "gpt-4o-2024-08-06"
LONGMEMEVAL_JUDGE_MAX_TOKENS = 10

_STANDARD = (
    "I will give you a question, a correct answer, and a response from a model. Please answer "
    "yes if the response contains the correct answer. Otherwise, answer no. If the response is "
    "equivalent to the correct answer or contains all the intermediate steps to get the correct "
    "answer, you should also answer yes. If the response only contains a subset of the "
    "information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\n"
    "Model Response: {}\n\nIs the model response correct? Answer yes or no only."
)
_TEMPORAL = (
    "I will give you a question, a correct answer, and a response from a model. Please answer "
    "yes if the response contains the correct answer. Otherwise, answer no. If the response is "
    "equivalent to the correct answer or contains all the intermediate steps to get the correct "
    "answer, you should also answer yes. If the response only contains a subset of the "
    "information required by the answer, answer no. In addition, do not penalize off-by-one "
    "errors for the number of days. If the question asks for the number of days/weeks/months, "
    "etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is "
    "18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\n"
    "Model Response: {}\n\nIs the model response correct? Answer yes or no only."
)
_KNOWLEDGE_UPDATE = (
    "I will give you a question, a correct answer, and a response from a model. Please answer "
    "yes if the response contains the correct answer. Otherwise, answer no. If the response "
    "contains some previous information along with an updated answer, the response should be "
    "considered as correct as long as the updated answer is the required answer.\n\n"
    "Question: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? "
    "Answer yes or no only."
)
_PREFERENCE = (
    "I will give you a question, a rubric for desired personalized response, and a response "
    "from a model. Please answer yes if the response satisfies the desired response. Otherwise, "
    "answer no. The model does not need to reflect all the points in the rubric. The response is "
    "correct as long as it recalls and utilizes the user's personal information correctly.\n\n"
    "Question: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer "
    "yes or no only."
)
_ABSTENTION = (
    "I will give you an unanswerable question, an explanation, and a response from a model. "
    "Please answer yes if the model correctly identifies the question as unanswerable. The model "
    "could say that the information is incomplete, or some other information is given but the "
    "asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\n"
    "Does the model correctly identify the question as unanswerable? Answer yes or no only."
)


def longmemeval_judge_prompt(q: BenchQuestion, response: str) -> str:
    """``get_anscheck_prompt(task, question, answer, response, abstention)``, verbatim."""
    if q.abstention:
        template = _ABSTENTION
    elif q.question_type in ("single-session-user", "single-session-assistant", "multi-session"):
        template = _STANDARD
    elif q.question_type == "temporal-reasoning":
        template = _TEMPORAL
    elif q.question_type == "knowledge-update":
        template = _KNOWLEDGE_UPDATE
    elif q.question_type == "single-session-preference":
        template = _PREFERENCE
    else:
        raise NotImplementedError(f"LongMemEval has no judge prompt for {q.question_type!r}")
    return template.format(q.question, q.answer, response)


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def longmemeval_label(judge_output: str) -> bool:
    """``label = 'yes' in eval_response.lower()``, after any reasoning block."""
    return "yes" in _THINK.sub("", judge_output or "").strip().lower()


@dataclass(frozen=True)
class PlantedResponse:
    question: BenchQuestion
    response: str
    expect_yes: bool
    #: gold | other_answer | abstained | abstention_answered
    kind: str


_RIGHT_FORMS = ("{a}", "The answer is {a}.", "Based on our earlier chats, {a}.")
_ABSTAIN_FORMS = (
    "I don't know: the chat history does not mention that.",
    "The information is not in our previous conversations, so I can't say.",
)


def longmemeval_planted(
    questions: Sequence[BenchQuestion], *, seed: int = 1, per_kind: int = 60
) -> list[PlantedResponse]:
    """Planted right and wrong responses for validating a judge.

    Right: the reference answer itself, plainly phrased; for an abstention
    question, a plain "not mentioned". Wrong: another question's reference
    answer, of the same type when there is one (never one that contains this answer or is
    contained in it); for an abstention question, a confident answer. The
    preference rubric is a description, not an answer, so preference
    questions are not planted.
    """
    rng = random.Random(seed)
    by_type: dict[str, list[BenchQuestion]] = {}
    for q in questions:
        if not q.abstention:
            by_type.setdefault(q.question_type, []).append(q)
    answers = [q.answer for q in questions if not q.abstention]
    out: dict[str, list[PlantedResponse]] = {}

    def _add(p: PlantedResponse) -> None:
        out.setdefault(p.kind, []).append(p)

    for n, q in enumerate(questions):
        if q.question_type == "single-session-preference":
            continue
        if q.abstention:
            _add(PlantedResponse(q, _ABSTAIN_FORMS[n % 2], True, "abstained"))
            if answers:
                _add(PlantedResponse(q, rng.choice(answers), False, "abstention_answered"))
            continue
        _add(PlantedResponse(q, _RIGHT_FORMS[n % 3].format(a=q.answer), True, "gold"))
        mine = q.answer.lower()

        def _rivals(
            pool: Sequence[BenchQuestion], q: BenchQuestion = q, mine: str = mine
        ) -> list[str]:
            return [
                o.answer
                for o in pool
                if o.id != q.id and o.answer.lower() not in mine and mine not in o.answer.lower()
            ]

        # Same type first (the hardest wrong answer); any other otherwise.
        others = _rivals(by_type.get(q.question_type, [])) or _rivals(
            [
                o
                for qs in by_type.values()
                for o in qs
                if o.question_type != "single-session-preference"
            ]
        )
        if others:
            _add(PlantedResponse(q, rng.choice(others), False, "other_answer"))
    planted: list[PlantedResponse] = []
    for kind in sorted(out):
        planted.extend(out[kind][:per_kind])
    return planted


# --------------------------------------------------------------------------- #
# LoCoMo: reader prompts, as get_gpt_answers in task_eval/gpt_utils.py
# --------------------------------------------------------------------------- #

_LOCOMO_START = (
    "Below is a conversation between two people: {} and {}. The conversation takes place over "
    "multiple days and the date of each conversation is wriiten at the beginning of the "
    "conversation.\n\n"
)
_LOCOMO_QA = (
    "\nBased on the above context, write an answer in the form of a short phrase for the "
    "following question. Answer with exact words from the context whenever possible.\n\n"
    "Question: {} Short answer:\n"
)
_LOCOMO_QA_CAT_5 = (
    "\nBased on the above context, answer the following question.\n\nQuestion: {} Short answer:\n"
)
_NOT_MENTIONED = "Not mentioned in the conversation"
#: gpt_utils.py run_chatgpt(num_tokens_request=32).
LOCOMO_ANSWER_MAX_TOKENS = 32


def locomo_question_text(q: BenchQuestion, *, seed: int = 1) -> tuple[str, dict[str, str] | None]:
    """The question as get_gpt_answers asks it, and the choice key for category 5.

    The official code picks the option order with ``random.random()``; here
    it is seeded per question, so a rerun asks the same question.
    """
    if q.category == 2:
        return q.question + " Use DATE of CONVERSATION to answer with an approximate date.", None
    if q.category == 5:
        rng = random.Random(f"{seed}:{q.id}")
        text = q.question + " Select the correct answer: (a) {} (b) {}. "
        if rng.random() < 0.5:
            return text.format(_NOT_MENTIONED, q.answer), {"a": _NOT_MENTIONED, "b": q.answer}
        return text.format(q.answer, _NOT_MENTIONED), {"b": _NOT_MENTIONED, "a": q.answer}
    return q.question, None


def locomo_cat5_answer(prediction: str, key: dict[str, str]) -> str:
    """``get_cat_5_answer``: map a bare ``a`` / ``(b)`` choice to its option."""
    p = prediction.strip().lower()
    if len(p) == 1:
        return key["a"] if "a" in p else key["b"]
    if len(p) == 3:
        return key["a"] if "(a)" in p else key["b"]
    return p


def locomo_answer_prompt(
    speakers: Sequence[str], items: Sequence[PromptItem], question_text: str, q: BenchQuestion
) -> str:
    a, b = (list(speakers) + ["", ""])[:2]
    lines = [f"DATE: {i.date}\n{i.text}" if i.date else i.text for i in items]
    context = "\n\n".join(lines) if lines else "(no conversation)"
    qa = _LOCOMO_QA_CAT_5 if q.category == 5 else _LOCOMO_QA
    return _LOCOMO_START.format(a, b) + context + "\n\n" + qa.format(question_text)


# --------------------------------------------------------------------------- #
# LoCoMo: metric, as eval_question_answering in task_eval/evaluation.py
# --------------------------------------------------------------------------- #

Stemmer = Callable[[str], str]


def _porter() -> Stemmer:
    try:
        from nltk.stem import PorterStemmer
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "LoCoMo's official F1 stems with nltk's PorterStemmer; install nltk to score LoCoMo"
        ) from exc
    stem: Stemmer = PorterStemmer().stem
    return stem


def locomo_normalize_answer(s: str) -> str:
    """``normalize_answer``: drop commas, lower, punctuation, a/an/the/and, spaces."""
    import regex

    s = s.replace(",", "")
    s = s.lower()
    exclude = set(string.punctuation)
    s = "".join(ch for ch in s if ch not in exclude)
    s = regex.sub(r"\b(a|an|the|and)\b", " ", s)
    return " ".join(s.split())


def locomo_f1_score(prediction: str, ground_truth: str, *, stem: Stemmer | None = None) -> float:
    """``f1_score``: token F1 over Porter-stemmed normalised words."""
    from collections import Counter

    st = stem or _porter()
    pred = [st(w) for w in locomo_normalize_answer(prediction).split()]
    gold = [st(w) for w in locomo_normalize_answer(ground_truth).split()]
    common = Counter(pred) & Counter(gold)
    same = sum(common.values())
    if same == 0:
        return 0.0
    precision = same / len(pred)
    recall = same / len(gold)
    return (2 * precision * recall) / (precision + recall)


def locomo_f1_multi(prediction: str, ground_truth: str, *, stem: Stemmer | None = None) -> float:
    """``f1``: per gold part (comma-split), the best prediction part; their mean."""
    st = stem or _porter()
    preds = [p.strip() for p in prediction.split(",")]
    golds = [g.strip() for g in ground_truth.split(",")]
    return sum(max(locomo_f1_score(p, g, stem=st) for p in preds) for g in golds) / len(golds)


def locomo_score(q: BenchQuestion, prediction: str, *, stem: Stemmer | None = None) -> float:
    """``eval_question_answering`` for one question."""
    answer = q.answer
    if q.category == 3:
        answer = answer.split(";")[0].strip()
    if q.category in (2, 3, 4):
        return locomo_f1_score(prediction, answer, stem=stem)
    if q.category == 1:
        return locomo_f1_multi(prediction, answer, stem=stem)
    if q.category == 5:
        low = prediction.lower()
        return 1.0 if ("no information available" in low or "not mentioned" in low) else 0.0
    raise ValueError(f"LoCoMo has no category {q.category!r}")


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def _percentile(values: Sequence[float], pct: float) -> float:
    from robothor.memory.eval_stats import percentile

    return float(percentile(list(values), pct))


#: The three causes a wrong answer is put down to (:func:`loss_bucket`).
LOSS_BUCKETS: tuple[str, ...] = (
    "answer_turns_absent",
    "answer_turns_present_answer_wrong",
    "abstention",
)


def loss_bucket(rec: BenchRecord) -> str | None:
    """Why a wrong answer was wrong, at the level of the answer-bearing turns.

    ``abstention``: an unanswerable question answered anyway.
    ``answer_turns_present_answer_wrong``: every labelled answer turn was in
    the context; the answer step is at fault. ``answer_turns_absent``: some
    answer turn was not delivered (never retrieved, excerpted or packed
    away), whatever the session-level count says. A record with no turn
    measurement falls back to its session-level evidence. None when right.
    """
    if rec.score >= 1.0:
        return None
    if rec.abstention:
        return "abstention"
    complete = rec.answer_turns_complete
    if complete is None:
        complete = rec.evidence_recall is not None and rec.evidence_recall >= 1.0
    return "answer_turns_present_answer_wrong" if complete else "answer_turns_absent"


def _block(recs: Sequence[BenchRecord]) -> dict[str, Any]:
    n = len(recs)
    correct = sum(r.score for r in recs)
    from robothor.memory.eval_stats import wilson_interval

    lo, hi = wilson_interval(round(correct), n) if n else (0.0, 0.0)
    found = [r.evidence_recall for r in recs if r.evidence_recall is not None]
    wrong = [r for r in recs if r.score < 1.0 and r.evidence_recall is not None]
    turns = [r.answer_turn_recall for r in recs if r.answer_turn_recall is not None]
    reranker: dict[str, int] = {}
    for r in recs:
        state = r.reranker or "unrecorded"
        reranker[state] = reranker.get(state, 0) + 1
    losses = dict.fromkeys(LOSS_BUCKETS, 0)
    for r in recs:
        bucket = loss_bucket(r)
        if bucket:
            losses[bucket] += 1
    return {
        "n": n,
        "accuracy": (correct / n) if n else 0.0,
        "evidence_n": len(found),
        "evidence_recall": (sum(found) / len(found)) if found else None,
        "evidence_complete": sum(1 for x in found if x >= 1.0),
        "wrong_with_evidence": sum(1 for r in wrong if (r.evidence_recall or 0.0) >= 1.0),
        "wrong_missing_evidence": sum(1 for r in wrong if (r.evidence_recall or 0.0) < 1.0),
        "answer_turn_n": len(turns),
        "answer_turn_recall": (sum(turns) / len(turns)) if turns else None,
        "answer_turns_complete": sum(1 for r in recs if r.answer_turns_complete),
        "losses": losses,
        "reranker": dict(sorted(reranker.items())),
        "wilson": [lo, hi],
        "errors": sum(1 for r in recs if r.error),
        "mean_tokens": (sum(r.tokens for r in recs) / n) if n else 0.0,
        "median_tokens": _percentile([float(r.tokens) for r in recs], 50) if n else 0.0,
        "retrieval_p50_ms": _percentile([r.retrieval_ms for r in recs], 50) if n else 0.0,
        "retrieval_p95_ms": _percentile([r.retrieval_ms for r in recs], 95) if n else 0.0,
        "answer_p50_ms": _percentile([r.answer_ms for r in recs], 50) if n else 0.0,
        "answer_p95_ms": _percentile([r.answer_ms for r in recs], 95) if n else 0.0,
    }


def summarise_bench(records: Sequence[BenchRecord], *, benchmark: str) -> dict[str, Any]:
    """Per system: overall, per type, task-averaged and abstention accuracy.

    For LongMemEval as ``print_qa_metrics.py`` reports it: overall is the
    question-weighted mean, task-averaged the mean of the per-type means,
    abstention the ``_abs`` questions (also counted in their own type). For
    LoCoMo "accuracy" is the mean token F1. An errored record counts as 0.
    """
    out: dict[str, Any] = {}
    for system in sorted({r.system for r in records}):
        recs = [r for r in records if r.system == system]
        by_type = {
            t: _block([r for r in recs if r.question_type == t])
            for t in sorted({r.question_type for r in recs})
        }
        abst = [r for r in recs if r.abstention]
        out[system] = {
            "metric": "token_f1" if benchmark == "locomo" else "judge_accuracy",
            "overall": _block(recs),
            "by_type": by_type,
            "task_averaged": (
                sum(b["accuracy"] for b in by_type.values()) / len(by_type) if by_type else 0.0
            ),
            "abstention": _block(abst) if abst else {"n": 0, "accuracy": None},
        }
    return out


# --------------------------------------------------------------------------- #
# Published LongMemEval-S results: collected from their sources, not reproduced
# --------------------------------------------------------------------------- #

#: Outside LongMemEval-S numbers with the conditions that decide whether they
#: compare with a run here. Read from each source (2026-11), never re-run by
#: this harness. ``score`` is overall accuracy (%) as published; ``context_tokens``
#: is what the answering model sees per question.
PUBLISHED_LONGMEMEVAL: tuple[dict[str, str], ...] = (
    {
        "system": "Full context (benchmark paper)",
        "source": "https://arxiv.org/abs/2410.10813",
        "answer_model": "gpt-4o",
        "judge": "gpt-4o (official)",
        "context_tokens": "~115k",
        "retrieval": "no",
        "score": "60.6 (64.0 with chain-of-note)",
        "caveats": "the benchmark authors' own full-context baseline",
    },
    {
        "system": "Raw-RAG, no turn expansion (independent paper)",
        "source": "https://arxiv.org/abs/2610.30755",
        "answer_model": "gpt-4.1-mini",
        "judge": "gpt-4o-mini x3",
        "context_tokens": "4.1k",
        "retrieval": "yes",
        "score": "89.2",
        "caveats": "independent; budget reported; judge not the official one (likely more "
        "lenient); nearest fair comparison at ~4k tokens",
    },
    {
        "system": "Zep / Graphiti",
        "source": "https://arxiv.org/abs/2501.13956",
        "answer_model": "gpt-4o",
        "judge": "not stated",
        "context_tokens": "~1.6k",
        "retrieval": "yes",
        "score": "71.2",
        "caveats": "vendor-run; budget nearest a 1k cell; higher figures quoted elsewhere "
        "were not found in a primary source",
    },
    {
        "system": "Redis Remis + Instruct",
        "source": "https://redis.io/resources/agent-memory-benchmark-report/",
        "answer_model": "gpt-4o",
        "judge": "gpt-4o",
        "context_tokens": "not stated (top-5 chunks + neighbours)",
        "retrieval": "yes",
        "score": "86.5 (Remis alone 83.4)",
        "caveats": "same reader and judge for every system; harness not public",
    },
    {
        "system": "EmergenceMem Simple",
        "source": "https://www.emergence.ai/blog/sota-on-longmemeval-with-rag",
        "answer_model": "gpt-4o-2024-08-06",
        "judge": "not stated",
        "context_tokens": "not stated (whole sessions)",
        "retrieval": "yes",
        "score": "82.4",
        "caveats": "reasons before answering; a third party reproduced the Fast variant at 81.3",
    },
    {
        "system": "Mastra Observational Memory",
        "source": "https://mastra.ai/research/observational-memory",
        "answer_model": "gpt-5-mini",
        "judge": "gpt-4o (per-type prompts)",
        "context_tokens": "~30k",
        "retrieval": "no (whole observation log)",
        "score": "94.87",
        "caveats": "7-30x a 1k-4k budget; LLM rewrites history at write time; macro average "
        "over types (micro 93.6); prompts tuned on the same 500 questions; its gpt-4o run "
        "is 84.23, reproduced by a third party at 84.2",
    },
    {
        "system": "MemMachine",
        "source": "https://arxiv.org/abs/2604.04853",
        "answer_model": "gpt-5-mini",
        "judge": "gpt-4o-mini",
        "context_tokens": "5.2k-19.6k",
        "retrieval": "yes",
        "score": "93.0",
        "caveats": "best of 12 configurations chosen on all 500 questions, no held-out half; "
        "judge not the official one; an independent re-run scored 74.3",
    },
    {
        "system": "Hindsight",
        "source": "https://arxiv.org/abs/2512.12818",
        "answer_model": "gemini-3-pro",
        "judge": "gpt-oss-120b",
        "context_tokens": "not reported",
        "retrieval": "yes",
        "score": "91.4",
        "caveats": "judge not the official one; context budget not reported",
    },
    {
        "system": "Chronos",
        "source": "https://arxiv.org/abs/2603.16862",
        "answer_model": "gpt-4o",
        "judge": "claude-opus-4.6",
        "context_tokens": "not stated",
        "retrieval": "yes",
        "score": "92.6",
        "caveats": "an audit (arXiv 2609.38021) reports the published runner read the "
        "oracle file (evidence sessions only)",
    },
    {
        "system": "Mem0",
        "source": "https://dev.to/gaurav_dadhich/the-state-of-ai-memory-in-2026-claimed-vs-observed-1n94",
        "answer_model": "not stated",
        "judge": "not stated",
        "context_tokens": "not stated",
        "retrieval": "yes",
        "score": "93.4 claimed; 57.5-73.8 observed",
        "caveats": "an audit found question-specific rules in the answer prompt; very low trust",
    },
)
