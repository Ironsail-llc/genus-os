"""Compare JSON measurements only within identical model/tool/machine cohorts.

Input: a JSON array of records. Each record must name harness, version, case,
model, reasoning, startup (cold/warm), machine, prompt_hash, tools_hash,
cohort (local/cloud), duration_ms,
harness_ms, model_calls, input_tokens, post_completion_tool_calls, and
state_checks (a nonempty mapping of independent fixture assertions to bool).
Never substitute transcript claims for fixture state checks.
Qualification also requires nonempty model_settings and resources objects.
Legacy records remain readable, but missing configuration cannot qualify a gate.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

METRICS = ("duration_ms", "harness_ms", "model_calls", "input_tokens", "post_completion_tool_calls")
COHORT = ("cohort", "model", "reasoning", "startup", "machine", "prompt_hash", "tools_hash")
CONFIGURATION = ("model_settings", "resources")
HARNESSES = {"current", "optimized", "minimal", "opencode", "pi", "pydantic-ai", "deepagents"}
EXTRA_METRICS = (
    "output_tokens",
    "cost_usd",
    "queue_ms",
    "ack_ms",
    "stop_ack_ms",
    "recovery_ms",
    "descendant_control_ms",
)


def percentile(values: list[float], p: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)]


def compare(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups = defaultdict(list)
    cohorts = set()
    configurations = set()
    configuration_complete = True
    for row in records:
        if row["harness"] not in HARNESSES or not row.get("version"):
            raise ValueError("Unknown or unversioned harness")
        cohorts.add(tuple(row[k] for k in COHORT))
        configuration = []
        for name in CONFIGURATION:
            value = row.get(name)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"Configuration must be an object: {name}")
            configuration_complete &= bool(value)
            configuration.append(json.dumps(value, sort_keys=True, allow_nan=False))
        configurations.add(tuple(configuration))
        for metric in METRICS:
            if (
                isinstance(row.get(metric), bool)
                or not isinstance(row.get(metric), (int, float))
                or not math.isfinite(row[metric])
                or row[metric] < 0
            ):
                raise ValueError(f"Missing/invalid measurement: {metric}")
        if not isinstance(row.get("state_checks"), dict) or not row["state_checks"]:
            raise ValueError("Independent state checks are required")
        if row.get("status", "completed") not in {
            "completed",
            "failed",
            "timeout",
            "cancelled",
            "unresolved",
        }:
            raise ValueError("Unknown terminal status")
        for metric in EXTRA_METRICS:
            if metric in row and (
                type(row[metric]) not in (int, float)
                or not math.isfinite(row[metric])
                or row[metric] < 0
            ):
                raise ValueError(f"Invalid measurement: {metric}")
        groups[row["harness"]].append(row)
    if len(cohorts) != 1 or len(configurations) != 1:
        raise ValueError(
            "Compare exactly one matching model/tool/prompt/machine/configuration cohort at a time"
        )
    from bench.interactive.statistics import summary

    report: dict[str, Any] = {}
    for harness, rows in groups.items():
        if len({r["version"] for r in rows}) != 1:
            raise ValueError("Do not mix versions of one harness")
        report[harness] = {
            "sufficient_samples": min(Counter(r["case"] for r in rows).values()) >= 30,
            "samples": len(rows),
            "finalist_samples": min(Counter(r["case"] for r in rows).values()) >= 100,
            "outcomes": dict(Counter(r.get("status", "completed") for r in rows)),
            "all_completed": all(r.get("status", "completed") == "completed" for r in rows),
            "scenarios": {
                case: {
                    "samples": len(case_rows),
                    "outcomes": dict(Counter(r.get("status", "completed") for r in case_rows)),
                    "all_state_checks_pass": all(
                        v is True for r in case_rows for v in r["state_checks"].values()
                    ),
                    **{
                        metric: summary([r[metric] for r in case_rows if metric in r])
                        for metric in (*METRICS, *EXTRA_METRICS)
                    },
                }
                for case in sorted({r["case"] for r in rows})
                for case_rows in [[r for r in rows if r["case"] == case]]
            },
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
    gates = {
        "configuration_complete": configuration_complete,
        "comparable_baseline": bool(comparable and configuration_complete),
    }
    if comparable and current is not None and optimized is not None:
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
    for name in ("opencode", "pi", "pydantic-ai", "deepagents"):
        candidate = report.get(name)
        if optimized and candidate and case_counts[name] == case_counts["optimized"]:
            replacements[name] = (
                configuration_complete
                and candidate["all_completed"]
                and optimized["all_completed"]
                and candidate["sufficient_samples"]
                and optimized["sufficient_samples"]
                and candidate["all_state_checks_pass"]
                and optimized["all_state_checks_pass"]
                and candidate["duration_ms"]["p95"] <= optimized["duration_ms"]["p95"] * 0.8
                and all(r["post_completion_tool_calls"] == 0 for r in groups[name])
                and all(
                    candidate["scenarios"][case]["duration_ms"]["p95"]
                    <= baseline["duration_ms"]["p95"] * 1.1
                    for case, baseline in optimized["scenarios"].items()
                )
            )
    return {
        "cohort": dict(zip(COHORT, next(iter(cohorts)), strict=True)),
        "configuration": dict(
            zip(CONFIGURATION, map(json.loads, next(iter(configurations))), strict=True)
        ),
        "harnesses": report,
        "optimization_gates": gates,
        "replacement_latency_gate": replacements,
        "finalist_latency_gate": {
            name: passed
            and report[name]["finalist_samples"]
            and report["optimized"]["finalist_samples"]
            for name, passed in replacements.items()
        },
        "note": "Latency gate alone does not approve replacement; permission, identity, cancellation and recovery parity are required.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("measurements", type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(json.loads(args.measurements.read_text())), indent=2))
