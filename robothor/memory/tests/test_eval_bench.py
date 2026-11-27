"""Public benchmarks through the memory eval: tenant hygiene, ingestion, the run.

Unit tests use fakes for ingestion, the systems and the models. The
integration tests (real PostgreSQL, fake embedder, no model) check that what
is ingested carries the benchmark's clock and that cleanup leaves nothing.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from robothor.constants import DEFAULT_TENANT
from robothor.memory import eval_bench as eb
from robothor.memory import eval_bench_data as bd
from robothor.memory import eval_systems as es
from robothor.memory.eval import EVAL_TENANT, EvalPreconditionError

DIM = 1024


async def fake_embed_batch(texts: list[str]) -> list[list[float]]:
    out = []
    for t in texts:
        d = hashlib.sha256(t.encode()).digest()
        out.append([(d[i % 32] - 128) / 128.0 for i in range(DIM)])
    return out


def _turn(role: str, text: str, at: datetime) -> bd.BenchTurn:
    return bd.BenchTurn(role, text, at)


def _history(hid: str = "h1") -> bd.BenchHistory:
    t0 = datetime(2023, 5, 20, 2, 21, tzinfo=UTC)
    t1 = datetime(2023, 6, 1, 9, 0, tzinfo=UTC)
    s0 = bd.BenchSession(
        "s0",
        t0,
        (
            _turn("user", "I took up the cello last week.", t0),
            _turn("assistant", "Congratulations, Alice!", t0 + timedelta(seconds=30)),
        ),
    )
    s1 = bd.BenchSession(
        "s1",
        t1,
        (
            _turn("user", "Bob and I moved to Brelford.", t1),
            _turn("assistant", "Brelford is lovely.", t1 + timedelta(seconds=30)),
        ),
    )
    return bd.BenchHistory(hid, (s0, s1))


def _bench(n: int = 2) -> bd.Benchmark:
    histories = {f"h{i}": _history(f"h{i}") for i in range(n)}
    questions = tuple(
        bd.BenchQuestion(
            id=f"q{i}",
            history_id=f"h{i}",
            question_type="single-session-user",
            question="Which instrument did I take up?",
            answer="the cello",
            as_of="2023/06/02 (Fri) 10:00",
        )
        for i in range(n)
    )
    return bd.Benchmark("longmemeval", "s", histories, questions, source="fixture")


# --------------------------------------------------------------------------- #
# Tenant hygiene
# --------------------------------------------------------------------------- #


def _forbid_db(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("the guard must fire before any database call")

    monkeypatch.setattr("robothor.db.connection.get_connection", _boom)


class TestTenantGuard:
    def test_the_benchmark_tenant_is_an_eval_tenant_of_its_own(self):
        from robothor.memory.eval import is_eval_tenant
        from robothor.memory.eval_world_seed import WORLD_TENANT

        assert is_eval_tenant(eb.BENCH_TENANT)
        assert eb.BENCH_TENANT not in (EVAL_TENANT, WORLD_TENANT)

    @pytest.mark.parametrize("tenant", [DEFAULT_TENANT, "", "robothor-primary", EVAL_TENANT])
    def test_cleanup_refuses_a_non_eval_tenant_before_touching_the_db(self, monkeypatch, tenant):
        _forbid_db(monkeypatch)
        with pytest.raises(EvalPreconditionError):
            eb.cleanup_bench(tenant)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tenant", [DEFAULT_TENANT, "", EVAL_TENANT])
    async def test_ingest_refuses_a_non_eval_tenant_before_touching_the_db(
        self, monkeypatch, tenant
    ):
        _forbid_db(monkeypatch)
        with pytest.raises(EvalPreconditionError):
            await eb.ingest_history(_history(), tenant, embed_batch=fake_embed_batch)

    @pytest.mark.asyncio
    async def test_run_refuses_a_non_eval_tenant_before_ingesting(self, monkeypatch, tmp_path):
        _forbid_db(monkeypatch)
        called: list[str] = []

        async def ingest(*_a, **_k):
            called.append("ingest")

        with pytest.raises(EvalPreconditionError):
            await eb.run_benchmark(
                _bench(),
                systems=["recall"],
                tenant_id=DEFAULT_TENANT,
                ingest_fn=ingest,
                cleanup_fn=lambda _t: {},
                chat=_FakeChat(),
                validate_judge=False,
            )
        assert called == []

    def test_cleanup_deletes_the_world_tables_children_first(self, monkeypatch):
        """The same delete order as the eval world (which handles identity
        rows), plus what the benchmark's write path adds."""
        from robothor.memory import eval_world_seed as seed

        world_calls: list[list[str]] = []
        monkeypatch.setattr(
            seed, "cleanup_world", lambda t: world_calls.append(list(t)) or {"memory_facts": 2}
        )
        extra_sql: list[str] = []

        class _Cur:
            rowcount = 1

            def execute(self, sql, params=None):
                extra_sql.append(sql)
                assert params is None or EVAL_TENANT + "-bench" in str(params)

            def fetchall(self):
                return [(t,) for t in eb.BENCH_EXTRA_TABLES]

        class _Conn:
            def cursor(self):
                return _Cur()

        @contextlib.contextmanager
        def _conn(*_a, **_k):
            yield _Conn()

        monkeypatch.setattr("robothor.db.connection.get_connection", _conn)
        deleted = eb.cleanup_bench(eb.BENCH_TENANT)
        # Extra tables reference facts, so they go before the world's delete.
        assert world_calls == [[eb.BENCH_TENANT]]
        assert deleted["memory_facts"] == 2
        assert any("DELETE" in s for s in extra_sql)


