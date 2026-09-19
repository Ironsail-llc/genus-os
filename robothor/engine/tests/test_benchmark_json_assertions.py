"""Structured deliverables must not pass by narrating the expected answer."""

import pytest

from robothor.engine.tools.handlers.benchmark import (
    _score_task,
    _score_task_detailed,
    _validate_task,
)


def expected():
    return {
        "must_contain": ["opt_out"],
        "json_assertions": [
            {"path": "/classification", "op": "equals", "value": "opt_out"},
            {"path": "/draft", "op": "equals", "value": None},
            {"path": "/approved", "op": "absent"},
        ],
    }


@pytest.mark.parametrize(
    "output",
    [
        "I classified this as opt_out and will suppress it.",
        '{"classification":"question","draft":null,"reason":"opt_out"}',
        '{"classification":"opt_out","draft":{"body":"Buy now"}}',
        '{"classification":"opt_out","draft":null,"approved":true}',
        '{"classification":"opt_out"}',
        '{"classification":"question","classification":"opt_out","draft":null}',
        '{"classification":"opt_out","draft":null,"x":NaN}',
        '```json\n{"classification":"opt_out","draft":null}\n```',
    ],
)
async def test_invalid_contract_cannot_be_diluted_by_other_passing_checks(output):
    assert _score_task(output, expected(), {}) == 0
    score, detail = await _score_task_detailed(output, expected(), {})
    assert score == 0 and detail["json_assertions"]["passed"] is False


async def test_valid_json_contract_passes_both_scoring_paths():
    output = '{"classification":"opt_out","draft":null}'
    assert _score_task(output, expected(), {}) == 1
    score, detail = await _score_task_detailed(output, expected(), {})
    assert score == 1 and detail["json_assertions"]["passed"] is True


@pytest.mark.parametrize(
    "assertion",
    [
        {"path": "/a~1b/~0key/0", "op": "equals", "value": True},
        {"path": "/a~1b/~0key", "op": "length", "value": 1},
        {"path": "/a~1b/~0key", "op": "contains", "value": True},
        {"path": "", "op": "type", "value": "object"},
    ],
)
def test_nested_and_escaped_paths(assertion):
    assert _score_task('{"a/b":{"~key":[true]}}', {"json_assertions": [assertion]}, {}) == 1


@pytest.mark.parametrize("op,value", [("equals", 1), ("contains", 1)])
def test_booleans_are_not_numbers(op, value):
    output = "true" if op == "equals" else "[true]"
    assert (
        _score_task(output, {"json_assertions": [{"path": "", "op": op, "value": value}]}, {}) == 0
    )


@pytest.mark.parametrize(
    "checks",
    [
        [],
        {},
        [{"path": "bad", "op": "equals", "value": 1}],
        [{"path": "/bad~2", "op": "absent"}],
        [{"path": "", "op": "unknown"}],
        [{"path": "", "op": [], "value": None}],
        [{"path": "", "op": "equals"}],
        [{"path": "", "op": "absent", "value": None}],
        [{"path": "", "op": "type", "value": "made_up"}],
        [{"path": "", "op": "length", "value": True}],
        [{"path": "", "op": "equals", "value": 1, "typo": True}],
    ],
)
def test_invalid_assertions_rejected_before_running(checks):
    task = {"id": "contract", "prompt": "Return JSON", "expected": {"json_assertions": checks}}
    assert "json_assertions" in _validate_task(task)


def test_valid_contract_is_accepted():
    assert (
        _validate_task({"id": "contract", "prompt": "Return JSON", "expected": expected()}) is None
    )


async def test_strict_mode_requires_semantics_not_just_valid_fields(monkeypatch):
    from robothor.engine.tools.handlers import benchmark

    async def judge(*args):
        return benchmark.JudgeOutcome(score=0.0)

    monkeypatch.setattr(benchmark, "_judge_output", judge)
    contract = {
        **expected(),
        "require_all": True,
        "tools_not_used": ["exec", "write_file", "web_fetch"],
        "judge": {"rubric": ["No unsupported promises"], "threshold": 1.0},
    }
    score, _ = await _score_task_detailed('{"classification":"opt_out","draft":null}', contract, {})
    assert score == 0


def test_strict_mode_does_not_average_away_a_failed_check():
    contract = {"require_all": True, "must_contain": ["yes", "yes", "yes", "absent"]}
    assert _score_task("yes", contract, {}) == 0
    contract["require_all"] = False
    assert _score_task("yes", contract, {}) == 0.75


def test_strict_mode_flag_must_be_boolean():
    task = {
        "id": "contract",
        "prompt": "Return JSON",
        "expected": {**expected(), "require_all": "true"},
    }
    assert "require_all" in _validate_task(task)
