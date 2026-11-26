"""The replay dry run: one question, two recall configurations, side by side.

Unit tests: the search is a fake that records how it was called, so these pin
the report (what each side returned and how the two differ), the presets, the
alternating run order, redaction, the JSON shape and deterministic sampling
without a database or a model. The database half (writes nothing, one tenant,
each question read as its own reader) is test_replay_integration.py.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from robothor.memory import replay
from robothor.memory.replay import Question, Reader

TENANT = "test-tenant"


def _fact(fid: int, text: str, legs: dict[str, int], relevance: float = 0.5) -> dict[str, Any]:
    return {
        "id": fid,
        "kind": "fact",
        "fact_text": text,
        "leg_ranks": legs,
        "leg_scores": {},
        "relevance": relevance,
        "source": None,
    }


def _segment(sid: int, text: str) -> dict[str, Any]:
    return {
        "id": sid,
        "kind": "segment",
        "fact_text": text,
        "leg_ranks": {"segment": 0},
        "leg_scores": {"segment": 0.03},
        "relevance": 0.9,
        "source": "segment",
    }


SHARED_1 = _fact(1, "Alice keeps the spare key under the blue flowerpot", {"vector": 0}, 0.8)
SHARED_2 = _fact(2, "The garage door code was changed in March", {"keyword": 0}, 0.6)
TODAY_ONLY = _fact(3, "Bob prefers tea over coffee", {"entity_expansion": 0}, 0.3)
SEGMENT = _segment(7, "[user] remind me, the bike locker code is 4417")


class FakeSearch:
    """Answers like search_facts: segments only when they are switched on."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"query": query, **kwargs})
        if kwargs.get("include_segments"):
            return [dict(SEGMENT), dict(SHARED_2), dict(SHARED_1)]
        return [dict(SHARED_1), dict(SHARED_2), dict(TODAY_ONLY)]


def _questions(n: int = 2) -> list[Question]:
    return [
        Question(
            text=f"what is the bike locker code? ({i})",
            reader=Reader(person_id="11111111-1111-4111-8111-111111111111"),
            origin={"source": "file", "line": i + 1},
        )
        for i in range(n)
    ]


async def _run(questions: list[Question] | None = None, **kwargs: Any) -> dict[str, Any]:
    search = kwargs.pop("search", None) or FakeSearch()
    return await replay.run_replay(
        TENANT, questions if questions is not None else _questions(), search=search, **kwargs
    )


# --------------------------------------------------------------------------- #
# Acceptance 1: per question, what each path returned and the differences
# --------------------------------------------------------------------------- #


class TestPerQuestionReport:
    async def test_each_side_lists_what_it_returned(self) -> None:
        report = await _run()
        entry = report["questions"][0]
        today = entry["sides"]["today"]["results"]
        new = entry["sides"]["new"]["results"]

        assert [r["key"] for r in today] == ["fact:1", "fact:2", "fact:3"]
        assert [r["key"] for r in new] == ["segment:7", "fact:2", "fact:1"]
        top = new[0]
        assert top["rank"] == 1
        assert top["kind"] == "segment"
        assert top["legs"] == ["segment"]
        assert top["relevance"] == pytest.approx(0.9)
        assert "bike locker code" in top["excerpt"]
        assert top["chars"] == len(SEGMENT["fact_text"])

    async def test_the_differences_are_named(self) -> None:
        diff = (await _run())["questions"][0]["diff"]

        # {1, 2, 3} against {7, 2, 1}: two shared of four distinct.
        assert diff["overlap"] == pytest.approx(0.5)
        assert [r["key"] for r in diff["only_a"]] == ["fact:3"]
        assert [r["key"] for r in diff["only_b"]] == ["segment:7"]
        changes = {c["key"]: c for c in diff["rank_changes"]}
        assert changes["fact:1"]["rank_a"] == 1 and changes["fact:1"]["rank_b"] == 3
        assert changes["fact:1"]["delta"] == -2
        assert "fact:2" not in changes  # rank 2 on both sides
        assert diff["top_changed"] is True

    async def test_latency_and_context_tokens_per_side(self) -> None:
        sides = (await _run())["questions"][0]["sides"]
        for side in sides.values():
            assert side["latency_ms"] >= 0
            assert side["context_tokens"] > 0
        texts = "".join(r["fact_text"] for r in (SEGMENT, SHARED_2, SHARED_1))
        assert sides["new"]["context_tokens"] == pytest.approx(len(texts) / 4, abs=4)

    async def test_identical_sides_overlap_fully(self) -> None:
        report = await _run(compare=("today", "new-segments"))
        diff = report["questions"][0]["diff"]
        assert diff["overlap"] == 1.0
        assert diff["only_a"] == [] and diff["only_b"] == []
        assert diff["top_changed"] is False

    async def test_two_empty_sides_overlap_fully(self) -> None:
        async def nothing(_query: str, **_kw: Any) -> list[dict[str, Any]]:
            return []

        diff = (await _run(search=nothing))["questions"][0]["diff"]
        assert diff["overlap"] == 1.0
        assert diff["top_changed"] is False


