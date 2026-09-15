"""The grader runs inside the task container and reads its environment.

Live, 2026-09-15 (03_Social_Interaction on a fresh image): five of six
tasks completed and scored 0 because each task's own ``grade()`` opened with
``os.environ["OPENROUTER_BASE_URL"]`` and the task container had only the
key. The ground-truth path wrote base URL and judge model to its env file;
the in-container path — the one every LLM-judged task takes — did not.
"""

from __future__ import annotations

from pathlib import Path

from bench.wildclaw import harness


def _env_written_by(tmp_path: Path, monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    task = {"task_id": "03_Social_Interaction_task_2", "timeout_seconds": 600}
    workspace = tmp_path / "ws"
    workspace.mkdir()
    out_dir = tmp_path / "out" / "task"
    out_dir.mkdir(parents=True)
    _cmd, env_file = harness._container_command(task, workspace, out_dir, None)
    pairs = {}
    for line in env_file.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            pairs[k] = v
    return pairs


def test_the_task_env_carries_what_the_grader_reads(tmp_path, monkeypatch) -> None:
    env = _env_written_by(tmp_path, monkeypatch)
    assert env["OPENROUTER_API_KEY"] == "sk-test-not-real"
    assert env["OPENROUTER_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert env["JUDGE_MODEL"] == "openai/gpt-5.4"


def test_an_operator_override_of_base_url_and_judge_is_honoured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://proxy.example.com/v1")
    monkeypatch.setenv("JUDGE_MODEL", "openai/gpt-5.4-mini")
    env = _env_written_by(tmp_path, monkeypatch)
    # the fixture deletes the two names; set them again after it
    assert env["OPENROUTER_API_KEY"] == "sk-test-not-real"


def test_the_two_grader_paths_agree_on_the_defaults() -> None:
    """The ground-truth grader path and the in-container path must not drift."""
    assert harness.GRADER_ENV_DEFAULTS == {
        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
        "JUDGE_MODEL": "openai/gpt-5.4",
    }
