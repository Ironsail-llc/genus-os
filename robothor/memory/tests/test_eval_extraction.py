"""The conversation-extraction measurement: fixtures, matching, the judge's
validation, the metrics, and the shipped labelled set.

No database and no model: the extraction and the LLM judge are stand-ins.
Assertions are about what the harness counts, never about model wording.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from robothor.memory import eval_extraction as ex
from robothor.memory.eval import EvalPreconditionError
from robothor.memory.facts import Extraction

REPO = Path(__file__).resolve().parents[3]
SHIPPED = REPO / "docs" / "benchmarks" / "memory" / "conversation-facts.yaml"

_SMALL = textwrap.dedent(
    """
    version: 1
    conversations:
      - id: pref-a
        category: preference
        at: "2026-03-04T09:00:00+00:00"
        turns:
          - {role: user, text: "I really prefer dark roast coffee in the morning."}
          - {role: assistant, text: "Noted. You could try a French press."}
        facts:
          - fact: "The user prefers dark roast coffee."
            keys: ["dark roast"]
            speaker: user
            trust: user_stated
            turns: [1]
            date: null
            paraphrase: "The user likes dark roast coffee best."
            decoy: "The user dislikes dark roast coffee."
          - fact: "The assistant suggested a French press."
            keys: ["french press"]
            speaker: assistant
            trust: agent_generated
            turns: [2]
            date: null
            required: false
            paraphrase: "A French press was suggested by the assistant."
            decoy: "The user already owns a French press."
      - id: paste-a
        category: pasted
        at: "2026-03-05T10:00:00+00:00"
        third_party_turns: [1]
        turns:
          - role: user
            text: |
              I bought a kettle yesterday. Here's an email I got:
              From: shop@example.com
              Subject: offer
              Our store opens a new branch in Riverton.
        facts:
          - fact: "The user bought a kettle."
            keys: ["kettle"]
            speaker: user
            trust: user_stated
            turns: [1]
            date: "2026-03-04"
            paraphrase: "The user purchased a kettle."
            decoy: "The user sold a kettle."
          - fact: "The store is opening a new branch in Riverton."
            keys: ["riverton", "branch"]
            speaker: user
            trust: external
            turns: [1]
            date: null
            required: false
            paraphrase: "A new store branch is opening in Riverton."
            decoy: "The store is closing its Riverton branch."
      - id: filler-a
        category: filler
        at: "2026-03-06T10:00:00+00:00"
        turns:
          - {role: user, text: "Thanks, that is all for now."}
          - {role: assistant, text: "You're welcome!"}
        facts: []
    """
)


@pytest.fixture
def fixtures(tmp_path: Path) -> list[ex.Conversation]:
    path = tmp_path / "set.yaml"
    path.write_text(_SMALL, encoding="utf-8")
    return ex.load_fixtures(path)


def _x(text: str, role: str = "user", turns: list[int] | None = None, **kw: Any) -> dict[str, Any]:
    """An extracted fact as ``parse_conversation_response`` returns one."""
    return {
        "fact_text": text,
        "speaker_role": role,
        "turn_refs": turns or [1],
        "event_time": kw.get("event_time"),
        "about_user": kw.get("about_user", role == "user"),
        "kind": kw.get("kind", "other"),
    }


async def _keyword_llm(case: Any, ans: Any) -> tuple[bool, str]:
    """A stand-in LLM judge: same thing when it shares two words with the gold
    and carries no negating word the gold lacks."""
    gold = set(ex.normalise(case.gold[0]).split())
    got = set(ex.normalise(ans.text).split())
    flips = {"dislikes", "sold", "closing", "already", "not", "never", "owns"}
    if (got & flips) - gold:
        return False, "llm: contradicts"
    return len(gold & got) >= 3, "llm: overlap"


# ═══ fixtures ════════════════════════════════════════════════════════════


def test_fixtures_load_with_turns_in_order_and_owner_flags(fixtures) -> None:
    pref = fixtures[0]
    assert [c.id for c in fixtures] == ["pref-a", "paste-a", "filler-a"]
    assert [t.ref for t in pref.turns] == [1, 2]
    assert [t.role for t in pref.turns] == ["user", "assistant"]
    assert all(t.author_is_owner for t in pref.turns)
    assert pref.turns[0].at is not None and pref.turns[0].at.tzinfo is not None
    assert pref.turns[1].at > pref.turns[0].at  # later turns, later times
    assert pref.facts[0].id == "pref-a#1" and pref.facts[0].required
    assert pref.facts[1].required is False
    assert fixtures[1].third_party_turns == (1,)


def test_a_guest_turn_is_not_the_owners(tmp_path: Path) -> None:
    path = tmp_path / "g.yaml"
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            conversations:
              - id: guest-a
                category: guests
                at: "2026-03-04T09:00:00+00:00"
                turns:
                  - {role: user, text: "Hi, I am here with my friend."}
                  - {role: user, speaker: Bob, owner: false, text: "I just adopted a cat."}
                facts:
                  - fact: "Bob adopted a cat."
                    keys: ["cat"]
                    speaker: Bob
                    trust: external
                    turns: [2]
                    date: null
                    paraphrase: "Bob has newly adopted a cat."
                    decoy: "Bob gave away his cat."
            """
        ),
        encoding="utf-8",
    )
    (conv,) = ex.load_fixtures(path)
    assert conv.turns[1].author_is_owner is False and conv.turns[1].speaker == "Bob"
    assert ex.turn_speaker(conv.turns[1]) == "Bob"
    assert ex.turn_speaker(conv.turns[0]) == "user"


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"trust": "trusted"}, "trust"),
        ({"turns": [9]}, "turn"),
        ({"keys": []}, "keys"),
        ({"paraphrase": ""}, "paraphrase"),
        ({"date": "June"}, "date"),
    ],
)
def test_a_badly_labelled_fact_is_refused(tmp_path: Path, patch: dict, message: str) -> None:
    import yaml

    data = yaml.safe_load(_SMALL)
    data["conversations"][0]["facts"][0].update(patch)
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        ex.load_fixtures(path)


