"""Measure the real runner with an isolated database and deterministic I/O.

Usage: python -m bench.interactive.run_runner --output /tmp/runner-benchmark.json
Configure ROBOTHOR_TEST_DB_DSN for the integration database. Production is refused.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from bench.interactive.compare import compare


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=30)
    args = parser.parse_args()
    if args.samples < 30:
        parser.error("At least 30 samples per execution path are required")
    with tempfile.TemporaryDirectory(prefix="interactive-benchmark-") as directory:
        samples = Path(directory) / "samples.jsonl"
        env = dict(
            os.environ,
            ROBOTHOR_INTERACTIVE_BENCH_SAMPLES=str(args.samples),
            ROBOTHOR_INTERACTIVE_BENCH_OUTPUT=str(samples),
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "robothor/engine/tests/test_interactive_runner_benchmark.py",
                "-q",
                "--no-cov",
            ],
            env=env,
            check=False,
        )
        rows = (
            [json.loads(line) for line in samples.read_text().splitlines()]
            if samples.exists()
            else []
        )
    report = {
        "scope": "Actual runner and PostgreSQL operations; model/Google are deterministic fixtures. Token counts are synthetic, not provider billing.",
        "measurements": rows,
        "exit_code": completed.returncode,
        "complete": completed.returncode == 0,
        "comparison": compare(rows) if rows else None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["comparison"], indent=2))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
