from copy import deepcopy

import pytest

from bench.interactive.compare import compare


def samples(harness, duration):
    return [
        {
            "harness": harness,
            "version": "test",
            "case": "add",
            "cohort": "local",
            "model": "same",
            "reasoning": "same",
            "startup": "warm",
            "machine": "same",
            "prompt_hash": "same",
            "tools_hash": "same",
            "model_settings": {"temperature": 0.5, "max_output_tokens": 512},
            "resources": {"concurrency": 1, "deadline_seconds": 60},
            "duration_ms": duration,
            "harness_ms": 10,
            "model_calls": 0,
            "input_tokens": 0,
            "post_completion_tool_calls": 0,
            "state_checks": {"attendees": True, "rsvp": True},
        }
        for _ in range(30)
    ]


def test_replacement_requires_state_and_enough_samples():
    rows = samples("optimized", 100) + samples("pi", 70)
    assert compare(rows)["replacement_latency_gate"]["pi"] is True
    bad = deepcopy(rows)
    bad[-1]["state_checks"]["rsvp"] = False
    assert compare(bad)["replacement_latency_gate"]["pi"] is False
    assert compare([rows[0], rows[-1]])["replacement_latency_gate"]["pi"] is False


def test_cannot_report_mixed_model_improvement():
    rows = samples("current", 100) + samples("optimized", 10)
    rows[-1]["model"] = "different"
    with pytest.raises(ValueError, match="cohort"):
        compare(rows)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("model_settings", "temperature", 0.0),
        ("model_settings", "max_output_tokens", 4096),
        ("resources", "concurrency", 20),
        ("resources", "deadline_seconds", 120),
    ],
)
def test_different_settings_or_resource_limits_cannot_qualify(section, key, value):
    rows = samples("optimized", 100) + samples("deepagents", 50)
    rows[-1][section][key] = value
    with pytest.raises(ValueError, match="configuration cohort"):
        compare(rows)


def test_legacy_unknown_configuration_is_reported_without_qualification():
    rows = samples("optimized", 100) + samples("deepagents", 50)
    for row in rows:
        del row["model_settings"]
        del row["resources"]
    report = compare(rows)
    assert report["harnesses"]["deepagents"]["samples"] == 30
    assert not report["optimization_gates"]["configuration_complete"]
    assert not report["replacement_latency_gate"]["deepagents"]


def test_pooled_speedup_cannot_hide_scenario_regression():
    baseline = samples("optimized", 1000) + [
        dict(r, case="critical", duration_ms=10) for r in samples("optimized", 10)
    ]
    candidate = samples("pydantic-ai", 500) + [
        dict(r, case="critical", duration_ms=12) for r in samples("pydantic-ai", 12)
    ]
    result = compare(baseline + candidate)
    assert result["replacement_latency_gate"]["pydantic-ai"] is False
    assert result["harnesses"]["pydantic-ai"]["scenarios"]["critical"]["duration_ms"][
        "p95_ci95"
    ] == [12, 12]


def test_failure_and_timeout_are_retained_and_disqualify():
    rows = samples("optimized", 100) + samples("deepagents", 70)
    rows[-1].update(status="timeout", duration_ms=60000, cost_usd=0.02, queue_ms=30)
    result = compare(rows)
    assert result["replacement_latency_gate"]["deepagents"] is False
    candidate = result["harnesses"]["deepagents"]
    assert candidate["samples"] == 30 and candidate["outcomes"]["timeout"] == 1
    assert candidate["scenarios"]["add"]["cost_usd"]["samples"] == 1
    assert result["finalist_latency_gate"]["deepagents"] is False


def test_finalist_requires_one_hundred_repetitions():
    rows = samples("optimized", 100) + samples("deepagents", 70)
    assert compare(rows)["replacement_latency_gate"]["deepagents"]
    assert not compare(rows)["finalist_latency_gate"]["deepagents"]


@pytest.mark.parametrize("status", ["timeout", "failed", "cancelled", "completed"])
def test_unknown_usage_is_retained_without_zero_imputation_or_qualification(status):
    rows = samples("optimized", 100) + samples("deepagents", 70)
    rows[-1].update(
        status=status,
        duration_ms=60000,
        model_calls=None,
        input_tokens=None,
        cost_usd=None,
        harness_ms=None,
        post_completion_tool_calls=None,
    )
    report = compare(rows)
    candidate = report["harnesses"]["deepagents"]
    assert candidate["samples"] == 30
    assert candidate["outcomes"][status] >= 1
    assert candidate["duration_ms"]["samples"] == 30
    assert candidate["duration_ms"]["unknown_samples"] == 0
    assert candidate["model_calls"]["samples"] == 29
    assert candidate["model_calls"]["unknown_samples"] == 1
    cost = candidate["scenarios"]["add"]["cost_usd"]
    assert cost["samples"] == 0 and cost["unknown_samples"] == 30
    assert cost["p95"] is None
    assert not candidate["measurements_complete"]
    assert not report["replacement_latency_gate"]["deepagents"]


def test_unknown_baseline_usage_does_not_qualify_known_candidate():
    rows = samples("optimized", 100) + samples("deepagents", 70)
    rows[0]["input_tokens"] = None
    assert not compare(rows)["replacement_latency_gate"]["deepagents"]


def test_unknown_optimization_usage_does_not_assert_reduction():
    rows = samples("current", 100) + samples("optimized", 50)
    for row in rows:
        row["model_calls"] = row["input_tokens"] = None
    report = compare(rows)
    gates = report["optimization_gates"]
    assert not gates["measurements_complete"]
    assert "model_calls_reduced_80pct" not in gates
    assert report["harnesses"]["current"]["input_tokens"]["p95"] is None


@pytest.mark.parametrize("value", [None, -1, True, float("nan")])
def test_missing_or_invalid_elapsed_time_is_not_a_measurement(value):
    rows = samples("optimized", 100)
    rows[0]["duration_ms"] = value
    with pytest.raises(ValueError, match="duration_ms"):
        compare(rows)
