"""``python -m bench.turn``: drive turns, print p50/p95, compare with a baseline.

Exit codes: 0 within threshold (or no baseline yet), 1 regression, 2 the
benchmark could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bench.turn.compare import compare
from bench.turn.stats import summarize

BASELINES = Path(__file__).resolve().parent / "baselines"
HALVES = ("http", "inprocess")


def _default_db_url() -> str:
    explicit = os.environ.get("GENUS_BENCH_DB_URL", "")
    if explicit:
        return explicit
    name = os.environ.get("ROBOTHOR_DB_NAME", "")
    if not name:
        return ""
    parts = {
        "dbname": name,
        "user": os.environ.get("ROBOTHOR_DB_USER", ""),
        "password": os.environ.get("ROBOTHOR_DB_PASSWORD", ""),
        "host": os.environ.get("ROBOTHOR_DB_HOST", ""),
        "port": os.environ.get("ROBOTHOR_DB_PORT", ""),
    }
    return " ".join(f"{k}={v}" for k, v in parts.items() if v)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m bench.turn",
        description="Per-turn latency benchmark: webchat over HTTP and Telegram-shaped "
        "runner.execute turns in-process, p50/p95 per metric, compared with a baseline.",
    )
    p.add_argument("--half", choices=(*HALVES, "both"), default="both", help="which turns to drive")
    p.add_argument("--runs", type=int, default=20, help="turns per half (default 20)")
    p.add_argument("--engine-url", default="http://127.0.0.1:18800", help="engine base URL")
    p.add_argument(
        "--db-url",
        default=None,
        help="Postgres DSN or URL for pg_stat_statements and run steps "
        "(default GENUS_BENCH_DB_URL, else built from ROBOTHOR_DB_*)",
    )
    p.add_argument(
        "--token",
        default=os.environ.get("GENUS_BENCH_TOKEN", ""),
        help="engine bearer token for the http half (default GENUS_BENCH_TOKEN)",
    )
    p.add_argument(
        "--mint-token",
        action="store_true",
        help="mint an owner token in-process (needs the engine's signing key, e.g. inside the engine container)",
    )
    p.add_argument(
        "--agent", default="", help="agent id (default: the engine's default chat agent)"
    )
    p.add_argument("--message", default="say pong", help="the user message each turn sends")
    p.add_argument("--model", default="", help="model override for the in-process half")
    p.add_argument(
        "--settle-ms", type=int, default=500, help="wait after done before reading DB counts"
    )
    p.add_argument(
        "--restart-cmd",
        default="",
        help="shell command that restarts the engine; times it to /ready as engine_ready_ms",
    )
    p.add_argument(
        "--out", default="", help="write the JSON result here (per half: -http/-inprocess)"
    )
    p.add_argument(
        "--baseline",
        default="",
        help="baseline JSON (default bench/turn/baselines/<half>.json)",
    )
    p.add_argument(
        "--threshold", type=float, default=0.10, help="regression threshold (default 0.10)"
    )
    p.add_argument("--min-delta", type=float, default=1.0, help="ignore changes smaller than this")
    p.add_argument("--update-baseline", action="store_true", help="write this run as the baseline")
    p.add_argument(
        "--compare", default="", metavar="RESULT", help="compare a saved result; drive nothing"
    )
    return p


def _per_half(path: str, half: str, both: bool) -> Path:
    target = Path(path)
    return target.with_name(f"{target.stem}-{half}{target.suffix}") if both else target


def _baseline_path(args: argparse.Namespace, half: str) -> Path:
    if args.baseline:
        return _per_half(args.baseline, half, args.half == "both" and not args.compare)
    return BASELINES / f"{half}.json"


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    return data if isinstance(data, dict) else None


def _print_summary(result: dict[str, Any]) -> None:
    print(f"\n{result.get('half', '?')} ({result.get('runs', '?')} turns)")
    print(f"  {'metric':<22} {'p50':>10} {'p95':>10} {'n':>4}")
    for metric, row in (result.get("summary") or {}).items():
        print(f"  {metric:<22} {row['p50']:>10.1f} {row['p95']:>10.1f} {row['n']:>4}")
    for metric, reason in (result.get("notes") or {}).items():
        print(f"  {metric:<22} off: {reason}")


def _judge(args: argparse.Namespace, half: str, result: dict[str, Any]) -> int:
    path = _baseline_path(args, half)
    if args.update_baseline:
        baseline = {
            k: result[k] for k in ("half", "runs", "recorded_at", "host", "summary") if k in result
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
        print(f"baseline written: {path}")
        return 0
    outcome = compare(_load(path), result, threshold=args.threshold, min_delta=args.min_delta)
    print(f"\nagainst {path}: {outcome.message}")
    return outcome.exit_code


def _token(args: argparse.Namespace) -> str:
    if args.token or not args.mint_token:
        return str(args.token)
    from robothor.auth.tokens import issue_access_token
    from robothor.engine.config import EngineConfig

    return issue_access_token(
        "bench-operator",
        EngineConfig.from_env().tenant_id,
        "owner",
        audience="genus-engine",
        scopes=("engine:*",),
        ttl_seconds=3600,
    )


def _drive(args: argparse.Namespace, half: str, db_url: str) -> dict[str, Any]:
    if half == "http":
        from bench.turn.http_half import run_http

        raw = run_http(
            engine_url=args.engine_url.rstrip("/"),
            token=_token(args),
            runs=args.runs,
            agent=args.agent or "main",
            message=args.message,
            db_url=db_url,
            settle_ms=args.settle_ms,
            restart_cmd=args.restart_cmd,
        )
    else:
        from bench.turn.inprocess_half import run_inprocess

        raw = run_inprocess(
            runs=args.runs,
            agent=args.agent,
            message=args.message,
            model=args.model,
            db_url=db_url,
            settle_ms=args.settle_ms,
        )
    summary = summarize(raw["turns"])
    ready = raw.get("engine_ready_ms")
    if ready is not None:
        summary["engine_ready_ms"] = {"p50": ready, "p95": ready, "n": 1}
    return {
        "half": half,
        "runs": args.runs,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "host": {"python": platform.python_version(), "machine": platform.machine()},
        "summary": summary,
        "notes": raw.get("notes") or {},
        "turns": raw["turns"],
        "meta": raw.get("meta") or [],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.compare:
        result = _load(Path(args.compare))
        if result is None:
            print(f"cannot read {args.compare}", file=sys.stderr)
            return 2
        half = str(result.get("half") or "inprocess")
        return _judge(args, half, result)

    db_url = args.db_url if args.db_url is not None else _default_db_url()
    halves = HALVES if args.half == "both" else (args.half,)
    worst = 0
    for half in halves:
        try:
            result = _drive(args, half, db_url)
        except Exception as exc:  # noqa: BLE001 - report and exit 2
            print(f"{half} half failed: {exc}", file=sys.stderr)
            return 2
        _print_summary(result)
        failed = [m for m in result.get("meta") or [] if m.get("status") != "completed"]
        if args.out:
            out = _per_half(args.out, half, args.half == "both")
            out.write_text(json.dumps(result, indent=2, default=str) + "\n")
            print(f"result written: {out}")
        if failed:
            # Timings of runs that failed measure the failure, not a turn.
            first = failed[0]
            print(
                f"{half} half: {len(failed)} of {len(result.get('meta') or [])} turns did not "
                f"complete (first: turn {first.get('turn')}, status {first.get('status')}, "
                f"error {first.get('error')}); no comparison, no baseline",
                file=sys.stderr,
            )
            return 2
        worst = max(worst, _judge(args, half, result))
    return worst
