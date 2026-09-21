import json

import pytest

from bench.runtime.live_task_report import report


def write(path, events):
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def test_failures_and_unfinished_attempts_stay_in_the_population(tmp_path):
    path = tmp_path / "events.jsonl"
    rows = [{"configuration": {"samples": 30}}]
    for index in range(29):
        rows.append({"started": {"index": index, "request_id": str(index)}})
        if index == 28:
            continue
        rows.append(
            {
                "sample": {
                    "index": index,
                    "request_id": str(index),
                    "verified": index < 26,
                    "duration_ms": 65000 if index >= 26 else 1000,
                    "provider_calls": [{"model": "fixture"}],
                    "runs": [{"estimated_cost_usd": 0}],
                    "post_return_model_calls": 0,
                    "task_count": 0 if index >= 26 else 1,
                }
            }
        )
    write(path, rows)
    result = report(path)
    assert result["counts"] == {"verified": 26, "failed": 2, "unfinished": 1, "not_started": 1}
    assert result["elapsed_ms"]["p95"] == 65000 and result["over_60_seconds"] == 2
    assert not result["screening_passed"]


def test_duplicate_finish_is_not_counted_as_an_extra_success(tmp_path):
    path = tmp_path / "events.jsonl"
    row = {"index": 0, "request_id": "one"}
    write(
        path,
        [{"configuration": {"samples": 30}}, {"started": row}, {"sample": row}, {"sample": row}],
    )
    with pytest.raises(ValueError, match="Duplicate"):
        report(path)


def test_confirmed_write_does_not_hide_an_interrupted_request(tmp_path):
    path = tmp_path / "events.jsonl"
    identity = {"index": 0, "request_id": "one"}
    row = {
        **identity,
        "verified": False,
        "duration_ms": 60010,
        "provider_calls": [],
        "runs": [{"estimated_cost_usd": 0, "status": "cancelled"}],
        "post_return_model_calls": 0,
        "task_count": 1,
        "task_fields_match": True,
        "effects": {"confirmed": 1},
    }
    write(path, [{"configuration": {"samples": 1}}, {"started": identity}, {"sample": row}])
    result = report(path)
    assert result["counts"]["failed"] == 1
    assert result["stored_task_matches"] == result["confirmed_action_samples"] == 1
    assert result["interrupted_after_confirmed_action"] == 1
    assert not result["screening_passed"]