class TestSummary:
    async def test_summary_over_all_questions(self) -> None:
        summary = (await _run(_questions(4)))["summary"]

        assert summary["questions"] == 4
        assert summary["mean_overlap"] == pytest.approx(0.5)
        assert summary["top_changed_share"] == 1.0
        assert summary["segment_share"] == {"today": 0.0, "new": 1.0}
        for name in ("today", "new"):
            assert set(summary["latency_ms"][name]) == {"p50", "p95"}
        assert summary["only_b_by_leg"] == {"segment": 4}
        assert summary["only_a_by_leg"] == {"entity_expansion": 4}

    async def test_an_empty_replay_has_a_summary(self) -> None:
        summary = (await _run([]))["summary"]
        assert summary["questions"] == 0
        assert summary["mean_overlap"] is None

    def test_percentiles_are_nearest_rank(self) -> None:
        values = [float(v) for v in range(1, 101)]
        assert replay.percentile(values, 50) == 50.0
        assert replay.percentile(values, 95) == 95.0
        assert replay.percentile([], 95) is None


class TestRunOrder:
    async def test_sides_alternate_which_runs_first(self) -> None:
        search = FakeSearch()
        report = await _run(_questions(4), search=search)

        firsts = [e["first"] for e in report["questions"]]
        assert firsts == ["today", "new", "today", "new"]
        segments_on = [bool(c["include_segments"]) for c in search.calls]
        assert segments_on == [False, True, True, False, False, True, True, False]
        assert "alternat" in report["order"]


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #


class TestPresets:
    def test_the_presets_live_in_one_table(self) -> None:
        assert {"today", "new", "new-graph", "new-segments"} <= set(replay.PRESETS)
        assert replay.DEFAULT_COMPARE == ("today", "new")

    def test_today_is_recall_as_main_runs_it(self) -> None:
        kw = replay.preset_kwargs(replay.PRESETS["today"], fan_out=True)
        assert kw["include_segments"] is False
        assert kw["include_graph"] is False
        # main's search_memory handler fans out unless MEMORY_NARROW_SEARCH.
        assert kw["expand_entities"] is True
        assert kw["include_insights"] is True
        assert kw["include_episodes"] is True

    def test_new_is_the_product_default_on_this_tree(self) -> None:
        kw = replay.preset_kwargs(replay.PRESETS["new"], fan_out=True)
        assert kw["include_segments"] is True
        assert kw["include_graph"] is True

    def test_variants(self) -> None:
        no_graph = replay.preset_kwargs(replay.PRESETS["new-graph"], fan_out=True)
        assert no_graph["include_segments"] is True and no_graph["include_graph"] is False
        no_seg = replay.preset_kwargs(replay.PRESETS["new-segments"], fan_out=True)
        assert no_seg["include_segments"] is False and no_seg["include_graph"] is True

    def test_narrow_search_turns_the_fan_out_off_on_both_sides(self) -> None:
        for name in replay.PRESETS:
            kw = replay.preset_kwargs(replay.PRESETS[name], fan_out=False)
            assert not kw["expand_entities"] and not kw["include_insights"]

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            (None, ("today", "new")),
            ("today,new", ("today", "new")),
            ("new-graph, new-segments", ("new-graph", "new-segments")),
        ],
    )
    def test_resolve_compare(self, spec: str | None, expected: tuple[str, str]) -> None:
        assert replay.resolve_compare(spec) == expected

    @pytest.mark.parametrize("spec", ["today", "today,new,new-graph", "today,today", "today,v2"])
    def test_resolve_compare_refuses(self, spec: str) -> None:
        with pytest.raises(ValueError):
            replay.resolve_compare(spec)

    async def test_compare_picks_the_two_presets(self) -> None:
        search = FakeSearch()
        report = await _run(_questions(1), search=search, compare=("new-graph", "new-segments"))
        assert report["compare"] == ["new-graph", "new-segments"]
        assert [c["include_graph"] for c in search.calls] == [False, True]
        assert set(report["questions"][0]["sides"]) == {"new-graph", "new-segments"}
        assert report["presets"]["new-graph"]["search"]["include_graph"] is False
        assert report["presets"]["new-segments"]["graph_leg_runs"] is True


