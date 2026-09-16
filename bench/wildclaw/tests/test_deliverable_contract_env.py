"""The deliverable contract has to reach the container, or it measures nothing.

Three Productivity tasks scored 0 against a competitor's 86 / 91 / 49 on
2026-09-16, purely on output shape, and the harness was running with this
control off: `ROBOTHOR_COMPLETION_CONTRACTS_*` was in the container env dict and
`ROBOTHOR_DELIVERABLE_CONTRACT_*` was not. A re-measure "with the fix on" would
have run it at the in-container default and read the flat result as "the fix
does nothing" — the same trap `test_step_efficiency_env.py` exists for.

Same idiom as the two rungs above it: default `enforce`, host value wins, so an
off-vs-enforce differential sweep still works on one image.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bench.wildclaw import harness

if TYPE_CHECKING:
    from pathlib import Path


def _env(tmp_path: Path, monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    task = {"task_id": "01_Productivity_task_4", "timeout_seconds": 1200}
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


def test_the_control_is_on_in_the_container(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ROBOTHOR_DELIVERABLE_CONTRACT_ENABLED", raising=False)
    monkeypatch.delenv("ROBOTHOR_DELIVERABLE_CONTRACT_MODE", raising=False)
    env = _env(tmp_path, monkeypatch)
    assert env["ROBOTHOR_DELIVERABLE_CONTRACT_ENABLED"] == "1"
    assert env["ROBOTHOR_DELIVERABLE_CONTRACT_MODE"] == "enforce"


def test_the_host_setting_wins_when_the_operator_sets_one(tmp_path, monkeypatch) -> None:
    """So the off-vs-enforce differential that proves the control did the work
    can be run on one image."""
    monkeypatch.setenv("ROBOTHOR_DELIVERABLE_CONTRACT_MODE", "off")
    assert _env(tmp_path, monkeypatch)["ROBOTHOR_DELIVERABLE_CONTRACT_MODE"] == "off"


def test_it_does_not_measure_the_fleet_default(tmp_path, monkeypatch) -> None:
    """The fleet ships at `observe`, where the control records and changes
    nothing. A sweep that silently measured that would report no effect and be
    believed."""
    monkeypatch.delenv("ROBOTHOR_DELIVERABLE_CONTRACT_MODE", raising=False)
    assert _env(tmp_path, monkeypatch)["ROBOTHOR_DELIVERABLE_CONTRACT_MODE"] != "observe"


def test_the_bench_model_is_in_the_registry() -> None:
    """The sweep's own model was unregistered: one run logged the conservative
    128K fallback 655 times, so every context decision it made was sized for a
    model with an eighth of the real window. Both spellings must resolve."""
    from robothor.engine.model_registry import get_model_limits

    prefixed = get_model_limits("openrouter/z-ai/glm-5.2")
    bare = get_model_limits("z-ai/glm-5.2")
    assert prefixed.max_input_tokens == 1_048_576
    assert prefixed.max_output_tokens == 163_840
    assert bare == prefixed


def test_the_task_spec_reaches_the_contract_through_the_run() -> None:
    """`run_one` hands the task spec to `runner.execute` as the user message,
    `AgentSession.start` persists it as `run.task_text`, and the contract reads
    it there. That chain is the source the control never had: before it, the
    finalizer could see a count of the prompt and not the prompt."""
    from robothor.engine.deliverable_contract import extract_contract, task_text_for_run
    from robothor.engine.models import AgentRun
    from robothor.engine.session import AgentSession

    spec = "Compile the rows and save them to:\n\n- `/tmp_workspace/results/out.tsv`\n"
    session = AgentSession(AgentRun(agent_id="wildclaw"), "wildclaw")
    session.start("system", spec, [])
    contract = extract_contract(task_text_for_run(session.run, session))
    assert [i.path for i in contract.items] == ["/tmp_workspace/results/out.tsv"]
