"""Nightly benchmark rotation — the measurement as a standing practice.

    python -m bench.wildclaw.rotation --repo <WildClawBench checkout> \
        --data <workspace root> --out <ledger dir>

Every number this project believed about its competitive standing came from
ad-hoc campaign runs, and campaigns end. This driver runs ONE category per
night — same model, same graders, same containers as the published OpenClaw
baseline — and appends one JSON line per run to a ledger. With six runnable
categories, six nights is a full sweep: a regression surfaces within a week
of being introduced instead of at the next campaign.

Design constraints, learned the hard way this week:

* **A skipped category must be visible.** Categories whose workspace data is
  not staged are excluded from the rotation and named on stdout — a rotation
  that silently shrinks to the easy categories is grading a different
  platform than it claims.
* **A low score is a result; only a failed RUN is a failure.** The unit exits
  non-zero (and pages via OnFailure=) when the harness could not produce a
  summary — never because the numbers were bad. Bad numbers go in the ledger,
  which is the point of having one.
* **The baseline rides with the repo.** `baselines.json` holds OpenClaw's
  published per-task scores (their harness, GLM 5.2, their graders), so every
  ledger line carries its own delta and needs no other context to read.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


def resolve_paths(
    repo: Path | None, data: Path | None, out: Path | None
) -> tuple[Path, Path, Path]:
    """Flags when given, WILDCLAW_* environment otherwise.

    The systemd unit passes no arguments at all: the render gate refuses any
    ``${...}`` in a directive (systemd expands them only in ExecStart=, and a
    typo'd variable there becomes an empty word silently), so the unit sets
    the three WILDCLAW_* variables via EnvironmentFile and this resolves
    them. Missing both is a configuration error and says which name is
    missing rather than crashing later on a None path.
    """
    resolved = []
    for value, env_name in (
        (repo, "WILDCLAW_REPO"),
        (data, "WILDCLAW_DATA"),
        (out, "WILDCLAW_OUT"),
    ):
        if value is None:
            raw = os.environ.get(env_name, "")
            if not raw:
                print(f"{env_name} is not set and no flag was given", file=sys.stderr)
                raise SystemExit(2)
            value = Path(raw)
        resolved.append(value)
    return resolved[0], resolved[1], resolved[2]


def runnable_categories(repo: Path, data_root: Path) -> list[str]:
    """Categories that have both task specs and staged workspace data.

    Sorted, so the rotation order is stable across machines and restarts.
    """
    cats = []
    tasks_dir = repo / "tasks"
    if not tasks_dir.is_dir():
        return []
    for d in sorted(tasks_dir.iterdir()):
        if not d.is_dir() or not any(d.glob("*.md")):
            continue
        if (data_root / "workspace" / d.name).is_dir():
            cats.append(d.name)
    return cats


def pick_category(day_ordinal: int, categories: list[str]) -> str:
    """Deterministic rotation: same day, same roster -> same category."""
    if not categories:
        raise ValueError("no runnable categories — is the workspace data staged?")
    return categories[day_ordinal % len(categories)]


def load_baselines() -> dict[str, Any]:
    path = _HERE / "baselines.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


class EmptyRunError(RuntimeError):
    """The harness produced a summary, but no task ever reached a model.

    A summary is not evidence that anything ran. When the provider refuses
    every call — a capped key, a revoked credential — each task is graded
    against an untouched workspace and the harness writes a well-formed
    summary whose mean is 0.0. Appending that would put a fabricated zero in
    the ledger, and the ledger's whole job is to average runs and report the
    spread, so one such line corrupts every later verdict.

    This is the module's own rule applied to its own inputs: a low score is a
    result, only a failed RUN is a failure, and a run where no model answered
    is a failed run.
    """


def _tasks_executed(results: list[dict[str, Any]]) -> int:
    """How many tasks actually reached a model.

    Tokens and request count are recorded per task and were never read. A
    task that consumed neither did not run, whatever its score says.
    """
    executed = 0
    for r in results:
        usage = r.get("usage")
        if not usage:
            # No usage block at all is absence of evidence, not evidence of
            # absence — older summaries predate it. Only claim a task did not
            # run when the harness actually recorded that it consumed nothing.
            executed += 1
            continue
        if (usage.get("input_tokens") or 0) > 0 or (usage.get("request_count") or 0) > 0:
            executed += 1
    return executed


def ledger_entry(summary: dict[str, Any], baselines: dict[str, Any], when: str) -> dict[str, Any]:
    """One ledger line: the run, its baseline, and the delta between them.

    Raises ``EmptyRunError`` when nothing executed — see that class.
    """
    category = summary.get("category", "")
    base = baselines.get(category) or {}
    baseline_mean = base.get("mean")
    mean = float(summary.get("mean_score", 0.0))
    results = summary.get("results") or []
    executed = _tasks_executed(results)
    if executed == 0:
        raise EmptyRunError(
            f"{category or 'run'}: no model answered — {executed} of {len(results)} tasks "
            "consumed any tokens. Refusing to record a fabricated score; check the "
            "provider credential."
        )
    return {
        "when": when,
        "category": category,
        "mean": mean,
        "baseline_mean": baseline_mean,
        "delta": round(mean - baseline_mean, 4) if baseline_mean is not None else None,
        "tasks_attempted": summary.get("tasks_attempted", 0),
        "tasks_graded": summary.get("tasks_graded", 0),
        "tasks_without_workspace": summary.get("tasks_without_workspace", 0),
        "tasks_executed": executed,
        "harness_kills": sum(1 for r in results if r.get("harness_kill")),
        "per_task": {r["task_id"]: r["score"] for r in results if "task_id" in r},
    }


_DB_ENV = {
    "POSTGRES_USER": "robothor",
    "POSTGRES_PASSWORD": "robothor",
    "POSTGRES_DB": "robothor_test",
}


def ensure_pod() -> None:
    """Build the bench pod if it is not running.

    Rootless podman containers do not survive a reboot, so a nightly unit
    that assumes the pod exists pages the operator every morning after one.
    Mirrors the recipe in README.md; a pod that already exists is left
    untouched (its database carries prior runs' rows, which is fine — every
    run is keyed by its own run_id).
    """
    from bench.wildclaw.harness import POD

    probe = subprocess.run(["podman", "pod", "exists", POD], capture_output=True)
    if probe.returncode == 0:
        return
    print(f"bench pod {POD!r} missing — building it")
    subprocess.run(["podman", "pod", "create", "--name", POD], check=True)
    pg_env = [f"-e{k}={v}" for k, v in _DB_ENV.items()]
    subprocess.run(
        [
            "podman",
            "run",
            "-d",
            "--pod",
            POD,
            "--name",
            "gb-pg",
            *pg_env,
            "docker.io/pgvector/pgvector:pg16",
        ],
        check=True,
    )
    subprocess.run(
        [
            "podman",
            "run",
            "-d",
            "--pod",
            POD,
            "--name",
            "gb-redis",
            "docker.io/library/redis:7-alpine",
        ],
        check=True,
    )
    migrate_env = [
        "-eROBOTHOR_DB_HOST=127.0.0.1",
        "-eROBOTHOR_DB_NAME=robothor_test",
        "-eROBOTHOR_DB_USER=robothor",
        "-eROBOTHOR_DB_PASSWORD=robothor",
    ]
    # Postgres needs a moment; the migrate is retried rather than slept at.
    for _attempt in range(12):
        done = subprocess.run(
            [
                "podman",
                "run",
                "--rm",
                "--pod",
                POD,
                *migrate_env,
                "localhost/genus-bench:latest",
                "python",
                "-m",
                "robothor.cli",
                "migrate",
            ],
            capture_output=True,
        )
        if done.returncode == 0:
            return
        time.sleep(5)
    raise RuntimeError("bench pod database never came up — see podman logs gb-pg")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path, default=None)
    ap.add_argument("--data", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None, help="ledger + run output root")
    ap.add_argument("--category", default="", help="override the rotation's pick")
    ap.add_argument("--model", default="openrouter/z-ai/glm-5.2")
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="run only the first N tasks — smoke-testing the plumbing, not a measurement",
    )
    args = ap.parse_args()
    repo_path, data_path, out_path = resolve_paths(args.repo, args.data, args.out)
    args.repo, args.data, args.out = repo_path, data_path, out_path

    cats = runnable_categories(args.repo, args.data)
    all_cats = sorted(
        d.name for d in (args.repo / "tasks").iterdir() if d.is_dir() and any(d.glob("*.md"))
    )
    skipped = [c for c in all_cats if c not in cats]
    if skipped:
        print(f"not in rotation (no staged data): {', '.join(skipped)}")

    ensure_pod()

    now = _dt.datetime.now(_dt.UTC)
    category = args.category or pick_category(now.toordinal(), cats)
    stamp = now.strftime("%Y%m%d")
    out_dir = args.out / "runs" / f"{stamp}-{category}"
    print(f"rotation: {category} -> {out_dir}")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "bench.wildclaw.harness",
            "--repo",
            str(args.repo),
            "--data",
            str(args.data),
            "--category",
            category,
            "--model",
            args.model,
            "--out",
            str(out_dir),
            *(["--limit", str(args.limit)] if args.limit else []),
        ],
        cwd=str(_HERE.parents[1]),
    )

    summary_path = out_dir / "summary.json"
    if not summary_path.exists():
        print(
            f"harness exited {proc.returncode} with no summary — that is a run "
            "failure, not a result",
            file=sys.stderr,
        )
        return 1

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    try:
        entry = ledger_entry(summary, load_baselines(), when=now.isoformat(timespec="seconds"))
    except EmptyRunError as exc:
        # A summary full of zeros because nothing ran is a run failure, and
        # the unit's OnFailure= turns that into a page. Recording it instead
        # would put a fabricated score in the ledger for good.
        print(f"{exc}", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    ledger = args.out / "ledger.jsonl"
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    delta = entry["delta"]
    verdict = (
        f"delta vs OpenClaw baseline: {delta:+.1%}"
        if delta is not None
        else "no baseline for this category"
    )
    print(
        f"{category}: mean {entry['mean']:.1%} over {entry['tasks_graded']}"
        f"/{entry['tasks_attempted']} — {verdict}"
        + (f"  [{entry['harness_kills']} harness kill(s)]" if entry["harness_kills"] else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