# --------------------------------------------------------------------------- #
# The reader and the limit go to the search as the product passes them
# --------------------------------------------------------------------------- #


class TestReader:
    async def test_each_question_is_searched_as_its_own_reader(self) -> None:
        search = FakeSearch()
        alice = Reader(person_id="11111111-1111-4111-8111-111111111111", room_ids=("r1",))
        bob = Reader(user_id="22222222-2222-4222-8222-222222222222")
        questions = [Question("q1", reader=alice), Question("q2", reader=bob, limit=5)]
        await _run(questions, search=search)

        by_query = {c["query"]: c for c in search.calls}
        assert by_query["q1"]["reader_person_id"] == alice.person_id
        # As search_memory: the person and user are the one reader; no second id.
        assert by_query["q1"].get("reader_scope_id") is None
        assert by_query["q1"]["reader_user_id"] is None
        assert list(by_query["q1"]["room_ids"]) == ["r1"]
        assert by_query["q1"]["limit"] == 10
        assert by_query["q2"]["reader_user_id"] == bob.user_id
        assert by_query["q2"]["reader_person_id"] is None
        assert by_query["q2"]["limit"] == 5
        assert {c["tenant_id"] for c in search.calls} == {TENANT}

    async def test_the_reader_is_in_the_report(self) -> None:
        entry = (await _run(_questions(1)))["questions"][0]
        assert entry["reader"]["person_id"] == "11111111-1111-4111-8111-111111111111"


# --------------------------------------------------------------------------- #
# Redaction, the notice, the JSON shape
# --------------------------------------------------------------------------- #


class TestRedact:
    async def test_redact_replaces_text_with_ids_and_lengths(self) -> None:
        report = await _run(redact=True)
        dumped = json.dumps(report, default=str)

        for row in (SHARED_1, SHARED_2, TODAY_ONLY, SEGMENT):
            assert row["fact_text"] not in dumped
        assert "bike locker" not in dumped  # the question is memory too
        entry = report["questions"][0]
        assert entry["question"] is None and entry["question_chars"] > 0
        top = entry["sides"]["new"]["results"][0]
        assert top["excerpt"] is None
        assert top["key"] == "segment:7" and top["chars"] == len(SEGMENT["fact_text"])
        assert report["redacted"] is True

    async def test_redacted_text_report_has_no_memory_text(self) -> None:
        text = replay.format_text(await _run(redact=True))
        assert "bike locker" not in text and "flowerpot" not in text
        assert "segment:7" in text


