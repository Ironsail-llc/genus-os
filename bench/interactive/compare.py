"""Compare JSON measurements only within identical model/tool/machine cohorts.

Input: a JSON array of records. Each record must name harness, version, case,
model, reasoning, startup (cold/warm), machine, prompt_hash, tools_hash,
cohort (local/cloud), duration_ms,
harness_ms, model_calls, input_tokens, post_completion_tool_calls, and
state_checks (a nonempty mapping of independent fixture assertions to bool).
Never substitute transcript claims for fixture state checks.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

METRICS = ("duration_ms", "harness_ms", "model_calls", "input_tokens", "post_completion_tool_calls")
COHORT = ("cohort", "model", "reasoning", "startup", "machine", "prompt_hash", "tools_hash")
HARNESSES = {"current", "optimized", "minimal", "opencode", "pi"}


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)]


def compare(records):
    groups = defaultdict(list)
    cohorts = set()
    for row in records:
        if row["harness"] not in HARNESSES or not row.get("version"):
            raise ValueError("Unknown or unversioned harness")
        cohorts.add(tuple(row[k] for k in COHORT))
        for metric in METRICS:
            if (
                not isinstance(row.get(metric), (int, float))
                or not math.isfinite(row[metric])
                or row[metric] < 0
            ):
                raise ValueError(f"Missing/invalid measurement: {metric}")
        if not isinstance(row.get("state_checks"), dict) or not row["state_checks"]:
            raise ValueError("Independent state checks are required")
        groups[row["harness"]].append(row)
    if len(cohorts) != 1:
        raise ValueError("Compare exactly one matching model/tool/prompt/machine cohort at a time")
    report = {}
    for harness, rows in groups.items():
        if len({r["version"] for r in rows}) != 1:
            raise ValueError("Do not mix versions of one harness")
        report[harness] = {
            "sufficient_samples": min(Counter(r["case"] for r in rows).values()) >= 30,
            "samples": len(rows),
            "version": sorted({r["version"] for r in rows}),
            "all_state_checks_pass": all(
                v is True for r in rows for v in r["state_checks"].values()
            ),
            **{
                metric: {
                    "p50": percentile([r[metric] for r in rows], 0.5),
                    "p95": percentile([r[metric] for r in rows], 0.95),
                }
                for metric in METRICS
            },
        }
    # Require the same cases AND repetitions before asserting any improvement.
    case_counts = {h: sorted((r["case"],) for r in rows) for h, rows in groups.items()}
    current = report.get("current")
    optimized = report.get("optimized")
    comparable = current and optimized and case_counts["current"] == case_counts["optimized"]
    gates = {"comparable_baseline": bool(comparable)}
    if comparable:
        gates.update(
            {
                "sufficient_samples": optimized["sufficient_samples"]
                and current["sufficient_samples"],
                "state": optimized["all_state_checks_pass"],
                "harness_p95_under_2s": optimized["harness_ms"]["p95"] < 2000,
                "no_post_completion_tools": all(
                    r["post_completion_tool_calls"] == 0 for r in groups["optimized"]
                ),
                "model_calls_reduced_80pct": optimized["model_calls"]["p95"]
                <= current["model_calls"]["p95"] * 0.2,
                "input_tokens_reduced_80pct": optimized["input_tokens"]["p95"]
                <= current["input_tokens"]["p95"] * 0.2,
            }
        )
    replacements = {}
    for name in ("opencode", "pi"):
        candidate = report.get(name)
        if optimized and candidate and case_counts[name] == case_counts["optimized"]:
            replacements[name] = (
                candidate["sufficient_samples"]
                and optimized["sufficient_samples"]
                and candidate["all_state_checks_pass"]
                and optimized["all_state_checks_pass"]
                and candidate["duration_ms"]["p95"] <= optimized["duration_ms"]["p95"] * 0.8
                and all(r["post_completion_tool_calls"] == 0 for r in groups[name])
            )
    return {
        "cohort": dict(zip(COHORT, next(iter(cohorts)), strict=True)),
        "harnesses": report,
        "optimization_gates": gates,
        "replacement_latency_gate": replacements,
        "note": "Latency gate alone does not approve replacement; permission, identity, cancellation and recovery parity are required.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("measurements", type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(json.loads(args.measurements.read_text())), indent=2))