# --------------------------------------------------------------------------- #
# How a session becomes product writes
# --------------------------------------------------------------------------- #


class TestExchanges:
    def test_user_assistant_pairs_and_lone_turns(self):
        t = datetime(2023, 1, 1, tzinfo=UTC)
        turns = (
            _turn("assistant", "Hello, how can I help?", t),
            _turn("user", "Tea?", t),
            _turn("assistant", "Green.", t),
            _turn("user", "Thanks.", t),
            _turn("user", "Bye.", t),
        )
        pairs = eb.exchanges(turns)
        assert [(u and u.text, a and a.text) for u, a in pairs] == [
            (None, "Hello, how can I help?"),
            ("Tea?", "Green."),
            ("Thanks.", None),
            ("Bye.", None),
        ]

    def test_extraction_windows_follow_the_product_transcript_cap(self):
        from robothor.memory.conversation_ingest import MAX_TRANSCRIPT_MESSAGES

        t = datetime(2023, 1, 1, tzinfo=UTC)
        turns = tuple(_turn("user" if i % 2 == 0 else "assistant", f"m{i}", t) for i in range(45))
        windows = eb.transcript_windows(turns)
        assert [len(w) for w in windows] == [MAX_TRANSCRIPT_MESSAGES, MAX_TRANSCRIPT_MESSAGES, 5]
        assert [m["content"] for w in windows for m in w] == [f"m{i}" for i in range(45)]


