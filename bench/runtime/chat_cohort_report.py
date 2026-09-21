"""Summarize live chat evidence without dropping failed or unfinished attempts."""

import json
from pathlib import Path

from bench.interactive.statistics import summary


def report(directory: Path, expected=30):
    events = [
        json.loads(line) for line in (directory / "cohort.events.jsonl").read_text().splitlines()
    ]
    starts, finishes = {}, {}
    for event in events:
        index = event["repetition"]
        if not isinstance(index, int) or not 0 <= index < expected:
            raise ValueError("unexpected repetition")
        target = starts if event["event"] == "sample_started" else finishes
        if event["event"] not in {"sample_started", "sample_finished"} or index in target:
            raise ValueError("invalid or duplicate journal event")
        if event["event"] == "sample_finished" and index not in starts:
            raise ValueError("finish without start")
        target[index] = event
    rows, durations = [], {"status": [], "pause": []}
    for index in range(expected):
        finish = finishes.get(index)
        state = "not_started" if index not in starts else "unresolved"
        detail = {}
        if finish is not None:
            name = finish.get("artifact")
            if name is not None:
                if name != f"sample-{index:03d}.json":
                    raise ValueError("unexpected sample artifact path")
                detail = json.loads((directory / name).read_text())
            turns = detail.get("transcript", [])
            correct = (
                len(turns) == 2
                and all(turn.get("run", {}).get("status") == "completed" for turn in turns)
                and detail.get("goal_status") == "paused"
                and detail.get("task_statuses") == ["DONE", "TODO"]
            )
            state = "passed" if finish["exit_code"] == 0 and correct else "failed"
            for position, metric in enumerate(durations):
                if position < len(turns):
                    value = turns[position].get("elapsed_ms")
                    if isinstance(value, (int, float)) and value >= 0:
                        durations[metric].append(value)
        rows.append(
            {
                "repetition": index,
                "state": state,
                "exit_code": finish.get("exit_code") if finish else None,
            }
        )
    metrics = {}
    for turn, values in durations.items():
        metrics[turn] = {**summary(values), "missing": expected - len(values)}
    counts = {
        state: sum(row["state"] == state for row in rows)
        for state in ["passed", "failed", "unresolved", "not_started"]
    }
    latency_passed = all(
        metric["missing"] == 0
        and metric["p95"] is not None
        and metric["p95"] <= 30_000
        and max(durations[turn], default=float("inf")) <= 60_000
        for turn, metric in metrics.items()
    )
    return {
        "expected_conversations": expected,
        "counts": counts,
        "conversations": rows,
        "elapsed_ms": metrics,
        "complete_population": len(finishes) == expected,
        "manual_acceptance": False,
        "screening_passed": expected >= 30 and counts["passed"] == expected and latency_passed,
        "screening_targets": {"turn_p95_ms": 30_000, "turn_deadline_ms": 60_000},
        "latency_note": "Measured distributions include completed failed attempts when timing exists. Missing turns remain counted and cannot qualify the cohort. Status and pause are summarized separately.",
    }
