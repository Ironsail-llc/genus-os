"""The step-efficiency rung has to reach the container, or it measures nothing.

The harness builds the task container's environment from a fixed dict and
forwards a host ``ROBOTHOR_*`` variable only when the task's own ``env:`` block
names it. `ROBOTHOR_STEP_EFFICIENCY_MODE` is named by no task, so a sandbox
re-measure with the controls "turned on" would have run them at the in-container
default and produced a flat result that read as "the controls do nothing".

Same reasoning as `ROBOTHOR_COMPLETION_CONTRACTS_MODE` two lines above it: the
harness measures the platform the operator is deciding about, so the rung the
operator sets is the rung the container runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bench.wildclaw import harness

if TYPE_CHECKING:
    from pathlib import Path


def _env(tmp_path: Path, monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    task = {"task_id": "02_Code_task_1", "timeout_seconds": 1200}
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


def test_the_rung_reaches_the_container(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ROBOTHOR_STEP_EFFICIENCY_MODE", raising=False)
    assert _env(tmp_path, monkeypatch)["ROBOTHOR_STEP_EFFICIENCY_MODE"] == "enforce"


def test_the_host_setting_wins_when_the_operator_sets_one(tmp_path, monkeypatch) -> None:
    """So a differential sweep can run the same image at `off` and `enforce`."""
    monkeypatch.setenv("ROBOTHOR_STEP_EFFICIENCY_MODE", "off")
    assert _env(tmp_path, monkeypatch)["ROBOTHOR_STEP_EFFICIENCY_MODE"] == "off"


def test_it_defaults_to_enforce_not_to_the_fleet_default(tmp_path, monkeypatch) -> None:
    """The fleet ships at `observe`; the harness exists to measure `enforce`.
    A benchmark that silently measured `observe` would report no effect and be
    believed."""
    monkeypatch.delenv("ROBOTHOR_STEP_EFFICIENCY_MODE", raising=False)
    env = _env(tmp_path, monkeypatch)
    assert env["ROBOTHOR_STEP_EFFICIENCY_MODE"] != "observe"