class TestExtractMode:
    @pytest.mark.asyncio
    async def test_extraction_runs_per_role_with_the_session_date_pinned(self, monkeypatch):
        """Extract mode calls the product's conversation write (ingest_content,
        once per role, the other role read-only) with the session's date as
        the extraction's "today"."""
        from robothor.memory import facts

        seen: list[dict[str, Any]] = []

        async def fake_ingest_content(content, source_channel, content_type, **kwargs):
            seen.append(
                {
                    "role": kwargs.get("role"),
                    "content": content,
                    "context": kwargs.get("read_only_context"),
                    "tenant": kwargs.get("tenant_id"),
                    "today": facts.build_extraction_prompt("x").split("relative to ")[1][:10],
                    "fallback": kwargs.get("fallback_on_failure"),
                    "content_type": content_type,
                }
            )
            return {"fact_ids": [], "extraction_failed": False}

        monkeypatch.setattr(eb, "_ingest_content", fake_ingest_content)
        stats = await eb.extract_session(_history().sessions[0], eb.BENCH_TENANT, "bench:s0")
        assert [s["role"] for s in seen] == ["user", "assistant"]
        assert all(s["today"] == "2023-05-20" for s in seen)
        assert all(s["tenant"] == eb.BENCH_TENANT for s in seen)
        assert all(s["fallback"] is False and s["content_type"] == "conversation" for s in seen)
        assert "the cello" in seen[0]["content"]
        assert "Congratulations" in seen[0]["context"]
        assert stats["windows"] == 1 and stats["failed"] == 0


# --------------------------------------------------------------------------- #
# The run, with fakes
# --------------------------------------------------------------------------- #


class _FakeChat:
    """Answers with the gold; the judge says yes when the response holds the answer."""

    def __init__(self, answer: str = "You took up the cello.") -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def __call__(self, messages, **_k):
        prompt = messages[-1]["content"]
        self.calls.append(prompt)
        if prompt.startswith("I will give you a question, a correct answer"):
            gold = prompt.split("Correct Answer: ")[1].split("\n\n")[0]
            resp = prompt.split("Model Response: ")[1].split("\n\nIs the model")[0]
            return "yes" if gold.lower() in resp.lower() else "no"
        return self.answer


class _EchoSystem:
    name = "echo_bench"
    equal_budget = True

    async def retrieve(self, case, *, world_handle, token_budget):
        return es.ContextBundle(
            system=self.name,
            items=es.fill_budget([("t1", "turn", "User: I took up the cello.")], token_budget),
            budget=token_budget,
            equal_budget=True,
            abstained=False,
            retrieval_ms=1.0,
        )


@pytest.fixture
def echo_system():
    es.register_system("echo_bench", _EchoSystem)
    yield "echo_bench"
    es.unregister_system("echo_bench")


def _fake_ingest(log: list[str]):
    async def ingest(history, tenant, **_k):
        log.append(f"ingest {history.id} {tenant}")
        return eb.IngestResult(
            seeded=eb.SeededWorld(tenant_ids=(tenant,)),
            turn_dates={},
            sessions=len(history.sessions),
            turns=4,
            seconds=0.1,
        )

    return ingest