def test_duplicate_conversation_ids_are_refused(tmp_path: Path) -> None:
    import yaml

    data = yaml.safe_load(_SMALL)
    data["conversations"][1]["id"] = "pref-a"
    path = tmp_path / "dup.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        ex.load_fixtures(path)


# ═══ the judge: rules first, then the model ══════════════════════════════


def test_trust_follows_the_extractions_speaker_role() -> None:
    assert ex.trust_of(_x("a", "user")) == "user_stated"
    assert ex.trust_of(_x("a", "assistant")) == "agent_generated"
    assert ex.trust_of(_x("a", "external")) == "external"


def test_an_extracted_fact_sharing_no_key_is_settled_without_the_model(fixtures) -> None:
    fact = fixtures[0].facts[0]
    verdict = ex.rule_match(fact, "The user bought a kettle.")
    assert verdict is not None and verdict.correct is False and verdict.method == "rule"


def test_the_gold_sentence_itself_is_settled_without_the_model(fixtures) -> None:
    fact = fixtures[0].facts[0]
    verdict = ex.rule_match(fact, "the user prefers DARK ROAST coffee")
    assert verdict is not None and verdict.correct is True


def test_a_sentence_sharing_a_key_goes_to_the_model(fixtures) -> None:
    assert ex.rule_match(fixtures[0].facts[0], "The user drinks dark roast daily.") is None


@pytest.mark.asyncio
async def test_without_a_model_judge_an_unsettled_pair_is_no_match(fixtures) -> None:
    judge = ex.make_fact_judge(None)
    verdict = await judge(fixtures[0].facts[0], "The user drinks dark roast daily.")
    assert verdict.correct is False


@pytest.mark.asyncio
async def test_the_model_judge_gets_the_gold_and_the_extracted_sentence(fixtures) -> None:
    seen: list[tuple[str, str]] = []

    async def llm(case: Any, ans: Any) -> tuple[bool, str]:
        seen.append((case.gold[0], ans.text))
        return True, "llm: same"

    judge = ex.make_fact_judge(llm)
    verdict = await judge(fixtures[0].facts[0], "The user drinks dark roast daily.")
    assert verdict.correct is True and verdict.method == "llm"
    assert seen == [("The user prefers dark roast coffee.", "The user drinks dark roast daily.")]


# ═══ the judge is validated on planted matches ═══════════════════════════


def test_planted_matches_cover_right_and_wrong_kinds(fixtures) -> None:
    planted = ex.planted_matches(fixtures)
    kinds = {p.kind for p in planted}
    assert {"paraphrase", "decoy", "sibling"} <= kinds
    assert all(p.expect_match for p in planted if p.kind == "paraphrase")
    assert not any(p.expect_match for p in planted if p.kind != "paraphrase")
    # A sibling is another fact of the same conversation.
    sib = next(p for p in planted if p.kind == "sibling")
    assert sib.text != sib.fact.fact and sib.fact.id.split("#")[0] == "pref-a"


