"""The two throughput changes have to reach the graded run, or they measure nothing.

Measured 2026-09-16 over ten runs: Genus issued ZERO parallel tool calls, and
had no way to call a tool from inside code. The competing harness batched on
twenty turns of one task and imported its tool library inside a code sandbox 52
/ 13 / 7 times on the three tasks we scored 0.000 on.

Both fixes are platform features the fleet ships. Measuring the platform
without a capability the platform ships measures the wrong platform — the same
reasoning that put `view_image`, `analyze_image`, `skill_view` and `todo_write`
in this manifest. Neither is task-specific and neither is a hint about any
answer.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from bench.wildclaw import harness

MANIFEST = Path(__file__).resolve().parents[1] / "agent.yaml"


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text())


def _env(tmp_path, monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    task = {"task_id": "01_Productivity_Flow_task_1", "timeout_seconds": 1200}
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


class TestTheManifestGrantsTheCodeTool:
    def test_execute_code_is_granted(self):
        assert "execute_code" in _manifest()["tools_allowed"]

    def test_exec_is_granted_too_because_execute_code_requires_it(self):
        """The tool refuses an agent whose manifest does not grant `exec`, so a
        manifest that named one without the other would advertise a tool that
        could never run."""
        allowed = _manifest()["tools_allowed"]
        assert "exec" in allowed


class TestTheParallelSettingReachesTheContainer:
    def test_it_is_in_the_container_environment(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_PARALLEL_TOOL_CALLS", raising=False)
        assert _env(tmp_path, monkeypatch)["ROBOTHOR_PARALLEL_TOOL_CALLS"] == "4"

    def test_the_host_setting_wins_so_a_sequential_control_is_possible(self, tmp_path, monkeypatch):
        """1 is fully sequential — the before-picture for a differential sweep."""
        monkeypatch.setenv("ROBOTHOR_PARALLEL_TOOL_CALLS", "1")
        assert _env(tmp_path, monkeypatch)["ROBOTHOR_PARALLEL_TOOL_CALLS"] == "1"
