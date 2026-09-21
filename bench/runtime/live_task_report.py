"""Summarize every task-screening attempt, including failures and missing finishes."""

import argparse
import json
from collections import Counter
from pathlib import Path

from bench.interactive.statistics import summary


def report(path):
    events = [json.loads(line) for line in Path(path).read_text().splitlines()]
    if not events or "configuration" not in events[0]:
        raise ValueError("Missing screening configuration")
    config = events[0]["configuration"]
    expected = config["samples"]
    starts, finishes = {}, {}
    for event in events[1:]:
        if len(event) != 1 or next(iter(event)) not in {"started", "sample"}:
            raise ValueError("Invalid screening event")
        kind, row = next(iter(event.items()))
        index = row["index"]
        if type(index) is not int or not 0 <= index < expected:
            raise ValueError("Invalid repetition index")
        target = starts if kind == "started" else finishes
        if index in target or kind == "sample" and index not in starts:
            raise ValueError("Duplicate or unordered attempt")
        if kind == "sample" and row["request_id"] != starts[index]["request_id"]:
            raise ValueError("Attempt identity mismatch")
        target[index] = row
    rows = list(finishes.values())
    elapsed = [row["duration_ms"] for row in rows]
    latency = summary(elapsed)
    counts = {
        "verified": sum(row["verified"] is True for row in rows),
        "failed": sum(row["verified"] is not True for row in rows),
        "unfinished": len(starts.keys() - finishes.keys()),
        "not_started": expected - len(starts),
    }
    incomplete_calls = sum(
        bool(call.get("error_type")) for row in rows for call in row["provider_calls"]
    )
    unobserved_streams = sum(
        call.get("stream") is True and "stream_completed" not in call
        for row in rows
        for call in row["provider_calls"]
    )
    interrupted_streams = sum(
        call.get("stream_completed") is False for row in rows for call in row["provider_calls"]
    )
    latency_passed = (
        len(rows) == expected
        and latency["p95"] is not None
        and latency["p95"] <= 30000
        and max(elapsed) <= 60000
    )
    return {
        "configuration": config,
        "counts": counts,
        "elapsed_ms": latency,
        "over_30_seconds": sum(value > 30000 for value in elapsed),
        "over_60_seconds": sum(value > 60000 for value in elapsed),
        "provider_invocations": dict(
            Counter(call["model"] for row in rows for call in row["provider_calls"])
        ),
        "native_estimated_cost_usd": sum(
            run["estimated_cost_usd"] for row in rows for run in row["runs"]
        ),
        "provider_calls_without_result": incomplete_calls,
        "interrupted_streams": interrupted_streams,
        "streams_without_completion_telemetry": unobserved_streams,
        "cost_estimate_excludes_unknown_provider_usage": bool(
            incomplete_calls or counts["unfinished"] or interrupted_streams or unobserved_streams
        ),
        "post_return_model_calls": sum(row["post_return_model_calls"] for row in rows),
        "duplicate_task_samples": sum(row["task_count"] > 1 for row in rows),
        "stored_task_matches": sum(row.get("task_fields_match") is True for row in rows),
        "confirmed_action_samples": sum(
            row.get("effects", {}).get("confirmed") == 1 for row in rows
        ),
        "interrupted_after_confirmed_action": sum(
            row.get("effects", {}).get("confirmed") == 1
            and any(run.get("status") != "completed" for run in row["runs"])
            for row in rows
        ),
        "latency_passed": latency_passed,
        "screening_passed": expected >= 30 and counts["verified"] == expected and latency_passed,
        "manual_acceptance": False,
        "note": "All finished attempts contribute timing, including failed/cutoff attempts; missing attempts cannot qualify. Provider invocations count LiteLLM calls, not independently observed HTTP retries. Cost is native recorded estimate, not billing; interrupted calls may incur charges absent from this estimate. Direct native admission, not chat ingress/queue timing; no matched baseline or framework-selection claim.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = report(args.input)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