def test_planted_matches_are_the_same_every_run(fixtures) -> None:
    assert ex.planted_matches(fixtures) == ex.planted_matches(fixtures)


@pytest.mark.asyncio
async def test_a_good_judge_passes_validation(fixtures) -> None:
    result = await ex.validate_fact_judge(fixtures, ex.make_fact_judge(_keyword_llm))
    assert result.passed
    assert result.right_marked_right == 1.0 and result.wrong_marked_wrong == 1.0
    assert result.by_kind["paraphrase"]["llm"] >= 1  # the model was actually exercised


@pytest.mark.asyncio
async def test_a_judge_that_accepts_everything_fails_and_the_run_aborts(fixtures) -> None:
    async def yes(case: Any, ans: Any) -> tuple[bool, str]:
        return True, "llm: yes"

    with pytest.raises(EvalPreconditionError, match="judge failed validation"):
        await ex.require_valid_fact_judge(fixtures, ex.make_fact_judge(yes))


@pytest.mark.asyncio
async def test_a_judge_that_errors_has_failed_those_cases(fixtures) -> None:
    async def broken(case: Any, ans: Any) -> tuple[bool, str]:
        raise RuntimeError("unparseable")

    result = await ex.validate_fact_judge(fixtures, ex.make_fact_judge(broken))
    assert result.passed is False


# ═══ scoring one conversation ════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_matched_fact_scores_trust_speaker_turns_and_date(fixtures) -> None:
    paste = fixtures[1]
    facts = [_x("The user bought a kettle.", "user", [1], event_time="2026-03-04")]
    score = await ex.score_conversation(paste, facts, ex.make_fact_judge(_keyword_llm))
    (kettle, branch) = score.expected
    assert kettle.matched and kettle.trust_ok and kettle.speaker_ok and kettle.turns_ok
    assert kettle.date_ok is True
    assert branch.matched is False and branch.required is False
    assert score.extracted[0].correct is True


@pytest.mark.asyncio
async def test_a_wrong_date_and_a_missing_date_are_both_date_errors(fixtures) -> None:
    paste = fixtures[1]
    judge = ex.make_fact_judge(_keyword_llm)
    wrong = await ex.score_conversation(
        paste, [_x("The user bought a kettle.", event_time="2026-03-03T00:00:00")], judge
    )
    missing = await ex.score_conversation(paste, [_x("The user bought a kettle.")], judge)
    assert wrong.expected[0].date_ok is False
    assert missing.expected[0].date_ok is False


@pytest.mark.asyncio
async def test_an_invented_date_on_an_undated_fact_is_a_date_error(fixtures) -> None:
    pref = fixtures[0]
    facts = [_x("The user prefers dark roast coffee.", event_time="2026-03-04")]
    score = await ex.score_conversation(pref, facts, ex.make_fact_judge(_keyword_llm))
    assert score.expected[0].date_ok is False


@pytest.mark.asyncio
async def test_third_party_content_stated_as_the_users_is_counted(fixtures) -> None:
    paste = fixtures[1]
    facts = [
        _x("The user bought a kettle.", event_time="2026-03-04"),
        _x("The store is opening a new branch in Riverton.", "user"),
    ]
    score = await ex.score_conversation(paste, facts, ex.make_fact_judge(_keyword_llm))
    branch = score.expected[1]
    assert branch.matched and branch.trust_ok is False and branch.misattributed is True
    assert score.misattributed == 1


@pytest.mark.asyncio
async def test_an_unlabelled_user_fact_from_a_relaying_turn_is_flagged(fixtures) -> None:
    paste = fixtures[1]
    facts = [_x("The user likes the shop's weekly newsletter.", "user", [1])]
    score = await ex.score_conversation(paste, facts, ex.make_fact_judge(_keyword_llm))
    assert score.extracted[0].correct is False
    assert score.extracted[0].from_third_party_turn is True


@pytest.mark.asyncio
async def test_an_assistant_turn_cited_for_a_user_fact_is_a_speaker_error(fixtures) -> None:
    pref = fixtures[0]
    facts = [_x("The user prefers dark roast coffee.", "assistant", [1, 2])]
    score = await ex.score_conversation(pref, facts, ex.make_fact_judge(_keyword_llm))
    first = score.expected[0]
    assert first.matched and first.speaker_ok is False and first.turns_ok is False
    assert first.trust_ok is False


