"""CLI: ``robothor memory-eval`` — run the memory retrieval benchmark.

Seeds fixture facts into an isolated tenant, runs the real retrieval path,
scores recall/temporal/verbatim/persona, prints a report, and cleans up.
Requires a live PostgreSQL + local Ollama (embeddings + reranker).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from robothor.memory.eval import (
    EVAL_TENANT,
    EvalPreconditionError,
    exit_code_for,
    format_report,
    preflight,
    record_benchmark_row,
    report_to_benchmark_row,
    require_eval_tenant,
    run_suite,
)
from robothor.memory.eval_answer import format_answers_markdown, run_answers
from robothor.memory.eval_world_seed import WORLD_TENANT, format_world_report, run_world

if TYPE_CHECKING:
    import argparse

_DEFAULT_SUITE = "docs/benchmarks/memory/suite.yaml"

#: Where --record-baseline writes the world answer-arm baseline.
BASELINE_DIR = Path("docs/benchmarks/memory")


def cmd_memory_eval(args: argparse.Namespace) -> int:
    """Run the memory benchmark.

    Exit codes are three-way on purpose: 0 passed, 2 the suite ran and cases
    failed, 3 the suite could not run. Collapsing 3 into 2 is what let this
    eval sit broken behind row-level security while still looking like an
    ordinary failing test.
    """
    if getattr(args, "benchmark", None):
        return _cmd_benchmark(args)
    if getattr(args, "answers", False):
        if not getattr(args, "world", False):
            print("memory-eval cannot run: --answers needs --world")
            return 3
        return _cmd_answers(args)
    if getattr(args, "world", False):
        return _cmd_world(args)

    suite_path = Path(args.suite)
    if not suite_path.exists():
        print(f"Suite not found: {suite_path}")
        return 3

    tenant = args.tenant or EVAL_TENANT
    # Before preflight, which already opens a connection: the suite deletes
    # every fact in its tenant, so a real tenant must never get that far.
    try:
        require_eval_tenant(tenant)
    except EvalPreconditionError as e:
        print(f"memory-eval cannot run: {e}")
        return 3

    # NB: robothor.cli's main() already calls load_instance_env(), which reads
    # /etc/robothor/robothor.env AND the engine's systemd drop-in and fills in
    # anything the caller did not set. So this process already carries the
    # production flag posture; do not re-implement that here.

    blocked = preflight(tenant)
    if blocked:
        print(f"memory-eval cannot run: {blocked}")
        return exit_code_for(None, blocked)

    try:
        report = asyncio.run(run_suite(suite_path, tenant_id=tenant, cleanup=not args.keep))
    except EvalPreconditionError as e:
        print(f"memory-eval cannot run: {e}")
        return exit_code_for(None, str(e))

    print(format_report(report, as_json=args.json_output))

    if getattr(args, "record", False):
        row = report_to_benchmark_row(
            report,
            suite_path=str(suite_path),
            triggered_by=getattr(args, "triggered_by", "manual"),
        )
        if record_benchmark_row(row):
            print(
                f"recorded: benchmark_results agent_id=memory "
                f"pass_rate={row['pass_rate']} ({row['passed']}/{row['total_cases']})"
            )
        else:
            # Deliberately not fatal: a reporting failure must not turn a
            # passing eval into a failing process. Persistent absence is caught
            # by the fleet's staleness check, not by this exit code.
            print("warning: could not record the result to benchmark_results")

    return exit_code_for(report, None)


def _cmd_world(args: argparse.Namespace) -> int:
    """Seed the synthetic world, score its cases through search_facts, clean up.

    Exit 0 when it ran, 3 when it could not (or scored nothing): there is no
    world floor yet. Runs in its own tenant (``memory-eval-world`` by default),
    never the nightly suite's ``memory-eval``. Not recorded to benchmark_results:
    the fleet reads the latest ``memory`` row without a suite filter, so a
    world score would silently replace the suite's. Baselines for the world
    are their own later step.
    """
    tenant = args.tenant or WORLD_TENANT
    if tenant == EVAL_TENANT:
        print(
            f"memory-eval cannot run: --world may not use {EVAL_TENANT!r}, the nightly suite's "
            f"tenant; use {WORLD_TENANT!r} (the default)"
        )
        return 3
    blocked = preflight(tenant)
    if blocked:
        print(f"memory-eval cannot run: {blocked}")
        return exit_code_for(None, blocked)

    try:
        report = asyncio.run(
            run_world(
                args.world_seed,
                args.world_size,
                args.split,
                tenant_id=tenant,
                cleanup=not args.keep,
            )
        )
    except EvalPreconditionError as e:
        print(f"memory-eval cannot run: {e}")
        return exit_code_for(None, str(e))

    print(format_world_report(report, as_json=args.json_output))
    if getattr(args, "record", False):
        print("note: --record is ignored with --world (world results are not recorded yet)")
    # No floor exists for the world yet, so the suite's would make every run
    # exit 2 (raw_detail alone cannot pass through search_facts). 0 = it ran,
    # 3 = nothing was scored.
    return 0 if int(report.get("total") or 0) > 0 else 3


def _cmd_answers(args: argparse.Namespace) -> int:
    """Answer-and-judge comparison of several systems on the world.

    Writes the JSON report to ``--report`` (with a Markdown summary beside it)
    or to stdout. ``--record-baseline`` also writes both under
    ``docs/benchmarks/memory/``. Never touches benchmark_results: the suite
    owns that row.
    """
    import json

    from robothor.memory.eval_systems import make_systems

    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    try:
        make_systems(systems)
    except ValueError as e:
        print(f"memory-eval cannot run: {e}")
        return 3

    tenant = args.tenant or WORLD_TENANT
    if tenant == EVAL_TENANT:
        print(
            f"memory-eval cannot run: --world may not use {EVAL_TENANT!r}, the nightly suite's "
            f"tenant; use {WORLD_TENANT!r} (the default)"
        )
        return 3
    blocked = preflight(tenant)
    if blocked:
        print(f"memory-eval cannot run: {blocked}")
        return exit_code_for(None, blocked)

    try:
        report = asyncio.run(
            run_answers(
                args.world_seed,
                args.world_size,
                args.split,
                systems=systems,
                reference=args.reference,
                token_budget=args.token_budget,
                repeats=args.repeats,
                answer_model=args.answer_model,
                judge_model=args.judge_model,
                limit_per_stratum=args.limit_per_stratum,
                tenant_id=tenant,
                concurrency=args.concurrency,
                cache_dir=args.cache_dir,
                cleanup=not args.keep,
                progress_path=args.progress,
            )
        )
    except (EvalPreconditionError, ValueError) as e:
        print(f"memory-eval cannot run: {e}")
        return 3

    payload = json.dumps(report, indent=2, default=str)
    markdown = format_answers_markdown(report)
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
        path.with_suffix(".md").write_text(markdown, encoding="utf-8")
        print(markdown)
        print(f"report: {path} (summary: {path.with_suffix('.md')})")
    else:
        print(payload)
        print("\n<!-- markdown -->\n")
        print(markdown)
    if args.record_baseline:
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        summary = {k: v for k, v in report.items() if k != "records"}
        (BASELINE_DIR / "world-baseline.json").write_text(
            json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
        )
        (BASELINE_DIR / "world-baseline.md").write_text(markdown, encoding="utf-8")
        print(f"recorded baseline under {BASELINE_DIR}")
    return 0


#: The world's systems default; a benchmark run without --systems uses its own.
_WORLD_SYSTEMS = "recall,recall_budget,keyword_only,raw_verbatim,full_context"
_BENCH_SYSTEMS = "recall_today,recall"


def _cmd_benchmark(args: argparse.Namespace) -> int:
    """A public long-memory benchmark: dry run, or ingest, answer and score.

    One run per answer model (``--answer-models``), all sharing ``--bench-dir``:
    the first ingests and retrieves, the rest reuse its contexts. Exit 0 when
    it ran, 3 when it could not.
    """
    import json

    from robothor.memory import eval_bench as eb
    from robothor.memory import eval_bench_data as bd
    from robothor.memory.eval_systems import make_systems
    from robothor.memory.recall import RECALL_CONTEXT_CHARS

    path = Path(args.benchmark_path or "")
    if not args.benchmark_path or not path.is_file():
        print(
            f"memory-eval cannot run: benchmark file not found: {path} (see docs/memory-system.md)"
        )
        return 3
    if getattr(args, "rescore", None):
        return _cmd_rescore(args, path)
    systems_arg = args.systems if args.systems != _WORLD_SYSTEMS else _BENCH_SYSTEMS
    systems = [s.strip() for s in systems_arg.split(",") if s.strip()]
    try:
        make_systems(systems)
    except ValueError as e:
        print(f"memory-eval cannot run: {e}")
        return 3
    bench = bd.load_benchmark(args.benchmark, path)
    try:
        # Before preflight and before any cell: every path refuses the same way.
        eb.require_parity_supported(bench, systems)
    except ValueError as e:
        print(f"memory-eval cannot run: {e}")
        return 3
    drop = (
        () if (args.include_adversarial or args.benchmark != "locomo") else bd.LOCOMO_DEFAULT_DROP
    )
    questions = bd.select_questions(
        bench.questions,
        limit=args.limit,
        limit_per_type=args.limit_per_type,
        drop_types=drop,
        abstention_stratum=args.abstention_stratum,
    )
    split_seed = args.split_seed if args.split_seed is not None else eb.DEFAULT_SPLIT_SEED
    if args.half != "all":
        split = eb.dev_split(bench.questions, seed=split_seed)
        questions = [q for q in questions if split.get(q.id) == args.half]
    budgets = [
        int(b)
        for b in str(args.context_tokens or RECALL_CONTEXT_CHARS // 4).split(",")
        if b.strip()
    ]
    models = [
        m.strip() for m in (args.answer_models or args.answer_model or "").split(",") if m.strip()
    ]
    if not models:
        from robothor.llm import ollama

        models = [ollama.GENERATION_MODEL]
    carry = [s.strip() for s in args.carry_forward.split(",") if s.strip()]
    tenant = args.tenant or eb.BENCH_TENANT

    if args.cells:
        cells = []
        for part in args.cells.split(","):
            b, _, m = part.strip().partition(":")
            cells.append((int(b), m.strip()))
    else:
        cells = [(b, m) for b in budgets for m in models]
    judge = args.judge_model or cells[0][1]
    if args.dry_run:
        print(
            f"{len(questions)} questions selected: "
            + ", ".join(f"{t} {n}" for t, n in sorted(_count(questions).items()))
        )
        total = 0.0
        for budget in sorted({b for b, _m in cells}):
            try:
                est = eb.estimate_run(
                    bench,
                    questions,
                    systems=systems,
                    answer_models=[m for b, m in cells if b == budget],
                    judge_model=judge,
                    token_budget=budget,
                    ingest_mode=args.ingest,
                    cache_dir=args.bench_dir,
                    carry_forward=carry,
                )
            except ValueError as e:
                print(f"memory-eval cannot run: {e}")
                return 3
            total += est["total_cost_usd"]
            print(eb.format_estimate(est))
        print(f"Iteration total: ${total:.2f} (judge validation not included).")
        return 0

    # A bound built from the benchmark (evidence_only, full_history) ingests
    # nothing, so a run of bounds alone needs no database.
    ingests = eb.needs_database(systems)
    if ingests:
        blocked = preflight(tenant)
        if blocked:
            print(f"memory-eval cannot run: {blocked}")
            return exit_code_for(None, blocked)
    if ingests and len(cells) > 1 and args.bench_dir:
        # One ingestion per history for every budget; the cells then ingest nothing.
        stats = asyncio.run(
            eb.prefill_contexts(
                bench,
                questions,
                systems=systems,
                token_budgets=sorted({b for b, _m in cells}),
                cache_dir=args.bench_dir,
                ingest_mode=args.ingest,
                tenant_id=tenant,
                carry_forward=carry,
                cleanup=not args.keep,
                extract_concurrency=args.extract_concurrency,
            )
        )
        print(f"contexts prefilled: {stats}")
    for budget, model in cells:
        suffix = "" if len(cells) == 1 else f".{budget}.{_slug(model)}"
        progress = f"{args.progress}{suffix}" if args.progress else None
        try:
            report = asyncio.run(
                eb.run_benchmark(
                    bench,
                    systems=systems,
                    questions=questions,
                    token_budget=budget,
                    answer_model=model,
                    judge_model=judge,
                    ingest_mode=args.ingest,
                    tenant_id=tenant,
                    progress_path=progress,
                    cache_dir=args.bench_dir,
                    carry_forward=carry,
                    concurrency=args.concurrency,
                    cleanup=not args.keep,
                    split_seed=split_seed,
                    extract_concurrency=args.extract_concurrency,
                )
            )
        except (EvalPreconditionError, ValueError) as e:
            print(f"memory-eval cannot run: {e}")
            return 3
        markdown = eb.format_bench_markdown(report)
        if args.report:
            out = Path(f"{args.report}{suffix}.json" if suffix else args.report)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
            out.with_suffix(".md").write_text(markdown, encoding="utf-8")
            print(f"report: {out}")
        print(markdown)
    return 0


def _cmd_rescore(args: argparse.Namespace, path: Path) -> int:
    """Re-score saved reports from their cached contexts: ``<report>.rescored.json`` / ``.md``."""
    import json

    from robothor.memory import eval_bench as eb
    from robothor.memory import eval_bench_data as bd

    bench = bd.load_benchmark(args.benchmark, path)
    for name in (n.strip() for n in args.rescore.split(",") if n.strip()):
        saved = Path(name)
        if not saved.is_file():
            print(f"memory-eval cannot run: report not found: {saved}")
            return 3
        report = json.loads(saved.read_text(encoding="utf-8"))
        out = eb.rescore_report(report, bench, cache_dir=args.bench_dir)
        target = saved.with_name(saved.stem + ".rescored.json")
        target.write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
        target.with_suffix(".md").write_text(eb.format_bench_markdown(out), encoding="utf-8")
        print(f"rescored: {target} ({out['rescore']})")
    return 0


def _count(questions: list) -> dict[str, int]:  # type: ignore[type-arg]
    out: dict[str, int] = {}
    for q in questions:
        out[q.question_type] = out.get(q.question_type, 0) + 1
    return out


def _slug(model: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9._-]+", "_", model)
