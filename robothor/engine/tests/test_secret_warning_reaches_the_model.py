"""A detected credential has to reach the agent, not just the journal.

`no_sensitive_data` scans every tool result for credentials and it works. What
happened next was:

    logger.warning("Guardrail warning for %s: %s", tool_name, post_gr.reason)

A line in a log nobody reads mid-run. The model was never told. So an agent
could read a file containing an API key, have the platform notice, and carry
on entirely oblivious — which is exactly what it did on WildClawBench's
`leaked_api` task on 2026-08-24: it inspected the repository, never mentioned
the credential sitting in `agent.py`, and scored zero. The detector was fine.
The delivery did not exist.

Detection that reaches no one is the same shape as a control that never runs.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode
from robothor.engine.runner import AgentRunner

FAKE_KEY = "sk-proj-" + "A1b2C3d4E5f6G7h8i9J0"


def _tool_call(name: str, args: dict, call_id: str = "c1"):
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    return tc


def _response(content=None, tool_calls=None):
    r = MagicMock()
    r.model = "test-model"
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    r.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    r.usage = usage
    return r


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry") as reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "read_file"}}
        ]
        registry.get_tool_names.return_value = ["read_file"]
        reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


@pytest.fixture
def agent_config() -> AgentConfig:
    return AgentConfig(
        id="secret-agent",
        name="Secret Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        timeout_seconds=0,
        stall_timeout_seconds=0,
        delivery_mode=DeliveryMode.NONE,
        planning_enabled=False,
        scratchpad_enabled=False,
        tools_allowed=["read_file"],
    )


async def _run_reading_a_file(runner, agent_config, file_content: str):
    """One turn that reads a file, then a plain answer."""
    calls = {"n": 0}

    async def completion(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _response(tool_calls=[_tool_call("read_file", {"path": "/tmp/agent.py"})])
        return _response(content="done")

    runner.registry.execute = AsyncMock(return_value={"content": file_content})

    captured: list = []
    from robothor.engine import session_registry

    original = session_registry.register

    def _capture(session):
        captured.append(session)
        return original(session)

    session_registry.register = _capture
    try:
        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.run_finalizer.create_step"),
            patch("litellm.acompletion", side_effect=completion),
        ):
            await runner.execute("secret-agent", "look at the file", agent_config=agent_config)
    finally:
        session_registry.register = original
    return captured[0] if captured else None


def _engine_notes(session) -> list[str]:
    from robothor.engine.runner import ENGINE_CONTEXT_ROLE

    return [
        str(m.get("content", "")) for m in session.messages if m.get("role") == ENGINE_CONTEXT_ROLE
    ]


class TestTheAgentIsTold:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_credential_in_tool_output_produces_a_note_to_the_model(
        self, runner, agent_config
    ):
        session = await _run_reading_a_file(runner, agent_config, f'API_KEY = "{FAKE_KEY}"\n')
        notes = " ".join(_engine_notes(session)).lower()
        assert "credential" in notes or "secret" in notes, (
            "the platform detected a credential and told only the log"
        )

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_the_note_does_not_repeat_the_secret(self, runner, agent_config):
        """The warning must not be the leak. It goes into the conversation,
        which is persisted, replayed, and — on a benchmark — graded for
        whether the value was reproduced."""
        session = await _run_reading_a_file(runner, agent_config, f'API_KEY = "{FAKE_KEY}"\n')
        notes = " ".join(_engine_notes(session))
        assert FAKE_KEY not in notes

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_the_note_says_what_to_do_about_it(self, runner, agent_config):
        session = await _run_reading_a_file(runner, agent_config, f'API_KEY = "{FAKE_KEY}"\n')
        notes = " ".join(_engine_notes(session)).lower()
        assert "tell the user" in notes or "report" in notes

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_ordinary_output_produces_no_note(self, runner, agent_config):
        """A warning on every file read is a warning nobody reads."""
        session = await _run_reading_a_file(runner, agent_config, "def main():\n    pass\n")
        notes = " ".join(_engine_notes(session)).lower()
        assert "credential" not in notes and "secret" not in notes

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_the_agent_is_told_once_not_on_every_turn(self, runner, agent_config):
        """Repeating it each iteration would crowd out the task itself."""
        session = await _run_reading_a_file(runner, agent_config, f'K = "{FAKE_KEY}"\n')
        matching = [n for n in _engine_notes(session) if "credential" in n.lower()]
        assert len(matching) == 1
