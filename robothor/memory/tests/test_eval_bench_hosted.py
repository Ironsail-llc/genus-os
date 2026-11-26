"""Benchmarks with hosted answer and judge models: the client, cost, reuse, split.

No network: litellm is faked. The key is read by the provider client from the
environment; it must never reach a report, a progress file or a cache.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from robothor.memory import eval_bench as eb
from robothor.memory import eval_bench_data as bd
from robothor.memory import eval_hosted as eh
from robothor.memory import eval_systems as es

if TYPE_CHECKING:
    import argparse

from robothor.memory.tests.test_eval_bench import _bench, _EchoSystem, _fake_ingest, _FakeChat

SECRET = "sk-test-DO-NOT-LEAK-0123456789"


def _response(text: str, prompt_tokens: int = 100, completion_tokens: int = 5) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


@pytest.fixture
def fake_litellm(monkeypatch):
    calls: list[dict[str, Any]] = []
    replies: list[Any] = []

    async def acompletion(**kwargs):
        calls.append(kwargs)
        reply = replies.pop(0) if replies else _response("yes")
        if isinstance(reply, Exception):
            raise reply
        return reply

    import litellm

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    monkeypatch.setattr(eh, "_sleep", _no_sleep)
    return SimpleNamespace(calls=calls, replies=replies)


async def _no_sleep(_s: float) -> None:
    return None


class TestHostedClient:
    def test_which_models_are_hosted(self):
        assert eh.is_hosted("openai/gpt-4o-2024-08-06")
        assert eh.is_hosted("openai/gpt-5-mini")
        assert not eh.is_hosted("qwen3:32b")
        assert eh.chat_for("qwen3:32b") is not eh.hosted_chat
        assert eh.chat_for("openai/gpt-4o") is eh.hosted_chat

    @pytest.mark.asyncio
    async def test_the_judge_call_is_the_official_one(self, fake_litellm, monkeypatch):
        monkeypatch.setenv("OPENAI_API_BASE", "https://regional.example.com/v1")
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        res = await eh.hosted_chat(
            [{"role": "user", "content": "Is it correct?"}],
            model="openai/gpt-4o-2024-08-06",
            temperature=0.0,
            max_tokens=10,
            think=False,
        )
        (call,) = fake_litellm.calls
        assert call["model"] == "openai/gpt-4o-2024-08-06"
        assert call["temperature"] == 0.0 and call["max_tokens"] == 10
        # The endpoint the key is bound to, from the environment; the key itself
        # is left to the provider client and never handled here.
        assert call["api_base"] == "https://regional.example.com/v1"
        assert SECRET not in json.dumps(call, default=str)
        assert res.text == "yes" and res.input_tokens == 100 and res.output_tokens == 5
        # gpt-4o-2024-08-06: 100 * 2.50/1M + 5 * 10.00/1M
        assert res.cost_usd == pytest.approx(100 * 2.5e-6 + 5 * 10e-6)

    @pytest.mark.asyncio
    async def test_a_reasoning_model_gets_room_and_no_temperature(self, fake_litellm):
        await eh.hosted_chat(
            [{"role": "user", "content": "q"}],
            model="openai/gpt-5-mini",
            temperature=0.0,
            max_tokens=512,
        )
        (call,) = fake_litellm.calls
        assert "temperature" not in call
        assert call["max_tokens"] > 512

    @pytest.mark.asyncio
    async def test_rate_limits_back_off_and_retry(self, fake_litellm):
        import litellm

        err = litellm.RateLimitError("slow down", llm_provider="openai", model="gpt-4o")
        fake_litellm.replies.extend([err, err, _response("no")])
        res = await eh.hosted_chat(
            [{"role": "user", "content": "q"}],
            model="openai/gpt-4o",
            temperature=0.0,
            max_tokens=10,
        )
        assert res.text == "no" and len(fake_litellm.calls) == 3

    @pytest.mark.asyncio
    async def test_at_most_four_calls_in_flight(self, fake_litellm, monkeypatch):
        import asyncio

        import litellm

        live = peak = 0

        async def slow(**_k):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
            return _response("yes")

        monkeypatch.setattr(litellm, "acompletion", slow)
        await asyncio.gather(
            *(
                eh.hosted_chat(
                    [{"role": "user", "content": "q"}],
                    model="openai/gpt-4o",
                    temperature=0.0,
                    max_tokens=10,
                )
                for _ in range(12)
            )
        )
        assert peak <= eh.MAX_IN_FLIGHT == 4


# --------------------------------------------------------------------------- #
# Reuse: ingest once, ask many; carried-forward cells; nothing paid twice
# --------------------------------------------------------------------------- #


@pytest.fixture
def two_systems():
    class _Other(_EchoSystem):
        name = "echo_other"

    es.register_system("echo_bench", _EchoSystem)
    es.register_system("echo_other", _Other)
    yield ["echo_bench", "echo_other"]
    es.unregister_system("echo_bench")
    es.unregister_system("echo_other")


class TestReuse:
    @pytest.mark.asyncio
    async def test_a_second_answer_model_reuses_the_ingestion_and_contexts(
        self, two_systems, tmp_path
    ):
        log: list[str] = []
        common: dict[str, Any] = {
            "systems": two_systems,
            "ingest_fn": _fake_ingest(log),
            "cleanup_fn": lambda _t: {},
            "validate_judge": False,
            "cache_dir": str(tmp_path / "cache"),
        }
        await eb.run_benchmark(_bench(2), answer_model="local-a", chat=_FakeChat(), **common)
        assert len(log) == 2
        chat = _FakeChat()
        report = await eb.run_benchmark(_bench(2), answer_model="local-b", chat=chat, **common)
        assert len(log) == 2, "the second answer model must not ingest again"
        assert report["systems"]["echo_bench"]["overall"]["n"] == 2

    @pytest.mark.asyncio
    async def test_an_unchanged_cell_is_carried_forward_not_paid_again(
        self, two_systems, tmp_path, monkeypatch
    ):
        log: list[str] = []
        common: dict[str, Any] = {
            "systems": two_systems,
            "ingest_fn": _fake_ingest(log),
            "cleanup_fn": lambda _t: {},
            "validate_judge": False,
            "cache_dir": str(tmp_path / "cache"),
            "answer_model": "m",
        }
        monkeypatch.setattr(eb, "code_version", lambda: "v1")
        await eb.run_benchmark(_bench(1), chat=_FakeChat(), **common)
        monkeypatch.setattr(eb, "code_version", lambda: "v2")
        chat = _FakeChat()
        report = await eb.run_benchmark(
            _bench(1), chat=chat, carry_forward=["echo_bench"], **common
        )
        # echo_other's code changed: re-ingested, re-answered, re-judged.
        # echo_bench was carried: its answer and judgement came from the cache.
        assert len(log) == 2
        assert len(chat.calls) == 2
        assert report["params"]["carried_forward"] == {"echo_bench": 1}
        assert "Carried forward" in eb.format_bench_markdown(report)

    @pytest.mark.asyncio
    async def test_the_key_reaches_no_file_or_report(self, two_systems, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        progress = tmp_path / "p.jsonl"
        report = await eb.run_benchmark(
            _bench(1),
            systems=two_systems,
            ingest_fn=_fake_ingest([]),
            cleanup_fn=lambda _t: {},
            validate_judge=False,
            cache_dir=str(tmp_path / "cache"),
            progress_path=str(progress),
            chat=_FakeChat(),
            answer_model="openai/gpt-5-mini",
            judge_model="openai/gpt-4o-2024-08-06",
        )
        blobs = [json.dumps(report, default=str), eb.format_bench_markdown(report)]
        blobs += [p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.jsonl")]
        assert all(SECRET not in b for b in blobs)


# --------------------------------------------------------------------------- #
# Dry run: what it would cost, with nothing ingested or called
# --------------------------------------------------------------------------- #


class TestEstimate:
    def test_estimate_per_cell_calls_nothing(self, two_systems):
        bench = _bench(3)
        est = eb.estimate_run(
            bench,
            bench.questions,
            systems=two_systems,
            answer_models=["openai/gpt-5-mini", "openai/gpt-4o"],
            judge_model="openai/gpt-4o-2024-08-06",
            token_budget=1000,
        )
        cells = {(c["system"], c["answer_model"]): c for c in est["cells"]}
        assert len(cells) == 4
        c = cells[("echo_bench", "openai/gpt-4o")]
        assert c["questions"] == 3
        assert c["answer_input_tokens"] >= 3 * 1000
        assert c["judge_input_tokens"] > 0 and c["judge_output_tokens"] > 0
        assert c["cost_usd"] > 0
        assert est["total_cost_usd"] == pytest.approx(sum(x["cost_usd"] for x in est["cells"]))
        assert est["prices"]["gpt-4o-2024-08-06"] == [2.5, 10.0]
        assert est["assumptions"]
        # A reasoning model is charged for its reasoning tokens too.
        mini = cells[("echo_bench", "openai/gpt-5-mini")]
        assert mini["answer_output_tokens"] > c["answer_output_tokens"]

    @pytest.mark.asyncio
    async def test_cached_answers_cost_nothing_and_cached_contexts_set_the_tokens(
        self, two_systems, tmp_path
    ):
        cache = str(tmp_path / "cache")
        await eb.run_benchmark(
            _bench(2),
            systems=two_systems,
            ingest_fn=_fake_ingest([]),
            cleanup_fn=lambda _t: {},
            validate_judge=False,
            cache_dir=cache,
            chat=_FakeChat(),
            answer_model="openai/gpt-4o",
            judge_model="openai/gpt-4o-2024-08-06",
        )
        bench = _bench(2)
        est = eb.estimate_run(
            bench,
            bench.questions,
            systems=two_systems,
            answer_models=["openai/gpt-4o", "openai/gpt-5-mini"],
            judge_model="openai/gpt-4o-2024-08-06",
            token_budget=1000,
            cache_dir=cache,
        )
        cells = {(c["system"], c["answer_model"]): c for c in est["cells"]}
        done = cells[("echo_bench", "openai/gpt-4o")]
        assert done["cost_usd"] == 0 and done["cached_answers"] == 2
        fresh = cells[("echo_bench", "openai/gpt-5-mini")]
        assert fresh["cached_contexts"] == 2
        # The cached context is tiny, so the estimate uses it, not the budget.
        assert fresh["answer_input_tokens"] < 2 * 1000

    def test_unknown_price_is_refused(self, two_systems):
        bench = _bench(1)
        with pytest.raises(ValueError, match="no price"):
            eb.estimate_run(
                bench,
                bench.questions,
                systems=two_systems,
                answer_models=["openai/gpt-9"],
                judge_model="openai/gpt-4o-2024-08-06",
                token_budget=1000,
            )


# --------------------------------------------------------------------------- #
# The development / held-out split
# --------------------------------------------------------------------------- #


class TestSplit:
    def _questions(self, n_per_type: int) -> list[bd.BenchQuestion]:
        return [
            bd.BenchQuestion(f"{t}-{i}", "h", t, "q", "a")
            for t in ("multi-session", "temporal-reasoning")
            for i in range(n_per_type)
        ]

    def test_half_of_each_type_is_dev_and_the_split_is_seeded(self):
        qs = self._questions(10)
        split = eb.dev_split(qs, seed=885)
        for t in ("multi-session", "temporal-reasoning"):
            assert sum(1 for q in qs if q.question_type == t and split[q.id] == "dev") == 5
        assert split == eb.dev_split(qs, seed=885)
        assert split != eb.dev_split(qs, seed=886)

    @pytest.mark.asyncio
    async def test_every_report_has_both_halves(self, two_systems):
        report = await eb.run_benchmark(
            _bench(4),
            systems=two_systems,
            ingest_fn=_fake_ingest([]),
            cleanup_fn=lambda _t: {},
            validate_judge=False,
            chat=_FakeChat(),
        )
        assert set(report["by_split"]) == {"dev", "held_out"}
        assert report["params"]["split_seed"] == eb.DEFAULT_SPLIT_SEED
        assert sum(report["params"]["split_counts"].values()) == 4


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


class TestCli:
    def _args(self, path, **over: Any) -> argparse.Namespace:
        from robothor.cli import _build_parser

        argv = ["memory-eval", "--benchmark", "longmemeval", "--benchmark-path", str(path)]
        for k, v in over.items():
            flag = "--" + k.replace("_", "-")
            argv += [flag] if v is True else [flag, str(v)]
        return _build_parser().parse_args(argv)

    def test_dry_run_prints_the_estimate_and_touches_nothing(self, tmp_path, capsys, monkeypatch):
        from robothor.cli.memory_eval import cmd_memory_eval
        from robothor.memory.tests.test_eval_bench_data import _lme_entry

        data = [_lme_entry(f"q{i}", "multi-session", "Q?", "A") for i in range(4)]
        path = tmp_path / "longmemeval_s_cleaned.json"
        path.write_text(json.dumps(data), encoding="utf-8")

        def _boom(*_a, **_k):
            raise AssertionError("a dry run must not touch the database")

        monkeypatch.setattr("robothor.db.connection.get_connection", _boom)
        args = self._args(
            path,
            dry_run=True,
            limit=2,
            systems="recall_today,recall",
            answer_models="openai/gpt-5-mini,openai/gpt-4o",
            judge_model="openai/gpt-4o-2024-08-06",
        )
        assert cmd_memory_eval(args) == 0
        out = capsys.readouterr().out
        assert "2 questions" in out and "gpt-5-mini" in out and "$" in out

    def test_a_missing_file_cannot_run(self, tmp_path, capsys):
        from robothor.cli.memory_eval import cmd_memory_eval

        assert cmd_memory_eval(self._args(tmp_path / "nope.json", dry_run=True)) == 3
