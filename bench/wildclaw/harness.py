"""Run WildClawBench tasks against Genus and grade them with WildClawBench.

    python -m bench.wildclaw.harness --repo <WildClawBench checkout> \
        --data <downloaded workspace root> --category 06_Safety_Alignment

Three properties this deliberately holds:

**The harness under test never grades itself.** Scoring is the `grade()`
function embedded in each task's markdown, executed unmodified, with the
transcript passed in through the benchmark's own documented kwargs contract
(`grade(transcript=..., workspace_path=...)`). We supply a transcript and a
workspace; the benchmark decides what they are worth.

**Every task runs in a throwaway container.** These tasks are *designed* to
tempt an agent into destructive OS operations, credential harvesting, and
exfiltration — that is what they measure. Genus's `sandbox: docker` routes
`exec` into a container but leaves `read_file`/`write_file` on the host, so
running this on a workstation would put real secrets in reach of an agent
being deliberately provoked. The whole engine goes in the container instead.

**The workspace is rebuilt from the pristine copy every time.** A task that
inherits the previous run's leftovers is grading yesterday's agent.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

POD = "genus-bench"
#: The production image plus the toolchain the tasks assume — see
#: bench/wildclaw/Dockerfile for why that layer is separate.
IMAGE = os.environ.get("BENCH_IMAGE", "localhost/genus-bench-tools:latest")
CONTAINER_WORKSPACE = "/tmp_workspace"


def _api_key() -> str:
    """The OpenRouter key, unwrapped.

    systemd EnvironmentFile values are commonly written quoted, and the quotes
    are part of the value once a shell exports them. Passing them through
    produces a 401 whose message ("Missing Authentication header") points at
    the wrong problem entirely.
    """
    return os.environ.get("OPENROUTER_API_KEY", "").strip().strip('"').strip("'")


def _load_task(task_file: Path, repo: Path) -> dict[str, Any]:
    """Parse a task with the BENCHMARK's parser, never a copy of it.

    A second parser would drift from the one the authors run, and every
    difference would silently become a difference in what we claim to have
    measured.
    """
    sys.path.insert(0, str(repo))
    from src.utils.task_parser import parse_task_md  # type: ignore[import-not-found]

    return parse_task_md(task_file)


def _prepare_workspace(task: dict[str, Any], data_root: Path, repo: Path, dest: Path) -> None:
    """Copy the pristine task workspace into a fresh directory.

    The benchmark's parser resolves `workspace_path` against the REPO, but the
    task data is a separate HuggingFace download. Re-root it, then insist the
    result exists: an empty workspace still runs, still grades, and scores
    zero — an infrastructure mistake wearing the costume of a capability
    result, which is the failure this whole harness exists to avoid.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    relative = Path(task["workspace_path"])
    if relative.is_absolute():
        with contextlib.suppress(ValueError):
            relative = relative.relative_to(repo)
    source = data_root / relative / "exec"
    if not source.is_dir():
        raise FileNotFoundError(
            f"task workspace not found: {source} — download it with\n"
            f"  hf download internlm/WildClawBench --repo-type dataset "
            f"--include '{relative}/**' --local-dir {data_root}"
        )
    shutil.copytree(source, dest, dirs_exist_ok=True)

    results = dest / "results"
    results.mkdir(exist_ok=True)
    results.chmod(0o777)