class TestNoticeAndShape:
    async def test_the_notice_heads_every_output(self) -> None:
        report = await _run()
        assert report["notice"] == replay.NOTICE
        assert "tenant memory" in replay.NOTICE
        assert replay.format_text(report).splitlines()[0] == replay.NOTICE

    async def test_json_shape(self) -> None:
        report = json.loads(json.dumps(await _run(), default=str))
        assert {
            "notice",
            "tenant",
            "compare",
            "presets",
            "order",
            "redacted",
            "questions",
            "summary",
        } <= set(report)
        entry = report["questions"][0]
        assert {"index", "question", "question_chars", "reader", "origin", "first"} <= set(entry)
        assert {"overlap", "only_a", "only_b", "rank_changes", "top_changed"} <= set(entry["diff"])
        side = entry["sides"]["today"]
        assert {"latency_ms", "context_tokens", "results"} <= set(side)
        assert {"rank", "kind", "id", "key", "legs", "relevance", "excerpt", "chars"} <= set(
            side["results"][0]
        )

    async def test_text_report_names_each_side_and_the_summary(self) -> None:
        text = replay.format_text(await _run())
        assert "today" in text and "new" in text
        assert "overlap" in text.lower()
        assert "p95" in text
        assert "flowerpot" in text  # excerpts appear unless --redact


# --------------------------------------------------------------------------- #
# Sampling and the question file
# --------------------------------------------------------------------------- #


class TestSampling:
    ITEMS = [Question(f"q{i}", origin={"id": i}) for i in range(100)]

    def test_without_sample_the_first_n_are_kept(self) -> None:
        picked = replay.pick_questions(self.ITEMS, limit=5, sample=False, seed=1)
        assert [q.text for q in picked] == ["q0", "q1", "q2", "q3", "q4"]

    def test_the_sample_is_deterministic_for_a_seed(self) -> None:
        one = replay.pick_questions(self.ITEMS, limit=10, sample=True, seed=7)
        two = replay.pick_questions(list(reversed(self.ITEMS)), limit=10, sample=True, seed=7)
        other = replay.pick_questions(self.ITEMS, limit=10, sample=True, seed=8)
        assert [q.text for q in one] == [q.text for q in two]
        assert [q.text for q in one] != [q.text for q in other]
        assert len(one) == 10

    def test_duplicate_questions_from_one_reader_are_replayed_once(self) -> None:
        items = [Question("same"), Question("same"), Question("same", reader=Reader(user_id="u"))]
        assert len(replay.pick_questions(items, limit=10, sample=False, seed=1)) == 2


class TestQuestionFile:
    def test_one_question_per_line(self, tmp_path: Any) -> None:
        path = tmp_path / "questions.txt"
        path.write_text("# comment\nwhere is the spare key?\n\n  when is the dentist?  \n")
        reader = Reader(person_id="11111111-1111-4111-8111-111111111111")
        questions = replay.questions_from_file(path, reader=reader)

        assert [q.text for q in questions] == ["where is the spare key?", "when is the dentist?"]
        assert all(q.reader == reader for q in questions)
        assert questions[0].origin == {"source": "file", "line": 2}

    def test_since_accepts_a_duration_or_a_date(self) -> None:
        from datetime import UTC, datetime

        now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
        assert replay.parse_since("7d", now=now) == datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
        assert replay.parse_since("12h", now=now) == datetime(2026, 11, 24, 0, 0, tzinfo=UTC)
        assert replay.parse_since("2026-11-01", now=now) == datetime(2026, 11, 1, tzinfo=UTC)
        assert replay.parse_since(None, now=now) is None
        with pytest.raises(ValueError):
            replay.parse_since("last tuesday", now=now)


class TestStepArguments:
    """What --from-runs reads out of one persisted tool-call step."""

    def test_a_direct_call(self) -> None:
        args = replay.search_arguments("search_memory", {"query": "spare key", "limit": 3})
        assert args == {"query": "spare key", "limit": 3}

    def test_a_wrapped_call(self) -> None:
        wrapped = {"name": "search_memory", "arguments": json.dumps({"query": "spare key"})}
        assert replay.search_arguments("tool_call", wrapped) == {"query": "spare key"}

    def test_another_tool_is_not_a_search(self) -> None:
        assert replay.search_arguments("store_memory", {"content": "x"}) is None
        assert replay.search_arguments("tool_call", {"name": "store_memory"}) is None


