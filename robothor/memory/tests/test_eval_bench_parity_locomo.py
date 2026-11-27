"""The product-parity cell is refused where it cannot be product parity.

LoCoMo questions carry no date, so the product's retention cannot be run as
of the question, and LoCoMo's reader prompt is its own layout, not the tool
message. A LoCoMo run with ``search_memory_tool`` stops before anything is
ingested or called rather than labelling a harness rendering as parity.
"""

from __future__ import annotations

import json

import pytest

from robothor.memory import eval_bench as eb
from robothor.memory import eval_bench_data as bd


def _locomo(tmp_path) -> bd.Benchmark:
    sample = {
        "sample_id": "c1",
        "conversation": {
            "speaker_a": "Alice",
            "speaker_b": "Bob",
            "session_1_date_time": "1:56 pm on 8 May, 2023",
            "session_1": [
                {"speaker": "Alice", "dia_id": "D1:1", "text": "I took up the cello."},
                {"speaker": "Bob", "dia_id": "D1:2", "text": "Lovely."},
            ],
        },
        "qa": [{"question": "What did Alice take up?", "answer": "the cello", "category": 4}],
    }
    path = tmp_path / "locomo10.json"
    path.write_text(json.dumps([sample]), encoding="utf-8")
    return bd.load_benchmark("locomo", path)


@pytest.mark.asyncio
async def test_a_locomo_run_refuses_the_parity_cell(tmp_path):
    async def _no_ingest(*_a, **_k):
        raise AssertionError("refused before any ingest")

    with pytest.raises(ValueError, match="LoCoMo"):
        await eb.run_benchmark(
            _locomo(tmp_path),
            systems=["search_memory_tool"],
            ingest_fn=_no_ingest,
            cleanup_fn=lambda _t: {},
            validate_judge=False,
        )


@pytest.mark.asyncio
async def test_a_locomo_prefill_refuses_the_parity_cell(tmp_path):
    async def _no_ingest(*_a, **_k):
        raise AssertionError("refused before any ingest")

    with pytest.raises(ValueError, match="LoCoMo"):
        await eb.prefill_contexts(
            _locomo(tmp_path),
            _locomo(tmp_path).questions,
            systems=["search_memory_tool"],
            token_budgets=[1000],
            cache_dir=str(tmp_path / "c"),
            ingest_fn=_no_ingest,
            cleanup_fn=lambda _t: {},
        )


def test_the_cli_refuses_it_with_exit_3(tmp_path, capsys):
    from robothor.cli import _build_parser
    from robothor.cli.memory_eval import cmd_memory_eval

    _locomo(tmp_path)
    args = _build_parser().parse_args(
        [
            "memory-eval",
            "--benchmark",
            "locomo",
            "--benchmark-path",
            str(tmp_path / "locomo10.json"),
            "--systems",
            "search_memory_tool",
            "--dry-run",
        ]
    )
    assert cmd_memory_eval(args) == 3
    assert "LoCoMo" in capsys.readouterr().out
