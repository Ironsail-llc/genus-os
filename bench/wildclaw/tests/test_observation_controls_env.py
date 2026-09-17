"""The observation rungs have to reach the container, or they measure nothing.

Same reasoning as `test_step_efficiency_env` beside this file. The harness
builds the task container's environment from a fixed dict and forwards a host
`ROBOTHOR_*` only when the task's own `env:` block names it — and no task names
any of these. The platform default for all three is `observe`, which by design
changes nothing an agent sees, so a sweep left on the default would produce a
flat result that reads as "the controls do nothing".

Verdict commitment is the exception and it is deliberate: it is the ladder that
touches model judgement, it has not been probed with a real double-verdict
artefact, and a graded run is the wrong place to discover a false positive. It
runs where the fleet runs it until that probe exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bench.wildclaw import harness

if TYPE_CHECKING:
    from pathlib import Path


def _env(tmp_path: Path, monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    task = {"task_id": "03_Social_task_2", "timeout_seconds": 300}
    workspace = tmp_path / "ws"
    workspace.mkdir()
    out_dir = tmp_path / "out" / "task"
    out_dir.mkdir(parents=True)
    _cmd, env_file = harness._container_command(task, workspace, out_dir, None)
    pairs: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            pairs[key] = value
    return pairs


@pytest.mark.parametrize("name", ["ROBOTHOR_TRUNCATION_LEDGER_MODE", "ROBOTHOR_ACT_OBSERVE_MODE"])
def test_the_measured_rungs_reach_the_container_at_enforce(
    tmp_path, monkeypatch, name: str
) -> None:
    assert _env(tmp_path, monkeypatch)[name] == "enforce"


@pytest.mark.parametrize("name", ["ROBOTHOR_TRUNCATION_LEDGER_MODE", "ROBOTHOR_ACT_OBSERVE_MODE"])
def test_the_host_value_wins_so_a_differential_still_works(
    tmp_path, monkeypatch, name: str
) -> None:
    """An off-vs-enforce sweep is how "did it help" gets answered at all."""
    monkeypatch.setenv(name, "off")
    assert _env(tmp_path, monkeypatch)[name] == "off"


def test_verdict_commitment_runs_where_the_fleet_runs_it(tmp_path, monkeypatch) -> None:
    assert _env(tmp_path, monkeypatch)["ROBOTHOR_VERDICT_COMMITMENT_MODE"] == "observe"


def test_the_bench_model_has_real_limits_rather_than_the_128k_fallback(tmp_path) -> None:
    """Both spellings. The sweep runs `openrouter/z-ai/glm-5.2` and provider
    responses come back without the routing prefix, so cost fallback and
    context sizing look up two different strings for one model.
    """
    from robothor.engine.model_registry import get_model_limits

    for spelling in ("openrouter/z-ai/glm-5.2", "z-ai/glm-5.2"):
        limits = get_model_limits(spelling)
        assert limits.max_input_tokens > 128_000, spelling
