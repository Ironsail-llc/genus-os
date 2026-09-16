"""The sandbox has no Ollama, so a vision task in it measured nothing.

The harness builds the task container's environment from a fixed dict. This
fleet's vision model is local (`ROBOTHOR_VISION_MODEL`, served by Ollama on
the box), and there is no Ollama in the container — so every image task ran
against a backend that could not answer, and `view_image` on a text-only
primary fell all the way through to "nobody looked".

`ROBOTHOR_VISION_REMOTE_MODEL` gives the container a vision backend on the
OpenRouter key it already carries. Same reasoning as the completion-contract
and step-efficiency rungs beside it: the harness measures the platform the
operator is deciding about, and a capability withheld from the container is a
capability the measurement says this platform does not have.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bench.wildclaw import harness

if TYPE_CHECKING:
    from pathlib import Path


def _env(tmp_path: Path, monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    task = {"task_id": "05_Productivity_task_8", "timeout_seconds": 1200}
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


def test_the_container_gets_a_vision_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ROBOTHOR_VISION_REMOTE_MODEL", raising=False)
    assert _env(tmp_path, monkeypatch)["ROBOTHOR_VISION_REMOTE_MODEL"] == harness.BENCH_VISION_MODEL


def test_the_host_setting_wins_when_the_operator_sets_one(tmp_path, monkeypatch) -> None:
    """So a sweep can compare vision models without rebuilding the image."""
    monkeypatch.setenv("ROBOTHOR_VISION_REMOTE_MODEL", "openrouter/anthropic/claude-opus-4.7")
    env = _env(tmp_path, monkeypatch)
    assert env["ROBOTHOR_VISION_REMOTE_MODEL"] == "openrouter/anthropic/claude-opus-4.7"


def test_the_chosen_model_can_actually_be_shown_an_image(tmp_path, monkeypatch) -> None:
    """A vision backend the registry says is text-only is not a vision backend.

    The engine refuses to hand images to a model declared `accepts_images
    False` — correctly — so choosing one here would have restored the blind
    run with an extra config line to explain it.
    """
    from robothor.engine.model_registry import get_model_limits

    assert get_model_limits(harness.BENCH_VISION_MODEL).accepts_images is True


def test_the_agent_may_call_the_batch_tool() -> None:
    """Capability parity: the manifest's allow-list is the whole tool set, so
    a tool missing from it is a tool the benchmark measures this platform as
    not having."""
    import pathlib

    import yaml

    manifest = yaml.safe_load(
        (pathlib.Path(harness.__file__).parent / "agent.yaml").read_text(encoding="utf-8")
    )
    assert "analyze_image" in manifest["tools_allowed"]
    assert "view_image" in manifest["tools_allowed"]
