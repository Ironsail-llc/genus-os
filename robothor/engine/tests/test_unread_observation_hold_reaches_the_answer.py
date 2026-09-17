"""The honest completion has to be in the ANSWER, not in the transcript.

Hostile review 2026-09-17, finding C1. The first cut of
`unread_observation_hold` appended

    [SYSTEM] Finishing with these observations still unread: …

to `session.messages` and returned **False**. `nudge_for_missing_deliverable`
propagated the False, `runner._run_loop`'s stop branch is

    if nudge_for_missing_deliverable(session, _workspace):
        continue
    return

so the runner returned with no further LLM call, and `session.get_final_text`
walks backwards for the last message whose role is `assistant` — which is the
answer produced BEFORE the note. The run therefore completed with byte-identical
output to what `observe` would have produced, plus one dead message nobody read.

The test that certified it asserted `"still unread" in session.messages[-1]` —
that the engine had appended a string to itself. That is the anti-pattern this
repo has a name for twice over, so these tests assert on what the RUN said and
on the rows it left, through the real loop with a stubbed model.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode, RunStatus
from robothor.engine.runner import AgentRunner
from robothor.engine.session import ENGINE_CONTEXT_ROLE

BIG = "x" * 12_431


def _tool_call(name: str, args: dict | None = None, call_id: str = "call_1"):
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = json.dumps(args or {})
    return tc


def _response(content=None, tool_calls=None, model="test-model"):
    response = MagicMock()
    response.model = model
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    response.usage = usage
    return response


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "exec"}},
            {"type": "function", "function": {"name": "read_file"}},
        ]
        registry.get_tool_names.return_value = ["exec", "read_file"]
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


@pytest.fixture
def agent_config() -> AgentConfig:
    return AgentConfig(
        id="hold-agent",
        name="Hold Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        timeout_seconds=30,
        delivery_mode=DeliveryMode.NONE,
        planning_enabled=False,
        scratchpad_enabled=False,
        error_feedback=False,
    )


async def _drive(runner, agent_config, answers: list[str], tmp_path):
    """One truncated `exec`, then `answers` in order, one per stopping turn.

    Returns `(run, prompts)` where `prompts` is the message list the model was
    handed on each call — so a test can assert the note was IN FRONT of the
    model rather than merely present somewhere afterwards.
    """
    turns = {"n": 0}
    prompts: list[list[dict]] = []

    async def completion(**kwargs):
        prompts.append(list(kwargs.get("messages") or []))
        turns["n"] += 1
        if turns["n"] == 1:
            return _response(tool_calls=[_tool_call("exec", {"command": "curl -s http://svc/x"})])
        index = min(turns["n"] - 2, len(answers) - 1)
        return _response(content=answers[index])

    async def execute(name, args, **kwargs):
        from robothor.engine.exec_spill import shape_exec_result

        return shape_exec_result(
            {"stdout": BIG, "stderr": "", "exit_code": 0},
            workspace=tmp_path,
            run_id="hold-run",
        )

    runner.registry.execute = AsyncMock(side_effect=execute)

    with (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=completion),
    ):
        run = await runner.execute("hold-agent", "go", agent_config=agent_config)
    return run, prompts


def _engine_notes(prompts: list[list[dict]]) -> list[str]:
    """Every `[SYSTEM]` note the model was actually HANDED, de-duplicated.

    Role-agnostic on purpose: the Anthropic leg rewrites
    `ENGINE_CONTEXT_ROLE` ("developer") to "user" on the way out
    (`llm_client._normalize_developer_role`), so keying on the role would make
    this pass or fail on the provider rather than on the control.
    """
    assert ENGINE_CONTEXT_ROLE  # the role the engine appends under; see above
    seen: list[str] = []
    for prompt in prompts:
        for message in prompt:
            content = message.get("content")
            if isinstance(content, str) and "[SYSTEM]" in content and content not in seen:
                seen.append(content)
    return seen


@pytest.mark.asyncio
@pytest.mark.usefixtures("_mock_run_persistence")
async def test_the_run_answers_again_after_the_hold(runner, agent_config, tmp_path, monkeypatch):
    """The whole of C1 in one assertion: the run's OUTPUT is the answer the
    model gave after being told, not the one it gave before."""
    monkeypatch.setenv("ROBOTHOR_TRUNCATION_LEDGER_MODE", "enforce")
    monkeypatch.setenv("ROBOTHOR_ACT_OBSERVE_MODE", "off")
    monkeypatch.setenv("ROBOTHOR_VERDICT_COMMITMENT_MODE", "off")

    run, prompts = await _drive(
        runner,
        agent_config,
        ["all 12 records processed", "I could not read the rest", "final, and honest"],
        tmp_path,
    )

    assert run.status == RunStatus.COMPLETED
    assert run.output_text == "final, and honest"
    assert run.output_text != "all 12 records processed"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_mock_run_persistence")
async def test_both_notes_were_in_front_of_the_model(runner, agent_config, tmp_path, monkeypatch):
    """Not "the engine appended it" — the model was handed it on a later call.

    Two distinct sentences: go and read it, then say what you did not read.
    """
    monkeypatch.setenv("ROBOTHOR_TRUNCATION_LEDGER_MODE", "enforce")
    monkeypatch.setenv("ROBOTHOR_ACT_OBSERVE_MODE", "off")
    monkeypatch.setenv("ROBOTHOR_VERDICT_COMMITMENT_MODE", "off")

    _run, prompts = await _drive(runner, agent_config, ["a", "b", "c"], tmp_path)
    notes = "\n".join(_engine_notes(prompts))

    assert "part of what you asked for was never shown to you" in notes
    assert "say in it what you did not read" in notes
    assert "12,431 chars" in notes


@pytest.mark.asyncio
@pytest.mark.usefixtures("_mock_run_persistence")
async def test_the_hold_is_bounded_and_the_run_terminates(
    runner, agent_config, tmp_path, monkeypatch
):
    """Driven into a run that never resolves its entry. Exactly two extra model
    turns, then it ends — a stuck run is not an improvement on a confident
    wrong answer."""
    monkeypatch.setenv("ROBOTHOR_TRUNCATION_LEDGER_MODE", "enforce")
    monkeypatch.setenv("ROBOTHOR_ACT_OBSERVE_MODE", "off")
    monkeypatch.setenv("ROBOTHOR_VERDICT_COMMITMENT_MODE", "off")

    run, prompts = await _drive(runner, agent_config, ["a"], tmp_path)

    # one tool turn + three answering turns (the first stop, then two holds)
    assert len(prompts) == 4
    assert run.status == RunStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.usefixtures("_mock_run_persistence")
async def test_observe_does_not_hold_and_does_not_change_the_answer(
    runner, agent_config, tmp_path, monkeypatch
):
    """The rung this ships on. Same run, one answering turn, and the run's
    output is the first answer — so an observe-vs-enforce sweep is measuring
    the control."""
    monkeypatch.setenv("ROBOTHOR_TRUNCATION_LEDGER_MODE", "observe")
    monkeypatch.setenv("ROBOTHOR_ACT_OBSERVE_MODE", "off")
    monkeypatch.setenv("ROBOTHOR_VERDICT_COMMITMENT_MODE", "off")

    run, prompts = await _drive(runner, agent_config, ["all 12 records processed"], tmp_path)

    assert len(prompts) == 2
    assert run.output_text == "all 12 records processed"
    assert _engine_notes(prompts) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_mock_run_persistence")
async def test_a_run_that_reads_the_spill_back_is_never_held(
    runner, agent_config, tmp_path, monkeypatch
):
    """The resolution path, end to end: the second turn reads the spill file,
    the entry clears, and the run stops on its first answer."""
    monkeypatch.setenv("ROBOTHOR_TRUNCATION_LEDGER_MODE", "enforce")
    monkeypatch.setenv("ROBOTHOR_ACT_OBSERVE_MODE", "off")
    monkeypatch.setenv("ROBOTHOR_VERDICT_COMMITMENT_MODE", "off")

    from robothor.engine.exec_spill import shape_exec_result

    shaped = shape_exec_result(
        {"stdout": BIG, "stderr": "", "exit_code": 0}, workspace=tmp_path, run_id="hold-run"
    )
    spill_path = shaped["stdout_path"]
    turns = {"n": 0}

    async def completion(**kwargs):
        turns["n"] += 1
        if turns["n"] == 1:
            return _response(tool_calls=[_tool_call("exec", {"command": "curl -s http://svc/x"})])
        if turns["n"] == 2:
            return _response(tool_calls=[_tool_call("read_file", {"path": spill_path}, "call_2")])
        return _response(content="read the whole thing")

    async def execute(name, args, **kwargs):
        if name == "read_file":
            return {"content": BIG}
        return shaped

    runner.registry.execute = AsyncMock(side_effect=execute)

    with (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=completion),
    ):
        run = await runner.execute("hold-agent", "go", agent_config=agent_config)

    assert run.output_text == "read the whole thing"
    assert turns["n"] == 3, "a run that read the spill back was held anyway"
