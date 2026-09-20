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