@pytest.mark.asyncio
async def test_an_allowed_fact_is_not_a_false_positive_nor_counted_for_recall(fixtures) -> None:
    pref = fixtures[0]
    facts = [_x("The assistant suggested a French press.", "assistant", [2])]
    score = await ex.score_conversation(pref, facts, ex.make_fact_judge(_keyword_llm))
    assert score.extracted[0].correct is True
    assert score.expected[0].matched is False  # the required one is still missed


@pytest.mark.asyncio
async def test_a_second_fact_for_the_same_label_is_a_duplicate(fixtures) -> None:
    pref = fixtures[0]
    facts = [
        _x("The user prefers dark roast coffee."),
        _x("The user prefers dark roast coffee in the morning."),
    ]
    score = await ex.score_conversation(pref, facts, ex.make_fact_judge(_keyword_llm))
    assert [x.correct for x in score.extracted] == [True, True]
    assert [x.duplicate for x in score.extracted] == [False, True]


@pytest.mark.asyncio
async def test_details_a_fact_must_keep_are_checked(tmp_path: Path) -> None:
    import yaml

    data = yaml.safe_load(_SMALL)
    data["conversations"][0]["facts"][0]["details"] = [["morning", "mornings"]]
    path = tmp_path / "d.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    pref = ex.load_fixtures(path)[0]
    judge = ex.make_fact_judge(_keyword_llm)
    kept = await ex.score_conversation(
        pref, [_x("The user prefers dark roast coffee in the morning.")], judge
    )
    lost = await ex.score_conversation(pref, [_x("The user prefers dark roast coffee.")], judge)
    assert kept.expected[0].details_ok is True
    assert lost.expected[0].details_ok is False


# ═══ the run and its report ══════════════════════════════════════════════


def _stub_extract(answers: dict[str, list[dict[str, Any]]], seen: list[Any] | None = None):
    async def extract(window, *, model=None, **_kw):
        if seen is not None:
            seen.append(model)
        text = window[0].text
        for key, facts in answers.items():
            if key in text:
                return Extraction(facts)
        return Extraction([])

    return extract


