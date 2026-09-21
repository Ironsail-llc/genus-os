"""Compare dispatch/storage overhead on a canonical private database, no model."""

import json
import os
import random
import statistics
import subprocess
import sys
from pathlib import Path


def drill(root, env):
    baseline = Path(os.environ["ROBOTHOR_RUNTIME_BASELINE_CHECKOUT"]).resolve()
    expected = "eea3252b1577e3f51e6d6a6216d6f242fd7ac4a1"
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=baseline, text=True
    ).strip()
    assert revision == expected
    code = (Path.cwd() / "bench/runtime/crm_dispatch_worker.py").read_text()
    report = {}
    for label, checkout in [("baseline", baseline), ("current", Path.cwd())]:
        worker_env = {key: value for key, value in env.items() if key.startswith("ROBOTHOR_DB_")}
        worker_env.update(
            PATH=os.environ["PATH"],
            ROBOTHOR_TEST_DB_DSN=env["ROBOTHOR_TEST_DB_DSN"],
            PYTHONPATH=str(checkout),
            PYTHONNOUSERSITE="1",
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=checkout,
            env=worker_env,
            text=True,
            capture_output=True,
            timeout=90,
        )
        (root / (label + "-dispatch.log")).write_text(result.stdout + result.stderr)
        # Print samples even on failure so the caller's archived log retains them.
        print(
            label.upper()
            + "_DISPATCH_OUTPUT "
            + json.dumps({"stdout": result.stdout, "stderr": result.stderr}),
            flush=True,
        )
        assert result.returncode == 0, f"{label} dispatch screening failed; see retained output"
        samples = [
            json.loads(line.removeprefix("DISPATCH_SAMPLE "))
            for line in result.stdout.splitlines()
            if line.startswith("DISPATCH_SAMPLE ")
        ]
        assert len(samples) == 31 and all(sample["verified"] for sample in samples)
        values = [sample["duration_ms"] for sample in samples if not sample["warmup"]]

        def percentile(data):
            return statistics.quantiles(data, n=100, method="inclusive")[94]

        rng = random.Random(142)
        boots = sorted(percentile(rng.choices(values, k=len(values))) for _ in range(2000))
        report[label] = {
            "samples": samples,
            "p50_ms": statistics.median(values),
            "p95_ms": percentile(values),
            "p95_ci95_ms": [boots[49], boots[1949]],
        }
    return {
        "baseline_revision": revision,
        "current_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "measurements": report,
        "scope": "30 warm native create_task dispatch samples per revision plus one retained warmup; sequential baseline/current blocks in one private canonical database. Model, audit publishing and event delivery excluded; permission fixture identical. Includes handler and database overhead, with runtime receipt readback in current. Not complete agent harness overhead, live-provider latency, framework qualification or a causal speed comparison.",
    }
