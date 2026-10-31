"""The CI steps that run the benchmark must be able to fail."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"


def _bench_steps(name: str) -> list[tuple[str, dict]]:
    workflow = yaml.safe_load((WORKFLOWS / name).read_text())
    return [
        (f"{name}:{job_name}:{step.get('name', '<unnamed>')}", step)
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if "python -m bench.turn" in str(step.get("run", ""))
    ]


@pytest.mark.parametrize("name", ["ci.yml", "install-gate.yml"])
def test_each_workflow_runs_the_benchmark(name: str) -> None:
    assert _bench_steps(name), f"{name} no longer runs python -m bench.turn"


@pytest.mark.parametrize("name", ["ci.yml", "install-gate.yml"])
def test_a_benchmark_piped_into_tee_keeps_its_exit_code(name: str) -> None:
    """GitHub's default `bash -e {0}` has no pipefail, so `| tee` would return 0."""
    for label, step in _bench_steps(name):
        script = str(step["run"])
        if "| tee" not in script:
            continue
        shell = str(step.get("shell", ""))
        assert "set -o pipefail" in script or shell == "bash", (
            f"{label}: `| tee` without pipefail hides the benchmark's exit code"
        )
