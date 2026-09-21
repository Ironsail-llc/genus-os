"""Compare unchanged synthetic task scripts through both complete native runners."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from bench.interactive.statistics import summary


def drill(root, env):
    baseline = Path(os.environ["ROBOTHOR_RUNTIME_BASELINE_CHECKOUT"]).resolve()
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=baseline, text=True
    ).strip()
    assert revision == "eea3252b1577e3f51e6d6a6216d6f242fd7ac4a1"
    code = Path("bench/runtime/native_task_comparison_worker.py").read_text()
    reports = {}
    for label, checkout in [("baseline", baseline), ("current", Path.cwd())]:
        worker_env = {k: v for k, v in env.items() if k.startswith("ROBOTHOR_DB_")}
        worker_env.update(
            PATH=os.environ["PATH"],
            PYTHONPATH=str(checkout),
            PYTHONNOUSERSITE="1",
            ROBOTHOR_TEST_DB_DSN=env["ROBOTHOR_TEST_DB_DSN"],
        )
        try:
            process = subprocess.run(
                [sys.executable, "-c", code],
                cwd=checkout,
                env=worker_env,
                capture_output=True,
                text=True,
                timeout=240,
            )
        except subprocess.TimeoutExpired as exc:

            def decoded(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else value or ""

            process = SimpleNamespace(
                stdout=decoded(exc.stdout),
                stderr=decoded(exc.stderr) + "\nWorker exceeded 240-second cutoff",
                returncode=124,
            )
        print(
            "NATIVE_TASK_OUTPUT "
            + json.dumps(
                {
                    "revision": label,
                    "stdout": process.stdout,
                    "stderr": process.stderr,
                    "exit_code": process.returncode,
                }
            ),
            flush=True,
        )
        rows = [
            json.loads(s.split(" ", 1)[1])
            for s in process.stdout.splitlines()
            if s.startswith("NATIVE_TASK_SAMPLE ")
        ]
        reports[label] = {
            "exit_code": process.returncode,
            "samples": rows,
            "not_finished": max(0, 62 - len(rows)),
            "scenarios": {
                name: summary(
                    [r["duration_ms"] for r in rows if r["scenario"] == name and not r["warmup"]]
                )
                for name in ["standalone", "compound"]
            },
        }
    report = {
        "baseline_revision": revision,
        "current_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "measurements": reports,
        "passed": all(
            r["exit_code"] == 0
            and len(r["samples"]) == 62
            and all(s["verified"] for s in r["samples"])
            for r in reports.values()
        ),
        "scope": "Thirty warm requests plus retained warmup per scenario and revision. Full native runner with real private CRM, identical scripted two-turn provider and minimal task-protocol profile. Legacy finalReport-off behavior on both revisions; does not measure new host final-report speed. Network denied. Sequential revision blocks, not live-provider, HTTP ingress, full production prompt/memory, or framework qualification. Tool schemas and product prompts intentionally come from each revision.",
    }
    print("NATIVE_TASK_COMPARISON " + json.dumps(report), flush=True)
    assert report["passed"], "See retained per-revision failures"
    return report