class TestRunPrivateReader:
    """Who a persisted run's question may be read as: never more than the run could."""

    PERSON = "11111111-1111-4111-8111-111111111111"
    UID = "0c9d8e7f-6a5b-4c3d-9e2f-1a0b9c8d7e6f"

    def _reader(self, **run: Any) -> Any:
        base = {
            "trigger_type": "webchat",
            "trigger_detail": f"webchat:agent:main:user:{self.UID}",
            "user_id": self.UID,
            "user_role": "member",
            "person_id": self.PERSON,
            "parent_run_id": None,
        }
        return replay.run_private_reader(**{**base, **run})

    def test_a_webchat_turn_in_the_persons_own_conversation(self) -> None:
        assert self._reader() == (self.PERSON, self.UID)

    @pytest.mark.parametrize("detail_prefix", ["plan:", "plan-exec:", "plan-revise:"])
    def test_a_plan_step_of_that_conversation(self, detail_prefix: str) -> None:
        detail = f"{detail_prefix}agent:main:user:{self.UID}"
        assert self._reader(trigger_detail=detail) == (self.PERSON, self.UID)

    def test_the_operators_main_session(self) -> None:
        main = "agent:main:primary"
        assert self._reader(trigger_detail=f"webchat:{main}", user_role="owner") == (
            self.PERSON,
            self.UID,
        )
        assert self._reader(trigger_detail=f"webchat:{main}", user_role="member") is None

    def test_someone_elses_conversation_or_a_workspace(self) -> None:
        other = "webchat:agent:main:user:22222222-2222-4222-8222-222222222222"
        assert self._reader(trigger_detail=other) is None
        room = "webchat:agent:main:room:33333333-3333-4333-8333-333333333333"
        assert self._reader(trigger_detail=room) is None

    def test_a_telegram_run_reads_shared_rows_only(self) -> None:
        """A Telegram run's person may come from a chat-id lookup
        (link_run_person) when the live identity carried none, and then the
        live call had no private reader. The record does not say which, so a
        direct message is replayed with no reader, as a group is."""
        dm = self._reader(trigger_type="telegram", trigger_detail="chat:12345|sender:x", user_id="")
        assert dm is None
        group = self._reader(trigger_type="telegram", trigger_detail="chat:-100|sender:x")
        assert group is None

    @pytest.mark.parametrize(
        "trigger",
        ["cron", "hook", "event", "manual", "workflow", "sub_agent", "slack", "ide", "channel"],
    )
    def test_every_other_run_has_no_private_reader(self, trigger: str) -> None:
        assert self._reader(trigger_type=trigger) is None

    def test_a_spawned_run_inherits_no_reader(self) -> None:
        assert self._reader(parent_run_id="44444444-4444-4444-8444-444444444444") is None

    def test_the_engines_rule_decides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One rule: the engine's ``private_reader_for_run`` decides, with the
        run record as its run and a verified identity of the run's person."""
        from robothor.engine import private_recall

        seen: dict[str, Any] = {}

        def _rule(trigger_type: Any, trigger_detail: Any, user_id: Any, identity: Any) -> Any:
            seen.update(
                trigger_type=trigger_type,
                trigger_detail=trigger_detail,
                user_id=user_id,
                identity=identity,
            )
            return private_recall.PrivateReader(person_id="p-from-engine", user_id=None)

        monkeypatch.setattr(private_recall, "private_reader_for_run", _rule)
        assert self._reader(trigger_type="cron") == ("p-from-engine", None)
        assert seen["trigger_type"] == "cron"
        assert seen["user_id"] == self.UID
        assert seen["identity"].verified is True
        assert seen["identity"].person_id == self.PERSON
        assert seen["identity"].role == "member"

    def test_no_person_or_no_user_means_no_reader(self) -> None:
        assert self._reader(person_id=None, user_id=None) is None
        assert (
            self._reader(trigger_type="telegram", trigger_detail="chat:1", person_id=None) is None
        )