@pytest.fixture
def small_path(tmp_path: Path) -> Path:
    path = tmp_path / "set.yaml"
    path.write_text(_SMALL, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _local_generation(monkeypatch):
    from robothor.memory import generation

    monkeypatch.setattr(generation, "_remote_enabled", lambda: False)


@pytest.mark.asyncio
async def test_the_run_reports_every_metric(small_path: Path) -> None:
    seen: list[Any] = []
    extract = _stub_extract(
        {
            "dark roast": [_x("The user prefers dark roast coffee.")],
            "kettle": [
                _x("The user bought a kettle.", event_time="2026-03-04"),
                _x("The store is opening a new branch in Riverton.", "user"),
            ],
            "Thanks": [_x("The user is finished for now.")],
        },
        seen,
    )
    report = await ex.run_extraction_eval(
        small_path, model="local-model:1b", llm=_keyword_llm, extract=extract
    )
    m = report["metrics"]
    assert seen == ["local-model:1b"] * 3
    assert report["model"] == "local-model:1b"
    assert report["judge_validation"]["passed"] is True
    assert m["expected"] == 2 and m["extracted"] == 4
    assert m["recall"] == pytest.approx(1.0)
    assert m["precision"] == pytest.approx(3 / 4)  # the filler fact is wrong
    assert m["misattributed_to_operator"] == 1
    assert m["trust_accuracy"] == pytest.approx(2 / 3)
    assert m["date_accuracy"] == pytest.approx(1.0)
    assert m["facts_per_conversation"] == pytest.approx(4 / 3)
    assert m["filler_facts"] == 1
    assert report["by_category"]["filler"]["extracted"] == 1
    assert any(f["kind"] == "false_positive" for f in report["failures"])


@pytest.mark.asyncio
async def test_repeats_run_each_conversation_again(small_path: Path) -> None:
    seen: list[Any] = []
    report = await ex.run_extraction_eval(
        small_path, model="m", llm=_keyword_llm, extract=_stub_extract({}, seen), repeats=2
    )
    assert len(seen) == 6
    assert len(report["per_repeat"]) == 2
    assert report["metrics"]["recall"] == 0.0


@pytest.mark.asyncio
async def test_an_extraction_error_counts_as_nothing_extracted(small_path: Path) -> None:
    async def failing(window, **_kw):
        return Extraction([], "timeout")

    report = await ex.run_extraction_eval(small_path, model="m", llm=_keyword_llm, extract=failing)
    assert report["metrics"]["extraction_errors"] == 3
    assert report["metrics"]["recall"] == 0.0


@pytest.mark.asyncio
async def test_a_failing_judge_stops_the_run_before_any_extraction(small_path: Path) -> None:
    seen: list[Any] = []

    async def yes(case: Any, ans: Any) -> tuple[bool, str]:
        return True, "llm: yes"

    with pytest.raises(EvalPreconditionError):
        await ex.run_extraction_eval(
            small_path, model="m", llm=yes, extract=_stub_extract({}, seen)
        )
    assert seen == []


@pytest.mark.asyncio
async def test_a_named_model_is_refused_when_remote_generation_would_ignore_it(
    small_path: Path, monkeypatch
) -> None:
    from robothor.memory import generation

    monkeypatch.setattr(generation, "_remote_enabled", lambda: True)
    with pytest.raises(EvalPreconditionError, match="remote"):
        await ex.run_extraction_eval(
            small_path, model="m", llm=_keyword_llm, extract=_stub_extract({})
        )


@pytest.mark.asyncio
async def test_the_markdown_summary_names_the_model_and_the_counts(small_path: Path) -> None:
    report = await ex.run_extraction_eval(
        small_path,
        model="local-model:1b",
        llm=_keyword_llm,
        extract=_stub_extract({"dark roast": [_x("The user prefers dark roast coffee.")]}),
    )
    md = ex.format_extraction_markdown(report)
    assert "local-model:1b" in md
    assert "Precision" in md and "Recall" in md and "Trust" in md
    assert "3 conversations" in md
    assert "Judge validation" in md


# ═══ the shipped labelled set ════════════════════════════════════════════


def test_the_shipped_set_loads_and_covers_every_category() -> None:
    conversations = ex.load_fixtures(SHIPPED)
    assert 40 <= len(conversations) <= 60
    categories = {c.category for c in conversations}
    assert set(ex.CATEGORIES) <= categories
    filler = [c for c in conversations if c.category == "filler"]
    assert filler and all(not c.facts for c in filler)
    trusts = {f.trust for c in conversations for f in c.facts}
    assert trusts == {"user_stated", "agent_generated", "external"}
    assert any(f.dates != (None,) for c in conversations for f in c.facts)


def test_the_shipped_set_uses_generic_people_and_domains_only() -> None:
    import re

    text = SHIPPED.read_text(encoding="utf-8")
    for domain in re.findall(r"@([a-z0-9.-]+)", text):
        assert domain.endswith(("example.com", "example.org", "example.net")), domain


def test_every_shipped_fact_is_reachable_by_its_own_paraphrase_keys() -> None:
    """A paraphrase the key rule would reject could never be matched: the label is broken."""
    for conv in ex.load_fixtures(SHIPPED):
        for fact in conv.facts:
            assert ex.rule_match(fact, fact.paraphrase) is None, fact.id


@pytest.mark.asyncio
async def test_the_model_judge_is_told_a_claim_about_someone_else_is_wrong(fixtures) -> None:
    """The local judge accepted "The user is allergic to peanuts" for "Bob is
    allergic to peanuts" until the question said who the fact must be about."""
    questions: list[str] = []

    async def llm(case: Any, ans: Any) -> tuple[bool, str]:
        questions.append(case.question)
        return False, "llm: no"

    await ex.make_fact_judge(llm)(fixtures[0].facts[0], "The user drinks dark roast daily.")
    assert "same person" in questions[0] and "different person" in questions[0]


# ═══ a named model that does not answer, and alerts during a measurement ═


@pytest.mark.asyncio
async def test_a_named_model_that_does_not_answer_stops_the_run_first(
    small_path: Path, monkeypatch
) -> None:
    from robothor.llm import ollama

    asked: list[Any] = []

    async def down(*_a, **kw):
        asked.append(kw.get("model"))
        raise ConnectionError("connection refused")

    async def never(*_a, **_kw):
        raise AssertionError("no extraction may run against a model that did not answer")

    monkeypatch.setattr(ollama, "chat", down)
    monkeypatch.setattr(ex, "extract_conversation_facts", never)
    with pytest.raises(EvalPreconditionError, match="local-model:1b.*did not answer"):
        await ex.run_extraction_eval(small_path, model="local-model:1b", llm=_keyword_llm)
    assert asked == ["local-model:1b"]


@pytest.mark.asyncio
async def test_the_named_model_is_probed_with_one_short_call(small_path: Path, monkeypatch) -> None:
    from robothor.llm import ollama

    calls: list[dict[str, Any]] = []

    async def ok(messages, **kw):
        calls.append(kw)
        return "ok"

    async def extract(window, **_kw):
        return Extraction([])

    monkeypatch.setattr(ollama, "chat", ok)
    monkeypatch.setattr(ex, "extract_conversation_facts", extract)
    await ex.run_extraction_eval(small_path, model="local-model:1b", llm=_keyword_llm)
    assert calls and calls[0]["model"] == "local-model:1b" and calls[0]["max_tokens"] <= 8


@pytest.mark.asyncio
async def test_the_extraction_runs_as_benchmark_traffic(small_path: Path) -> None:
    from robothor.engine.run_context import in_benchmark_run

    seen: list[bool] = []

    async def extract(window, **_kw):
        seen.append(in_benchmark_run())
        return Extraction([])

    await ex.run_extraction_eval(small_path, model="m", llm=_keyword_llm, extract=extract)
    assert seen == [True, True, True]
    assert in_benchmark_run() is False  # restored afterwards


@pytest.mark.asyncio
async def test_a_failing_local_leg_during_a_measurement_never_pages_the_operator(
    small_path: Path, monkeypatch
) -> None:
    from robothor.engine import alerts
    from robothor.memory import generation

    paged: list[str] = []
    rows: list[str] = []

    async def page(level, title, body, alert_id):
        paged.append(title)
        return True

    async def note(notification_type, level, title, body, metadata):
        rows.append(notification_type)
        return True

    async def boom() -> str:
        raise ConnectionError("model not loaded")

    async def extract(window, **_kw):
        try:
            await generation._local_leg(boom(), None)
        except ConnectionError:
            return Extraction([], "ConnectionError")
        return Extraction([])

    monkeypatch.setattr(alerts, "_page", page)
    monkeypatch.setattr(alerts, "_write_notification", note)
    generation._release_latch(generation.GENERATION_DOWN_ALERT_KEY)
    try:
        await ex.run_extraction_eval(small_path, model="m", llm=_keyword_llm, extract=extract)
    finally:
        generation._release_latch(generation.GENERATION_DOWN_ALERT_KEY)
    assert paged == []
    assert rows and set(rows) == {alerts.BENCHMARK_DIGEST_TYPE}


# ═══ a judge that raises while scoring is reported, not hidden ═══════════


@pytest.mark.asyncio
async def test_a_judge_error_while_scoring_is_logged_and_in_the_summary(
    small_path: Path, caplog
) -> None:
    import logging

    async def flaky(case: Any, ans: Any) -> tuple[bool, str]:
        if "breaks the judge" in ans.text:
            raise RuntimeError("LLM judge returned unparseable output")
        return await _keyword_llm(case, ans)

    extract = _stub_extract({"dark roast": [_x("The user's dark roast habit breaks the judge.")]})
    with caplog.at_level(logging.WARNING, logger="robothor.memory.eval_extraction"):
        report = await ex.run_extraction_eval(small_path, model="m", llm=flaky, extract=extract)
    assert report["metrics"]["judge_errors"] == 1
    assert "| Judge errors | 1 |" in ex.format_extraction_markdown(report)
    assert any("unparseable" in r.getMessage() for r in caplog.records)


# ═══ a broken label is a ValueError naming it, never a crash ═════════════


def _broken(tmp_path: Path, edit) -> Path:
    import yaml

    data = yaml.safe_load(_SMALL)
    edit(data)
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("edit", "names"),
    [
        (lambda d: d["conversations"][0]["facts"][0].update(trust=[]), "pref-a#1"),
        (lambda d: d["conversations"][0]["facts"][0].update(speaker=[]), "pref-a#1"),
        (lambda d: d["conversations"][0]["facts"][0].update(trust=None), "pref-a#1"),
        (lambda d: d["conversations"][0].pop("at"), "pref-a"),
        (lambda d: d["conversations"][0].update(at="not a time"), "pref-a"),
        (lambda d: d["conversations"][0]["turns"].append("just a string"), "pref-a"),
        (lambda d: d["conversations"][0].update(turns="nope"), "pref-a"),
        (lambda d: d["conversations"][0]["facts"].append("just a string"), "pref-a"),
        (lambda d: d["conversations"][1].update(third_party_turns=[7]), "paste-a"),
        (lambda d: d["conversations"][1].update(third_party_turns=["one"]), "paste-a"),
        (lambda d: d["conversations"].append("not a conversation"), "conversation"),
        (lambda d: d.update(conversations="nope"), "conversations"),
    ],
)
def test_a_broken_label_is_a_value_error_naming_it(tmp_path: Path, edit, names: str) -> None:
    with pytest.raises(ValueError, match=names):
        ex.load_fixtures(_broken(tmp_path, edit))


