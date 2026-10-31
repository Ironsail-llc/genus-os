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


@pytest.mark.parametrize("name", ["ci.yml", "install-gate.yml"])
def test_ci_reads_the_ci_baseline_set_not_a_named_file(name: str) -> None:
    """A named --baseline that is empty fails the step; the set skips it, as documented."""
    for label, step in _bench_steps(name):
        script = str(step["run"])
        assert "--baseline-set ci" in script, f"{label}: compare with --baseline-set ci"
        assert "--baseline " not in script, f"{label}: a named --baseline errors when empty"


def test_the_gate_runs_on_every_path_that_feeds_the_prompt() -> None:
    """prompt_tokens is exact, so a skill or template change must meet the gate too."""
    (label, step), *_ = [
        (label, step)
        for label, step in (
            (f"ci.yml:bench-turn:{s.get('name')}", s)
            for s in yaml.safe_load((WORKFLOWS / "ci.yml").read_text())["jobs"]["bench-turn"][
                "steps"
            ]
        )
        if "git diff --name-only" in str(step.get("run", ""))
    ]
    script = str(step["run"])
    for path in ("robothor/engine/", "agents/", "templates/", "robothor/templates/"):
        assert path in script, f"{label}: {path} changes the prompt but skips the gate"