class TestRun:
    @pytest.mark.asyncio
    async def test_each_history_is_ingested_answered_judged_and_cleaned(self, echo_system):
        log: list[str] = []
        chat = _FakeChat()
        report = await eb.run_benchmark(
            _bench(2),
            systems=[echo_system],
            ingest_fn=_fake_ingest(log),
            cleanup_fn=lambda t: log.append(f"cleanup {t}") or {},
            chat=chat,
            validate_judge=False,
            answer_model="qwen3:32b",
            judge_model="qwen3:32b",
        )
        assert log == [
            f"cleanup {eb.BENCH_TENANT}",
            f"ingest h0 {eb.BENCH_TENANT}",
            f"cleanup {eb.BENCH_TENANT}",
            f"cleanup {eb.BENCH_TENANT}",
            f"ingest h1 {eb.BENCH_TENANT}",
            f"cleanup {eb.BENCH_TENANT}",
        ]
        m = report["systems"][echo_system]
        assert m["overall"]["n"] == 2 and m["overall"]["accuracy"] == 1.0
        p = report["params"]
        assert p["benchmark"] == "longmemeval" and p["variant"] == "s"
        assert p["answer_model"] == "qwen3:32b" and p["judge_model"] == "qwen3:32b"
        assert p["ingest_mode"] == "segments"
        assert p["questions"] == 2 and p["questions_by_type"] == {"single-session-user": 2}
        # The answer prompt is the benchmark's reader prompt.
        assert any(c.startswith("I will give you several history chats") for c in chat.calls)

    @pytest.mark.asyncio
    async def test_cleanup_runs_even_when_ingest_fails(self, echo_system):
        log: list[str] = []

        async def bad_ingest(history, tenant, **_k):
            raise RuntimeError("embedder down")

        report = await eb.run_benchmark(
            _bench(1),
            systems=[echo_system],
            ingest_fn=bad_ingest,
            cleanup_fn=lambda t: log.append("cleanup") or {},
            chat=_FakeChat(),
            validate_judge=False,
        )
        # Before the first attempt, between the retries, and after the last.
        assert log == ["cleanup"] * (eb.INGEST_ATTEMPTS + 1)
        # A history that never ingested is excluded and counted, not scored.
        assert report["systems"] == {}
        assert [e["question_id"] for e in report["excluded"]] == ["q0"]

    @pytest.mark.asyncio
    async def test_a_wrong_answer_is_scored_wrong(self, echo_system):
        report = await eb.run_benchmark(
            _bench(1),
            systems=[echo_system],
            ingest_fn=_fake_ingest([]),
            cleanup_fn=lambda _t: {},
            chat=_FakeChat(answer="A degree in physics."),
            validate_judge=False,
        )
        assert report["systems"][echo_system]["overall"]["accuracy"] == 0.0

    @pytest.mark.asyncio
    async def test_resume_skips_done_questions_without_ingesting_them(self, echo_system, tmp_path):
        progress = str(tmp_path / "p.jsonl")
        log: list[str] = []
        kwargs: dict[str, Any] = {
            "systems": [echo_system],
            "cleanup_fn": lambda _t: {},
            "chat": _FakeChat(),
            "validate_judge": False,
            "progress_path": progress,
        }
        await eb.run_benchmark(_bench(1), ingest_fn=_fake_ingest(log), **kwargs)
        assert log == ["ingest h0 " + eb.BENCH_TENANT]
        report = await eb.run_benchmark(_bench(2), ingest_fn=_fake_ingest(log), **kwargs)
        assert log[1:] == ["ingest h1 " + eb.BENCH_TENANT]
        assert report["params"]["resumed"] == 1
        assert report["systems"][echo_system]["overall"]["n"] == 2

    @pytest.mark.asyncio
    async def test_a_progress_file_from_other_parameters_is_refused(self, echo_system, tmp_path):
        progress = str(tmp_path / "p.jsonl")
        base: dict[str, Any] = {
            "systems": [echo_system],
            "ingest_fn": _fake_ingest([]),
            "cleanup_fn": lambda _t: {},
            "chat": _FakeChat(),
            "validate_judge": False,
            "progress_path": progress,
        }
        await eb.run_benchmark(_bench(1), **base)
        with pytest.raises(ValueError, match="different parameters"):
            await eb.run_benchmark(_bench(1), token_budget=999, **base)
        header = json.loads(Path(progress).read_text(encoding="utf-8").splitlines()[0])
        assert header["kind"] == "header" and header["benchmark"] == "longmemeval"

    def test_markdown_states_models_mode_and_counts(self):
        report = {
            "params": {
                "benchmark": "longmemeval",
                "variant": "s",
                "questions": 2,
                "questions_by_type": {"single-session-user": 2},
                "ingest_mode": "segments",
                "answer_model": "qwen3:32b",
                "judge_model": "qwen3:32b",
                "token_budget": 1000,
            },
            "judge_validation": None,
            "systems": bd.summarise_bench(
                [
                    bd.BenchRecord("recall", "q0", "single-session-user", False, 1.0, 10, 1.0, 2.0),
                    bd.BenchRecord("recall", "q1", "single-session-user", False, 0.0, 10, 1.0, 2.0),
                ],
                benchmark="longmemeval",
            ),
            "timings": {},
            "errors": [],
        }
        md = eb.format_bench_markdown(report)
        assert "qwen3:32b" in md and "segments" in md and "single-session-user" in md
        assert "50.0%" in md
        assert "Comparability" in md