def _run_agent(
    task: dict[str, Any], workspace: Path, out_dir: Path, model: str | None
) -> dict[str, Any]:
    """Run one task inside a throwaway container joined to the bench pod."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # See the mount comment below: the container writes as an unrelated
    # subuid, so both directories have to admit it.
    out_dir.chmod(0o777)
    workspace.chmod(0o777)
    env = [
        "-e",
        "ROBOTHOR_DB_HOST=127.0.0.1",
        "-e",
        "ROBOTHOR_DB_PORT=5432",
        "-e",
        "ROBOTHOR_DB_NAME=robothor_test",
        "-e",
        "ROBOTHOR_DB_USER=robothor",
        "-e",
        "ROBOTHOR_DB_PASSWORD=robothor",
        "-e",
        "ROBOTHOR_REDIS_URL=redis://127.0.0.1:6379/0",
        "-e",
        "ROBOTHOR_MANIFEST_DIR=/app/bench/wildclaw",
        "-e",
        f"BENCH_TASK_TIMEOUT={task.get('timeout_seconds', 600)}",
        "-e",
        f"OPENROUTER_API_KEY={_api_key()}",
    ]
    if model:
        env += ["-e", f"ROBOTHOR_BENCH_MODEL={model}"]

    repo_root = Path(__file__).resolve().parents[2]
    cmd = [
        "podman",
        "run",
        "--rm",
        "--pod",
        POD,
        # Mounted plain, not :U. The production image runs non-root, so under
        # rootless podman it writes as a subuid; the directories are opened up
        # below instead. :U would chown them away from the invoking user, and
        # --userns=keep-id is refused alongside --pod (which the database
        # needs), so this is the one combination that lets both sides write.
        "-v",
        f"{workspace}:{CONTAINER_WORKSPACE}:z",
        "-v",
        f"{out_dir}:/out:z",
        "-v",
        f"{repo_root}/bench:/app/bench:ro,z",
        *env,
        "-i",
        IMAGE,
        "python",
        "-m",
        "bench.wildclaw.run_one",
    ]
    started = time.perf_counter()
    proc = subprocess.run(
        cmd,
        input=task["prompt"],
        capture_output=True,
        text=True,
        timeout=int(task.get("timeout_seconds", 600)) + 300,
    )
    elapsed = time.perf_counter() - started
    (out_dir / "agent.log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
    return {"returncode": proc.returncode, "elapsed": elapsed}


def _grade(task: dict[str, Any], workspace: Path, out_dir: Path) -> dict[str, Any]:
    """Execute the task's own grade() against our transcript and workspace.

    Run in a subprocess so a grader that calls sys.exit, imports oddly, or
    leaks state cannot take the sweep down with it.
    """
    transcript_path = out_dir / "transcript.jsonl"
    entries: list[Any] = []
    if transcript_path.exists():
        for line in transcript_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    runner_src = "\n".join(
        [
            "import json, sys",
            f"_transcript = json.loads({json.dumps(json.dumps(entries))})",
            "",
            task["automated_checks"],
            "",
            # The CONTAINER path, not the host one: the grader runs inside,
            # where the workspace is mounted at /tmp_workspace. Passing the
            # host path made every file-fixture check read an absent
            # directory and score 0 — a broken probe that looks exactly like
            # a failing agent.
            f"result = grade(transcript=_transcript, "
            f"workspace_path={json.dumps(CONTAINER_WORKSPACE)})",
            "print('__SCORE__' + json.dumps(result))",
        ]
    )
    script = out_dir / "_grade.py"
    script.write_text(runner_src, encoding="utf-8")

    # Graded inside a container, as root, with cwd=/tmp_workspace — the
    # environment the graders are written for. They probe container-only paths
    # such as /root/.openclaw/... before falling back to the transcript passed
    # in, and on a host that probe raises PermissionError instead of returning
    # False. Running them anywhere else grades the harness, not the agent.
    cmd = [
        "podman",
        "run",
        "--rm",
        "--user",
        "0",
        "-v",
        f"{workspace}:{CONTAINER_WORKSPACE}:z",
        "-v",
        f"{out_dir}:/out:z",
        "-w",
        CONTAINER_WORKSPACE,
        "-e",
        f"OPENROUTER_API_KEY={_api_key()}",
        "-e",
        "OPENROUTER_BASE_URL="
        + os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "-e",
        "JUDGE_MODEL=" + os.environ.get("JUDGE_MODEL", "openai/gpt-5.4"),
        IMAGE,
        "python",
        "/out/_grade.py",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {"overall_score": 0.0, "grading_error": "grader timed out"}

    (out_dir / "grade.log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
    for line in proc.stdout.splitlines():
        if line.startswith("__SCORE__"):
            try:
                return json.loads(line[len("__SCORE__") :])
            except json.JSONDecodeError:
                break
    return {
        "overall_score": 0.0,
        "grading_error": (proc.stderr or "grader produced no score").strip()[:400],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, type=Path, help="WildClawBench checkout")
    ap.add_argument("--data", required=True, type=Path, help="downloaded workspace root")
    ap.add_argument("--category", default="06_Safety_Alignment")
    ap.add_argument("--task", default="", help="run a single task id substring")
    ap.add_argument("--model", default="", help="override the manifest model")
    ap.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "wildclaw-out")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    task_dir = args.repo / "tasks" / args.category
    task_files = sorted(task_dir.glob("*.md"))
    if args.task:
        task_files = [f for f in task_files if args.task in f.stem]
    if args.limit:
        task_files = task_files[: args.limit]
    if not task_files:
        print(f"no tasks matched under {task_dir}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for task_file in task_files:
        task = _load_task(task_file, args.repo)
        task_id = task["task_id"]
        print(f"\n=== {task_id} ===", flush=True)

        out_dir = args.out / task_id
        workspace = args.out / "_ws" / task_id
        _prepare_workspace(task, args.data, args.repo, workspace)

        run_info = _run_agent(task, workspace, out_dir, args.model or None)
        usage = {}
        usage_path = out_dir / "usage.json"
        if usage_path.exists():
            usage = json.loads(usage_path.read_text())

        score = _grade(task, workspace, out_dir)
        overall = float(score.get("overall_score", 0.0) or 0.0)
        (out_dir / "score.json").write_text(json.dumps(score, indent=2), encoding="utf-8")

        print(
            f"  score={overall:.2f}  "
            f"tokens={usage.get('total_tokens', 0)}  "
            f"cost=${usage.get('cost_usd', 0)}  "
            f"{run_info['elapsed']:.0f}s"
            + (f"  ERROR: {score['grading_error'][:80]}" if score.get("grading_error") else ""),
            flush=True,
        )
        results.append({"task_id": task_id, "score": overall, "usage": usage, "detail": score})

    graded = [r for r in results if not r["detail"].get("grading_error")]
    summary = {
        "category": args.category,
        "tasks_attempted": len(results),
        "tasks_graded": len(graded),
        "mean_score": round(sum(r["score"] for r in graded) / len(graded), 4) if graded else 0.0,
        "total_cost_usd": round(sum(r["usage"].get("cost_usd", 0) or 0 for r in results), 4),
        "results": results,
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        f"\n{summary['tasks_graded']}/{summary['tasks_attempted']} graded — "
        f"mean {summary['mean_score'] * 100:.1f}%  (${summary['total_cost_usd']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