def test_a_yaml_syntax_error_is_a_value_error_naming_the_file(tmp_path: Path) -> None:
    path = tmp_path / "syntax.yaml"
    path.write_text("version: 1\nconversations: [\n  - id: x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="syntax.yaml"):
        ex.load_fixtures(path)


def test_a_file_that_is_not_a_mapping_is_a_value_error(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="list.yaml"):
        ex.load_fixtures(path)


# ═══ the recorded baseline: regressions fail, proposed thresholds report ═

BASELINE = REPO / "docs" / "benchmarks" / "memory" / "conversation-facts-baseline.yaml"


def _baseline(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": 1,
        "suite_id": ex.SUITE_ID,
        "model": "m",
        "split": "dev",
        "runs": 3,
        "metrics": {"recall": 0.80, "precision": 0.60, "misattributed_to_operator": 1.0},
        "tolerance": {"recall": 0.10, "precision": 0.05, "misattributed_to_operator": 1.0},
        "thresholds": {
            "status": "PROPOSED",
            "min": {"recall": 0.85},
            "max": {"misattributed_to_operator": 0},
        },
    }
    base.update(over)
    return base


def _report(**metrics: Any) -> dict[str, Any]:
    return {"model": "m", "split": "dev", "repeats": 2, "comparable": metrics}


def test_a_run_inside_the_tolerance_does_not_regress() -> None:
    out = ex.compare_to_baseline(
        _report(recall=0.71, precision=0.56, misattributed_to_operator=1.5), _baseline()
    )
    assert out["regressed"] is False
    assert {r["metric"] for r in out["rows"]} == {
        "recall",
        "precision",
        "misattributed_to_operator",
    }


@pytest.mark.parametrize(
    "metrics",
    [
        {"recall": 0.69, "precision": 0.60, "misattributed_to_operator": 1.0},
        {"recall": 0.80, "precision": 0.54, "misattributed_to_operator": 1.0},
        {"recall": 0.80, "precision": 0.60, "misattributed_to_operator": 2.5},
    ],
)
def test_a_metric_beyond_its_tolerance_regresses(metrics: dict[str, float]) -> None:
    out = ex.compare_to_baseline(_report(**metrics), _baseline())
    assert out["regressed"] is True
    assert sum(r["regressed"] for r in out["rows"]) == 1


def test_proposed_thresholds_are_reported_and_never_fail_the_run() -> None:
    out = ex.compare_to_baseline(
        _report(recall=0.80, precision=0.60, misattributed_to_operator=1.0), _baseline()
    )
    assert out["regressed"] is False
    assert out["thresholds_status"] == "PROPOSED"
    by = {t["metric"]: t for t in out["thresholds"]}
    assert by["recall"]["met"] is False and by["misattributed_to_operator"]["met"] is False


@pytest.mark.parametrize(
    "over", [{"model": "other"}, {"split": "held_out"}, {"suite_id": "something-else"}]
)
def test_a_baseline_for_another_model_or_split_cannot_be_compared(over: dict[str, Any]) -> None:
    with pytest.raises(EvalPreconditionError, match="baseline"):
        ex.compare_to_baseline(
            _report(recall=0.8, precision=0.6, misattributed_to_operator=1.0), _baseline(**over)
        )


def test_counts_are_compared_per_run() -> None:
    metrics = {"misattributed_to_operator": 3, "guest_as_user": 0, "filler_facts": 0}
    flat = ex.comparable_metrics({**_full_metrics(), **metrics}, repeats=3)
    assert flat["misattributed_to_operator"] == pytest.approx(1.0)


def _full_metrics() -> dict[str, Any]:
    return {
        "recall": 0.8,
        "precision": 0.6,
        "trust_accuracy": 0.9,
        "speaker_accuracy": 1.0,
        "turn_accuracy": 1.0,
        "date_accuracy": 0.5,
        "dated_correct": [9, 10],
        "undated_left_undated": [5, 10],
        "precision_by_trust": {"user_stated": [9, 10]},
        "misattributed_to_operator": 0,
        "guest_as_user": 0,
        "filler_facts": 0,
    }


def test_rates_from_pairs_are_flattened() -> None:
    flat = ex.comparable_metrics(_full_metrics(), repeats=1)
    assert flat["dated_correct_rate"] == pytest.approx(0.9)
    assert flat["undated_left_undated_rate"] == pytest.approx(0.5)
    assert flat["user_stated_precision"] == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_a_guest_claim_written_as_the_users_is_counted(tmp_path: Path) -> None:
    path = tmp_path / "g.yaml"
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            conversations:
              - id: guest-a
                category: guests
                at: "2026-03-04T09:00:00+00:00"
                turns:
                  - {role: user, text: "Hi, I am here with my friend."}
                  - {role: user, speaker: Bob, owner: false, text: "I just adopted a cat."}
                facts:
                  - fact: "Bob adopted a cat."
                    keys: ["cat"]
                    speaker: Bob
                    trust: external
                    turns: [2]
                    date: null
                    paraphrase: "Bob has adopted a new cat."
                    decoy: "Bob gave away his cat."
            """
        ),
        encoding="utf-8",
    )
    extract = _stub_extract({"Hi": [_x("The user adopted a cat recently.", "external", [2])]})
    report = await ex.run_extraction_eval(path, model="m", llm=_keyword_llm, extract=extract)
    assert report["metrics"]["guest_as_user"] == 1


@pytest.mark.asyncio
async def test_precision_is_also_reported_by_stored_origin(small_path: Path) -> None:
    extract = _stub_extract(
        {
            "dark roast": [
                _x("The user prefers dark roast coffee."),
                _x("The assistant said hello to the user.", "assistant", [2]),
            ]
        }
    )
    report = await ex.run_extraction_eval(small_path, model="m", llm=_keyword_llm, extract=extract)
    by = report["metrics"]["precision_by_trust"]
    assert by["user_stated"] == [1, 1] and by["agent_generated"] == [0, 1]


@pytest.mark.asyncio
async def test_the_run_report_carries_the_comparable_metrics_and_per_run_values(
    small_path: Path,
) -> None:
    report = await ex.run_extraction_eval(
        small_path, model="m", llm=_keyword_llm, extract=_stub_extract({}), repeats=2
    )
    assert set(report["comparable"]) >= {"recall", "misattributed_to_operator", "guest_as_user"}
    assert all(set(r) >= {"recall", "guest_as_user"} for r in report["per_repeat"])


def test_a_new_baseline_takes_its_tolerance_from_the_runs_spread() -> None:
    report = {
        "suite_id": ex.SUITE_ID,
        "model": "m",
        "judge_model": "j",
        "split": "dev",
        "repeats": 3,
        "comparable": {"recall": 0.85, "misattributed_to_operator": 1.0},
        "per_repeat": [
            {"recall": 0.80, "misattributed_to_operator": 1},
            {"recall": 0.909, "misattributed_to_operator": 2},
            {"recall": 0.855, "misattributed_to_operator": 2},
        ],
    }
    out = ex.baseline_from_report(report, thresholds=_baseline()["thresholds"])
    assert out["metrics"]["recall"] == pytest.approx(0.85)
    assert out["tolerance"]["recall"] == pytest.approx(0.11)  # range 0.109, up to a point
    assert out["tolerance"]["misattributed_to_operator"] == pytest.approx(1.0)
    assert out["thresholds"]["status"] == "PROPOSED"


def test_a_spread_of_nothing_still_allows_one_unit() -> None:
    report = {
        "suite_id": ex.SUITE_ID,
        "model": "m",
        "judge_model": "j",
        "split": "dev",
        "repeats": 2,
        "comparable": {"recall": 1.0, "filler_facts": 0.0},
        "per_repeat": [{"recall": 1.0, "filler_facts": 0}, {"recall": 1.0, "filler_facts": 0}],
    }
    out = ex.baseline_from_report(report, thresholds={"status": "PROPOSED"})
    assert out["tolerance"] == {"recall": 0.01, "filler_facts": 1.0}


def test_the_committed_baseline_loads_and_its_thresholds_are_only_proposed() -> None:
    base = ex.load_baseline(BASELINE)
    assert base["model"] == "qwen3:8b" and base["split"] == "dev" and base["runs"] >= 2
    assert set(base["tolerance"]) == set(base["metrics"])
    assert base["thresholds"]["status"] == "PROPOSED"
    assert "operator" in base["thresholds"]["note"]


def test_a_baseline_without_its_parts_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "b.yaml"
    path.write_text("version: 1\nmodel: m\n", encoding="utf-8")
    with pytest.raises(ValueError, match="b.yaml"):
        ex.load_baseline(path)