class TestJudgeValidation:
    @pytest.mark.asyncio
    async def test_a_faithful_judge_passes_and_a_yes_man_fails(self):
        bench = _bench(1)
        qs = [
            *bench.questions,
            bd.BenchQuestion("x1", "h0", "multi-session", "How many pets?", "3"),
            bd.BenchQuestion("x2_abs", "h0", "multi-session", "Dog's name?", "Not mentioned."),
            bd.BenchQuestion("x3", "h0", "knowledge-update", "Where now?", "Brelford"),
        ]

        async def faithful(messages, **_k):
            p = messages[-1]["content"]
            if p.startswith("I will give you an unanswerable question"):
                resp = p.split("Model Response: ")[1]
                return "yes" if "not" in resp.lower() or "don't" in resp.lower() else "no"
            gold = p.split("Correct Answer: ")[1].split("\n\n")[0]
            resp = p.split("Model Response: ")[1].split("\n\nIs the model")[0]
            return "yes" if gold.lower() in resp.lower() else "no"

        async def yes_man(messages, **_k):
            return "yes"

        good = await eb.validate_longmemeval_judge(qs, chat=faithful, model="m")
        assert good["passed"] and good["right_marked_right"] == 1.0
        bad = await eb.validate_longmemeval_judge(qs, chat=yes_man, model="m")
        assert not bad["passed"] and bad["wrong_marked_wrong"] == 0.0


# --------------------------------------------------------------------------- #
# Integration: real PostgreSQL, fake embedder
# --------------------------------------------------------------------------- #


def _db_reachable() -> bool:
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            conn.cursor().execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 — any failure means "no database here"
        return False


@pytest.fixture
def bench_tenant():
    if not _db_reachable():
        pytest.skip("no PostgreSQL reachable for the integration test")
    tenant = f"{EVAL_TENANT}-bench-t{uuid.uuid4().hex[:8]}"
    yield tenant
    with contextlib.suppress(Exception):
        eb.cleanup_bench(tenant)


def _rows_by_tenant_table(tenant: str) -> dict[str, int]:
    """Rows for ``tenant`` in every table that has a tenant_id column."""
    from robothor.db.connection import get_connection

    out: dict[str, int] = {}
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT c.table_name FROM information_schema.columns c "
            "JOIN information_schema.tables t ON t.table_name = c.table_name "
            "AND t.table_schema = c.table_schema "
            "WHERE c.column_name = 'tenant_id' AND c.table_schema = 'public' "
            "AND t.table_type = 'BASE TABLE'"
        )
        for (table,) in cur.fetchall():
            cur.execute(f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (tenant,))  # noqa: S608
            n = int(cur.fetchone()[0])
            if n:
                out[table] = n
    return out


