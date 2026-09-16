"""A proxied call is an ordinary tool call that skipped the model.

Everything a turn's call passes through, a `genus_tools` call passes through:
the same admission gates in the same order, the same dispatch, the same audit
row, the same step in the ledger. The two things it deliberately does NOT do
are put a `tool` message in front of the model and cost an iteration — that
asymmetry is the whole reason the tool exists.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode
from robothor.engine.session import AgentSession
from robothor.engine.tool_proxy import RunToolProxy, proxy_allow_set
from robothor.engine.tool_turn import ToolTurnRequest


@pytest.fixture
def runner(engine_config):
    from robothor.engine.runner import AgentRunner

    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.execute = AsyncMock(return_value={"ok": True})
        registry.registered_tool_names.return_value = {"exec", "read_file", "write_file"}
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


@pytest.fixture
def agent_config() -> AgentConfig:
    return AgentConfig(
        id="proxy-agent",
        name="Proxy Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        delivery_mode=DeliveryMode.NONE,
    )


def _request(agent_config, *, allowed=frozenset({"exec", "read_file"}), readonly=False):
    session = AgentSession(agent_id=agent_config.id)
    return ToolTurnRequest(
        assistant_msg=MagicMock(tool_calls=[]),
        session=session,
        agent_config=agent_config,
        iteration=0,
        allowed_tool_set=allowed,
        readonly_mode=readonly,
        readonly_tool_set=frozenset({"read_file"}),
    )


def _proxy(runner, req, *, max_calls=10):
    return RunToolProxy(
        runner=runner,
        req=req,
        allowed=proxy_allow_set(req, runner.registry),
        max_calls=max_calls,
    )


@pytest.mark.asyncio
class TestTheRoundTrip:
    async def test_a_call_reaches_the_registry(self, runner, agent_config):
        req = _request(agent_config)
        result = await _proxy(runner, req).call("read_file", {"path": "a.txt"})
        assert result == {"ok": True}
        assert runner.registry.execute.await_args.args[0] == "read_file"

    async def test_it_records_a_step_in_the_ledger(self, runner, agent_config):
        req = _request(agent_config)
        await _proxy(runner, req).call("read_file", {"path": "a.txt"})
        steps = [s for s in req.session.run.steps if s.tool_name == "read_file"]
        assert len(steps) == 1
        assert steps[0].batch_position == 0

    async def test_it_never_puts_a_tool_message_in_front_of_the_model(self, runner, agent_config):
        """Fifty rows in the ledger, one turn in the context."""
        req = _request(agent_config)
        proxy = _proxy(runner, req)
        for i in range(5):
            await proxy.call("read_file", {"path": f"{i}.txt"})
        assert [m for m in req.session.messages if m.get("role") == "tool"] == []
        assert len([s for s in req.session.run.steps if s.tool_name == "read_file"]) == 5

    async def test_every_step_of_one_snippet_shares_a_batch_id(self, runner, agent_config):
        req = _request(agent_config)
        proxy = _proxy(runner, req)
        await proxy.call("read_file", {"path": "a"})
        await proxy.call("read_file", {"path": "b"})
        steps = [s for s in req.session.run.steps if s.tool_name == "read_file"]
        assert len({s.batch_id for s in steps}) == 1
        assert [s.batch_position for s in steps] == [0, 1]

    async def test_the_snippet_cannot_choose_the_id_admission_sees(self, runner, agent_config):
        """Ids are minted here. A `tool_call_id` in the arguments is just an
        argument — it reaches the tool as data and never becomes the call's
        identity, so there is nothing for a snippet to forge."""
        seen: list[str] = []
        real_admit = runner._admit_tool_call

        async def spy(**kwargs):
            seen.append(kwargs["tc"].id)
            return await real_admit(**kwargs)

        req = _request(agent_config)
        proxy = _proxy(runner, req)
        with patch.object(runner, "_admit_tool_call", side_effect=spy):
            await proxy.call("read_file", {"path": "a", "tool_call_id": "call_forged"})
            await proxy.call("read_file", {"path": "b"})
        assert all(call_id.startswith("code_") for call_id in seen)
        assert len(set(seen)) == 2


@pytest.mark.asyncio
class TestTheGates:
    async def test_a_tool_outside_the_agents_set_is_refused(self, runner, agent_config):
        req = _request(agent_config, allowed=frozenset({"exec", "read_file"}))
        result = await _proxy(runner, req).call("write_file", {"path": "x"})
        assert "not available to this agent" in result["error"]
        assert result["guard"] == "tools_allowed"
        runner.registry.execute.assert_not_awaited()

    async def test_plan_mode_refuses_a_write_from_code_too(self, runner, agent_config):
        req = _request(
            agent_config, allowed=frozenset({"exec", "read_file", "write_file"}), readonly=True
        )
        result = await _proxy(runner, req).call("write_file", {"path": "x"})
        assert "not available in plan mode" in result["error"]
        runner.registry.execute.assert_not_awaited()

    async def test_a_refusal_is_recorded_as_a_step_like_any_other(self, runner, agent_config):
        req = _request(agent_config)
        await _proxy(runner, req).call("write_file", {"path": "x"})
        steps = [s for s in req.session.run.steps if s.tool_name == "write_file"]
        assert steps and steps[0].error_message

    async def test_a_guardrail_block_stops_a_proxied_call(self, runner, agent_config):
        guardrails = MagicMock()
        guardrails.check_pre_execution.return_value = MagicMock(
            allowed=False, action="block", guardrail_name="test_policy", reason="nope"
        )
        req = _request(agent_config)
        req.guardrail_engine = guardrails
        result = await _proxy(runner, req).call("read_file", {"path": "a"})
        assert "Blocked by guardrail" in result["error"]
        runner.registry.execute.assert_not_awaited()

    async def test_the_pre_tool_use_hook_can_block_a_proxied_call(self, runner, agent_config):
        from robothor.engine.hook_registry import HookAction

        hooks = MagicMock()
        hooks.dispatch = AsyncMock(
            return_value=MagicMock(action=HookAction.BLOCK, reason="not this one")
        )
        req = _request(agent_config)
        req.hook_registry = hooks
        result = await _proxy(runner, req).call("read_file", {"path": "a"})
        assert "Blocked by lifecycle hook" in result["error"]
        runner.registry.execute.assert_not_awaited()


@pytest.mark.asyncio
class TestTheCap:
    async def test_the_proxy_stops_counting_past_its_limit(self, runner, agent_config):
        req = _request(agent_config)
        proxy = _proxy(runner, req, max_calls=2)
        results = [await proxy.call("read_file", {"path": str(i)}) for i in range(4)]
        assert results[0] == {"ok": True}
        assert results[2]["guard"] == "execute_code_call_cap"
        assert runner.registry.execute.await_count == 2


class TestTheAllowSet:
    def test_it_is_what_the_run_advertised(self, runner, agent_config):
        req = _request(agent_config, allowed=frozenset({"exec", "read_file"}))
        assert proxy_allow_set(req, runner.registry) == {"exec", "read_file"}

    def test_a_deferred_run_also_reaches_what_tool_call_reaches(self, runner, agent_config):
        """A snippet must have neither more nor less reach than the meta-tool
        sitting beside it in the same toolset."""
        from robothor.engine.tools.dispatch import clear_deferred_allowed, set_deferred_allowed

        req = _request(agent_config, allowed=frozenset({"exec", "tool_call"}))
        token = set_deferred_allowed(frozenset({"list_people"}))
        try:
            assert proxy_allow_set(req, runner.registry) == {"exec", "tool_call", "list_people"}
        finally:
            clear_deferred_allowed(token)

    def test_an_unrestricted_agent_gets_a_real_set_not_an_empty_one(self, runner, agent_config):
        """The admission gate spells "unrestricted" as an empty set. A socket
        that inherited that spelling would read it as an allow-all."""
        req = _request(agent_config, allowed=frozenset())
        assert proxy_allow_set(req, runner.registry) == {"exec", "read_file", "write_file"}