@pytest.mark.integration
class TestIngestAndCleanup:
    @pytest.mark.asyncio
    async def test_segments_mode_carries_the_benchmark_clock_and_cleanup_leaves_nothing(
        self, bench_tenant
    ):
        from robothor.db.connection import get_connection

        history = _history()
        result = await eb.ingest_history(
            history, bench_tenant, embed_batch=fake_embed_batch, embed_turns=True
        )
        assert result.sessions == 2 and result.turns == 4
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT m.created_at FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id "
                "WHERE s.tenant_id = %s ORDER BY m.created_at",
                (bench_tenant,),
            )
            stamps = [r[0].astimezone(UTC) for r in cur.fetchall()]
            cur.execute(
                "SELECT min(started_at) FROM memory_segments WHERE tenant_id = %s", (bench_tenant,)
            )
            first_segment = cur.fetchone()[0]
        expected = sorted(t.at for s in history.sessions for t in s.turns)
        assert stamps == expected
        assert first_segment is not None and first_segment.astimezone(UTC) == expected[0]
        assert _rows_by_tenant_table(bench_tenant)
        # Every turn has a date for the answer prompt.
        assert len(result.turn_dates) == 4

        eb.cleanup_bench(bench_tenant)
        assert _rows_by_tenant_table(bench_tenant) == {}

    @pytest.mark.asyncio
    async def test_extract_mode_writes_facts_at_the_session_time(self, bench_tenant, monkeypatch):
        """The real write path, with the extraction model and the embedder faked."""
        from robothor.db.connection import get_connection
        from robothor.llm import ollama
        from robothor.memory import facts

        async def fake_extract(content, max_retries=3, *, timeout=180.0, context=None):
            if "the cello" in content:
                return facts.Extraction(
                    [
                        {
                            "fact_text": "Alice took up the cello.",
                            "category": "personal",
                            "confidence": 0.9,
                            "entities": ["Alice"],
                            "mentions": [],
                            "relations": [],
                            "event_time": None,
                        }
                    ]
                )
            return facts.Extraction([])

        async def fake_embed_one(text, *_a, **_k):
            return (await fake_embed_batch([text]))[0]

        monkeypatch.setattr(facts, "extract_facts_detailed", fake_extract)
        monkeypatch.setattr(ollama, "get_embedding_async", fake_embed_one)
        monkeypatch.setattr(ollama, "get_embeddings_batch_async", fake_embed_batch)

        result = await eb.ingest_history(
            _history(), bench_tenant, mode="extract", embed_batch=fake_embed_batch
        )
        assert result.facts >= 1
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT created_at, valid_from FROM memory_facts WHERE tenant_id = %s "
                "AND fact_text LIKE %s",
                (bench_tenant, "%the cello%"),
            )
            created, valid_from = cur.fetchone()
        assert created.astimezone(UTC) == datetime(2023, 5, 20, 2, 21, tzinfo=UTC)
        assert valid_from is None or valid_from.astimezone(UTC) == datetime(
            2023, 5, 20, 2, 21, tzinfo=UTC
        )
        eb.cleanup_bench(bench_tenant)
        assert _rows_by_tenant_table(bench_tenant) == {}


@pytest.mark.integration
class TestCleanupAfterConflicts:
    @pytest.mark.asyncio
    async def test_conflict_decisions_are_cleaned_too(self, bench_tenant, monkeypatch):
        """A fact extracted again in a later session is reinforced, which
        records a conflict decision for the tenant; cleanup must remove it."""
        from robothor.llm import ollama
        from robothor.memory import facts

        async def fake_extract(content, max_retries=3, *, timeout=180.0, context=None):
            return facts.Extraction(
                [
                    {
                        "fact_text": "Alice Moreno lives in Brelford with Bob.",
                        "category": "personal",
                        "confidence": 0.9,
                        "entities": ["Alice Moreno", "Brelford"],
                        "mentions": [],
                        "relations": [],
                        "event_time": None,
                    }
                ]
            )

        async def fake_embed_one(text, *_a, **_k):
            return (await fake_embed_batch(["same"]))[0]

        async def same_embed_batch(texts):
            return await fake_embed_batch(["same" for _ in texts])

        monkeypatch.setattr(facts, "extract_facts_detailed", fake_extract)
        monkeypatch.setattr(ollama, "get_embedding_async", fake_embed_one)
        monkeypatch.setattr(ollama, "get_embeddings_batch_async", same_embed_batch)
        await eb.ingest_history(
            _history(), bench_tenant, mode="extract", embed_batch=same_embed_batch
        )
        assert "memory_conflict_decisions" in _rows_by_tenant_table(bench_tenant)
        eb.cleanup_bench(bench_tenant)
        assert _rows_by_tenant_table(bench_tenant) == {}
