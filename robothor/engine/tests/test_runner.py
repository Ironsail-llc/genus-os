"""Tests for the AgentRunner — core LLM conversation loop."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import RunStatus, TriggerType
from robothor.engine.runner import AgentRunner
from robothor.identity import IdentityContext


@pytest.fixture
def runner(engine_config):
    """Create an AgentRunner with mocked dependencies."""
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        mock_registry = MagicMock()
        mock_registry.build_for_agent.return_value = []
        mock_registry.get_tool_names.return_value = []
        mock_reg.return_value = mock_registry
        r = AgentRunner(engine_config)
        r.registry = mock_registry
        yield r


class TestAgentRunnerExecute:
    @pytest.mark.asyncio
    async def test_missing_agent_config(self, runner):
        """Agent run fails gracefully when config not found."""
        with patch("robothor.engine.runner.load_agent_config", return_value=None):
            with patch("robothor.engine.runner.create_run"):
                run = await runner.execute("nonexistent", "test message")
        assert run.status == RunStatus.FAILED
        assert "not found" in run.error_message

    @pytest.mark.asyncio
    async def test_no_models_configured(self, runner, sample_agent_config, mock_litellm_response):
        """A missing model declaration uses the availability fallback."""
        sample_agent_config.model_primary = ""
        sample_agent_config.model_fallbacks = []
        response = mock_litellm_response(content="Fallback completed")
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response) as call:
            with patch("robothor.engine.runner.create_run"):
                with patch("robothor.engine.runner.update_run"):
                    with patch("robothor.engine.runner.create_step"):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )
        assert run.status == RunStatus.COMPLETED
        assert call.call_args.kwargs["model"] == "openrouter/deepseek/deepseek-v4-pro"

    @pytest.mark.asyncio
    async def test_successful_simple_run(self, runner, sample_agent_config, mock_litellm_response):
        """Agent completes when LLM returns text without tool calls."""
        response = mock_litellm_response(content="Hello! I'm done.")

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion", new_callable=AsyncMock, return_value=response
                    ):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "Hello! I'm done."
        assert run.model_used == "test-model"
        llm_steps = [step for step in run.steps if step.step_type.value == "llm_call"]
        assert len(llm_steps) == 1
        assert llm_steps[0].model == "test-model"

    @pytest.mark.asyncio
    async def test_tool_call_loop(self, runner, sample_agent_config, mock_litellm_response):
        """Agent executes tool calls and continues the loop."""
        # First response: tool call
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = json.dumps({"status": "TODO"})

        response1 = mock_litellm_response(content=None, tool_calls=[tc])
        response1.choices[0].message.content = None

        # Second response: final text
        response2 = mock_litellm_response(content="Found 3 tasks.")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        runner.registry.execute = AsyncMock(return_value={"tasks": [], "count": 0})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "List my tasks",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "Found 3 tasks."
        assert len(run.steps) >= 3  # llm_call + tool_call + llm_call

    @pytest.mark.asyncio
    async def test_empty_choices_guard(self, runner, sample_agent_config):
        """Run fails when LLM returns empty choices list."""
        response = MagicMock()
        response.model = "test-model"
        response.choices = []  # empty choices
        response.usage = MagicMock(prompt_tokens=10, completion_tokens=0)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion", new_callable=AsyncMock, return_value=response
                    ):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.FAILED
        assert "empty choices" in (run.error_message or "")

    @pytest.mark.asyncio
    async def test_conversation_history_passed(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Conversation history is passed through to the session."""
        response = mock_litellm_response(content="I remember!")
        history = [
            {"role": "user", "content": "First message"},
            {"role": "assistant", "content": "First reply"},
        ]

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion", new_callable=AsyncMock, return_value=response
                    ) as mock_llm:
                        run = await runner.execute(
                            "test-agent",
                            "Follow-up",
                            agent_config=sample_agent_config,
                            conversation_history=history,
                        )

        assert run.status == RunStatus.COMPLETED
        # Verify history was included in messages sent to LLM
        call_args = mock_llm.call_args
        messages = call_args.kwargs["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "First message"
        assert messages[2]["role"] == "assistant"
        assert messages[2]["content"] == "First reply"
        assert messages[3]["role"] == "user"
        assert messages[3]["content"] == "Follow-up"

    @pytest.mark.asyncio
    async def test_all_models_fail(self, runner, sample_agent_config):
        """Run fails when all models error."""

        async def mock_fail(**kwargs):
            raise Exception("Model unavailable")

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_fail):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.FAILED
        assert "All models failed" in (run.error_message or "")

    @pytest.mark.asyncio
    async def test_timeout(self, runner, sample_agent_config, mock_litellm_response):
        """Hard timeout fires when stall watchdog is disabled."""
        import asyncio

        sample_agent_config.timeout_seconds = 1  # 1 second hard timeout
        sample_agent_config.stall_timeout_seconds = 0  # disable watchdog → hard timeout active

        async def slow_completion(**kwargs):
            await asyncio.sleep(5)  # Will be cancelled by timeout
            return mock_litellm_response()

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=slow_completion):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_model_fallback(self, runner, sample_agent_config, mock_litellm_response):
        """Falls back to next model when primary fails."""
        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("model") == "openrouter/test/model":
                raise Exception("Primary model down")
            return mock_litellm_response(
                content="Fallback worked", model="openrouter/test/fallback"
            )

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.COMPLETED
        assert run.model_used == "openrouter/test/fallback"
        assert len(run.models_attempted) >= 1

    @pytest.mark.asyncio
    async def test_hung_primary_falls_back_within_request_timeout(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """When the primary LLM hangs longer than LLM_REQUEST_TIMEOUT, the
        runner must cancel that call and fall through to the next model.

        Regression for the 2026-05-28 incident where main's worker timed out
        for 1800s because litellm.acompletion silently ignored its `timeout`
        kwarg and hung indefinitely after the codex/gpt-5.5 model_fallback
        step. The fix wraps each per-model awaitable in an asyncio.timeout
        the runner enforces directly (independent of litellm).
        """
        import asyncio as _asyncio
        import time

        # Squeeze the per-LLM-call timeout so the test runs in ~1s. The
        # primary "hangs" for 5s, the fallback completes immediately.
        call_count = 0
        observed_primary_wait: list[float] = []

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("model") == "openrouter/test/model":
                start = time.monotonic()
                try:
                    await _asyncio.sleep(30)  # would hang forever absent a timeout
                finally:
                    observed_primary_wait.append(time.monotonic() - start)
                return mock_litellm_response()  # never reached
            return mock_litellm_response(content="fallback ok", model="openrouter/test/fallback")

        with (
            patch("robothor.engine.llm_client.LLM_REQUEST_TIMEOUT", 1),
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_step"),
            patch("litellm.acompletion", side_effect=mock_completion),
        ):
            t0 = time.monotonic()
            run = await runner.execute("test-agent", "hello", agent_config=sample_agent_config)
            elapsed = time.monotonic() - t0

        assert run.status == RunStatus.COMPLETED, run.error_message
        assert run.model_used == "openrouter/test/fallback"
        # The hung primary should have been cancelled at or near the
        # 1-second LLM_REQUEST_TIMEOUT — not allowed to run the full 30s.
        assert observed_primary_wait, "primary was never invoked"
        assert max(observed_primary_wait) < 5, (
            f"primary hung for {observed_primary_wait}s — runner did not "
            f"enforce LLM_REQUEST_TIMEOUT (=1s)"
        )
        assert elapsed < 10, f"overall run took {elapsed}s — fallback too slow"

    @pytest.mark.asyncio
    async def test_trigger_type_preserved(self, runner, sample_agent_config, mock_litellm_response):
        """Trigger type and detail are preserved in the run."""
        response = mock_litellm_response(content="Done")

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion", new_callable=AsyncMock, return_value=response
                    ):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            trigger_type=TriggerType.CRON,
                            trigger_detail="0 * * * *",
                            agent_config=sample_agent_config,
                        )

        assert run.trigger_type == TriggerType.CRON
        assert run.trigger_detail == "0 * * * *"


class TestIdentityThreading:
    """Task 2 — IdentityContext threaded through execute().

    Precedence: explicit `identity` kwarg > webchat DB resolution > legacy
    Telegram `|sender:` parse. Effective identity feeds both the CURRENT USER
    prompt block (warmup on first turn, mini-preamble on follow-ups) and
    `run.person_id` attribution (ahead of the existing resolve_run_person_id
    fallback).
    """

    @pytest.mark.asyncio
    async def test_explicit_identity_passed_to_warmup(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Explicit `identity=` reaches build_interactive_preamble on a
        first-turn (no history) interactive run."""
        identity = IdentityContext(
            tenant_id="t-alpha",
            channel="webchat",
            identifier="acct-1",
            verified=True,
            display_name="Alice",
            role="owner",
        )
        response = mock_litellm_response(content="Hi Alice")

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_step"),
            patch(
                "litellm.acompletion", new_callable=AsyncMock, return_value=response
            ),
            patch(
                "robothor.engine.warmup.build_interactive_preamble", return_value=""
            ) as mock_warmup,
        ):
            run = await runner.execute(
                "test-agent",
                "hello",
                agent_config=sample_agent_config,
                trigger_type=TriggerType.WEBCHAT,
                tenant_id="t-alpha",
                user_id="u1",
                user_role="owner",
                identity=identity,
            )

        assert run.status == RunStatus.COMPLETED
        assert mock_warmup.call_args.kwargs["identity"] is identity

    @pytest.mark.asyncio
    async def test_person_id_set_from_identity_before_fallback(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """run.person_id is stamped from identity.person_id, and the
        resolve_run_person_id fallback is skipped entirely."""
        identity = IdentityContext(
            tenant_id="t-alpha",
            channel="webchat",
            identifier="acct-1",
            verified=True,
            display_name="Alice",
            person_id="person-99",
        )
        response = mock_litellm_response(content="Hi")

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_step"),
            patch(
                "litellm.acompletion", new_callable=AsyncMock, return_value=response
            ),
            patch("robothor.engine.run_person_link.resolve_run_person_id") as mock_resolve,
        ):
            run = await runner.execute(
                "test-agent",
                "hello",
                agent_config=sample_agent_config,
                trigger_type=TriggerType.WEBCHAT,
                tenant_id="t-alpha",
                user_id="u1",
                user_role="owner",
                identity=identity,
            )

        assert run.person_id == "person-99"
        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_webchat_resolves_identity_via_resolve_identity(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """No explicit identity + WEBCHAT trigger resolves via resolve_identity."""
        resolved = IdentityContext(
            tenant_id="t-alpha",
            channel="webchat",
            identifier="u1",
            verified=True,
            display_name="Alice",
            person_id="person-7",
        )
        response = mock_litellm_response(content="Hi")

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_step"),
            patch(
                "litellm.acompletion", new_callable=AsyncMock, return_value=response
            ),
            patch("robothor.identity.resolve_identity", return_value=resolved) as mock_resolve,
        ):
            run = await runner.execute(
                "test-agent",
                "hello",
                agent_config=sample_agent_config,
                trigger_type=TriggerType.WEBCHAT,
                tenant_id="t-alpha",
                user_id="u1",
                user_role="owner",
            )

        mock_resolve.assert_called_once_with("webchat", "u1", "t-alpha")
        assert run.person_id == "person-7"

    @pytest.mark.asyncio
    async def test_telegram_legacy_sender_parse_fallback_on_followup(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """No explicit identity + Telegram `|sender:` trigger_detail still
        produces a CURRENT USER mini-preamble on a follow-up turn."""
        response = mock_litellm_response(content="Hi Bob")
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        captured: dict[str, Any] = {}

        async def mock_completion(**kwargs):
            # Snapshot now — the runner mutates this same list object in
            # place (appends the assistant reply) after the call returns.
            captured["last_user_msg"] = kwargs["messages"][-1]["content"]
            return response

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_step"),
            patch("robothor.identity.enrich_identity", return_value=None),
            patch("litellm.acompletion", side_effect=mock_completion),
        ):
            run = await runner.execute(
                "test-agent",
                "follow-up",
                agent_config=sample_agent_config,
                trigger_type=TriggerType.TELEGRAM,
                trigger_detail="chat:123|sender:Bob",
                conversation_history=history,
                tenant_id="t-alpha",
                user_id="tg-1",
                user_role="member",
            )

        assert run.status == RunStatus.COMPLETED
        last_user_msg = captured["last_user_msg"]
        assert "--- CURRENT USER ---" in last_user_msg
        assert "Bob" in last_user_msg
        assert "Verified: yes" in last_user_msg

    @pytest.mark.asyncio
    async def test_no_duplicate_block_on_first_turn(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """First turn (no history): CURRENT USER appears exactly once, from
        warmup — the mini-preamble path must not also fire."""
        identity = IdentityContext(
            tenant_id="t-alpha",
            channel="webchat",
            identifier="acct-1",
            verified=True,
            display_name="Alice",
        )
        response = mock_litellm_response(content="Hi Alice")
        captured: dict[str, Any] = {}

        async def mock_completion(**kwargs):
            captured["last_user_msg"] = kwargs["messages"][-1]["content"]
            return response

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_step"),
            patch("robothor.identity.enrich_identity", return_value=None),
            patch("robothor.memory.blocks.read_block", return_value=None),
            patch("litellm.acompletion", side_effect=mock_completion),
        ):
            run = await runner.execute(
                "test-agent",
                "hello",
                agent_config=sample_agent_config,
                trigger_type=TriggerType.WEBCHAT,
                tenant_id="t-alpha",
                user_id="u1",
                user_role="owner",
                identity=identity,
            )

        assert run.status == RunStatus.COMPLETED
        last_user_msg = captured["last_user_msg"]
        assert last_user_msg.count("--- CURRENT USER ---") == 1

    @pytest.mark.asyncio
    async def test_no_identity_no_block_for_manual_trigger(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Non-interactive (manual/cron) triggers are untouched — no identity
        resolution attempted, no CURRENT USER block."""
        response = mock_litellm_response(content="Hi")

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_step"),
            patch(
                "litellm.acompletion", new_callable=AsyncMock, return_value=response
            ) as mock_llm,
        ):
            run = await runner.execute(
                "test-agent",
                "hello",
                agent_config=sample_agent_config,
                conversation_history=[{"role": "user", "content": "hi"}],
            )

        assert run.status == RunStatus.COMPLETED
        messages = mock_llm.call_args.kwargs["messages"]
        last_user_msg = messages[-1]["content"]
        assert "CURRENT USER" not in last_user_msg

    @pytest.mark.asyncio
    async def test_child_spawn_context_carries_identity(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """A top-level run that can spawn children stashes its effective
        identity on the fresh SpawnContext so children inherit it for
        person_id/user_id attribution (children's own prompts don't render
        the block — their trigger_type is SUB_AGENT, never interactive)."""
        from robothor.engine.tools import _current_spawn_context

        sample_agent_config.can_spawn_agents = True
        identity = IdentityContext(
            tenant_id="t-alpha",
            channel="webchat",
            identifier="acct-1",
            verified=True,
            display_name="Alice",
        )
        response = mock_litellm_response(content="done")

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_step"),
            patch(
                "litellm.acompletion", new_callable=AsyncMock, return_value=response
            ),
        ):
            run = await runner.execute(
                "test-agent",
                "hello",
                agent_config=sample_agent_config,
                trigger_type=TriggerType.WEBCHAT,
                tenant_id="t-alpha",
                user_id="u1",
                user_role="owner",
                identity=identity,
            )

        assert run.status == RunStatus.COMPLETED
        ctx = _current_spawn_context.get()
        assert ctx is not None
        assert ctx.identity is identity


class TestBrokenModelTracking:
    """Tests for rate-limited / permanently-failed model tracking."""

    @pytest.mark.asyncio
    async def test_rate_limited_model_skipped_on_subsequent_iterations(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """A model that returns 403 is skipped on the next iteration."""
        sample_agent_config.model_primary = "model-a"
        sample_agent_config.model_fallbacks = ["model-b"]

        # Track which models are actually called
        models_called: list[str] = []
        call_count = 0

        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        async def mock_completion(**kwargs):
            nonlocal call_count
            model = kwargs["model"]
            models_called.append(model)
            call_count += 1

            if model == "model-a":
                err = Exception("Rate limited")
                err.status_code = 403  # type: ignore[attr-defined]
                raise err

            # model-b succeeds
            if call_count <= 2:
                # First call: return tool call to force a second iteration
                resp = mock_litellm_response(content=None, tool_calls=[tc], model="model-b")
                resp.choices[0].message.content = None
                return resp
            return mock_litellm_response(content="Done", model="model-b")

        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.COMPLETED
        # model-a should only be tried once (iteration 1), then skipped
        assert models_called.count("model-a") == 1
        # model-b handles both iterations
        assert models_called.count("model-b") >= 2

    @pytest.mark.asyncio
    async def test_all_models_broken_immediate_failure(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """When all models hit permanent errors, run fails without retrying."""
        sample_agent_config.model_primary = "model-a"
        sample_agent_config.model_fallbacks = ["model-b"]
        sample_agent_config.max_iterations = 10

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            err = Exception("Forbidden")
            err.status_code = 403  # type: ignore[attr-defined]
            raise err

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.FAILED
        assert "All models failed" in (run.error_message or "")
        # Should only try each model once — NOT 10 iterations x 2 models = 20
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_safety_cap_stops_runaway_loop(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Safety cap stops infinite loops and forces a wrap-up summary."""
        sample_agent_config.max_iterations = 3  # check-in interval
        sample_agent_config.safety_cap = 5  # hard safety valve

        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        # Always return tool calls so the loop keeps going (simulates runaway)
        async def mock_completion(**kwargs):
            resp = mock_litellm_response(content=None, tool_calls=[tc])
            resp.choices[0].message.content = None
            return resp

        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        llm_call_count = 0

        async def counting_mock(**kwargs):
            nonlocal llm_call_count
            llm_call_count += 1
            return await mock_completion(**kwargs)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=counting_mock):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        # 5 iterations of tool calls + 1 wrap-up call = 6 total LLM calls
        assert llm_call_count == 6
        # Safety limit error is recorded
        error_steps = [
            s for s in run.steps if s.error_message and "Safety limit" in s.error_message
        ]
        assert len(error_steps) == 1

    @pytest.mark.asyncio
    async def test_checkin_message_injected_at_interval(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Check-in messages are injected every max_iterations iterations."""
        sample_agent_config.max_iterations = 2  # check-in every 2 iterations
        sample_agent_config.safety_cap = 10

        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        call_count = 0
        captured_messages = []

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            # Capture messages to check for check-in injections
            if "messages" in kwargs:
                captured_messages.append(list(kwargs["messages"]))
            # Stop after 5 iterations by returning no tool calls
            if call_count >= 5:
                return mock_litellm_response(content="Done")
            resp = mock_litellm_response(content=None, tool_calls=[tc])
            resp.choices[0].message.content = None
            return resp

        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        # Check that check-in messages were injected at iterations 2 and 4
        all_messages = [m for msgs in captured_messages for m in msgs]
        checkin_messages = [
            m
            for m in all_messages
            if m.get("role") == "developer" and "Progress check-in" in m.get("content", "")
        ]
        assert len(checkin_messages) >= 2

    @pytest.mark.asyncio
    async def test_budget_exhausted_does_not_stop_loop(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Token budget exhaustion is tracked but does not stop the run."""
        sample_agent_config.max_iterations = 50
        sample_agent_config.safety_cap = 200

        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            # Stop after 3 iterations
            if call_count >= 3:
                return mock_litellm_response(content="Done")
            resp = mock_litellm_response(content=None, tool_calls=[tc])
            resp.choices[0].message.content = None
            # Simulate high token usage
            resp.usage = MagicMock()
            resp.usage.prompt_tokens = 50000
            resp.usage.completion_tokens = 5000
            resp.usage.total_tokens = 55000
            return resp

        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        with patch(
                            "robothor.engine.model_registry.compute_token_budget",
                            return_value=10000,
                        ):
                            run = await runner.execute(
                                "test-agent",
                                "hello",
                                agent_config=sample_agent_config,
                            )

        # Run completed normally (3 calls) — budget did NOT cut it short
        assert call_count == 3
        assert run.output_text == "Done"


class TestOnToolCallback:
    """Tests for the on_tool callback in tool execution."""

    @pytest.mark.asyncio
    async def test_on_tool_receives_start_and_end(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """on_tool callback fires for tool_start and tool_end events."""
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = json.dumps({"status": "TODO"})

        response1 = mock_litellm_response(content=None, tool_calls=[tc])
        response1.choices[0].message.content = None
        response2 = mock_litellm_response(content="Done.")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        runner.registry.execute = AsyncMock(return_value={"tasks": [], "count": 0})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        tool_events: list[dict] = []

        async def on_tool(event: dict) -> None:
            tool_events.append(event)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                            on_tool=on_tool,
                        )

        assert run.status == RunStatus.COMPLETED
        assert len(tool_events) == 2

        # Verify tool_start event
        start_evt = tool_events[0]
        assert start_evt["event"] == "tool_start"
        assert start_evt["tool"] == "list_tasks"
        assert start_evt["call_id"] == "call_1"
        assert start_evt["args"] == {"status": "TODO"}

        # Verify tool_end event
        end_evt = tool_events[1]
        assert end_evt["event"] == "tool_end"
        assert end_evt["tool"] == "list_tasks"
        assert end_evt["call_id"] == "call_1"
        assert end_evt["duration_ms"] >= 0
        assert end_evt["error"] is None
        assert "result_preview" in end_evt

    @pytest.mark.asyncio
    async def test_on_tool_errors_are_swallowed(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Errors in on_tool callback must never block tool execution."""
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        response1 = mock_litellm_response(content=None, tool_calls=[tc])
        response1.choices[0].message.content = None
        response2 = mock_litellm_response(content="Done.")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        async def failing_on_tool(event: dict) -> None:
            raise RuntimeError("Callback exploded!")

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                            on_tool=failing_on_tool,
                        )

        # Run should still complete despite callback errors
        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "Done."

    @pytest.mark.asyncio
    async def test_on_tool_works_alongside_on_content(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """on_tool and on_content can both be provided (non-streaming path)."""
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        response1 = mock_litellm_response(content=None, tool_calls=[tc])
        response1.choices[0].message.content = None
        response2 = mock_litellm_response(content="Done.")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        tool_events: list[dict] = []

        async def on_tool(event: dict) -> None:
            tool_events.append(event)

        # Test both params accepted — use _run_loop directly to avoid
        # streaming path which needs a real async iterator mock
        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        # No on_content to avoid streaming path; just verify
                        # on_tool param is accepted alongside on_content signature
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                            on_tool=on_tool,
                        )

        assert run.status == RunStatus.COMPLETED
        # Tool events should have fired
        assert len(tool_events) == 2

    @pytest.mark.asyncio
    async def test_on_tool_result_preview_truncated(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Result preview in tool_end is truncated to 2000 chars."""
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "search_memory"
        tc.function.arguments = "{}"

        response1 = mock_litellm_response(content=None, tool_calls=[tc])
        response1.choices[0].message.content = None
        response2 = mock_litellm_response(content="Done.")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        # Return a very large result
        large_result = {"data": "x" * 5000}
        runner.registry.execute = AsyncMock(return_value=large_result)
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "search_memory"}}
        ]
        runner.registry.get_tool_names.return_value = ["search_memory"]

        tool_events: list[dict] = []

        async def on_tool(event: dict) -> None:
            tool_events.append(event)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                            on_tool=on_tool,
                        )

        # Find the tool_end event
        end_events = [e for e in tool_events if e["event"] == "tool_end"]
        assert len(end_events) == 1
        # Preview should be truncated
        preview = end_events[0]["result_preview"]
        assert len(preview) <= 2003 + 1  # 2000 chars + "..."


class TestThinkingAPI:
    """Tests for the adaptive thinking API integration."""

    @pytest.mark.asyncio
    async def test_thinking_sets_temperature_1(self, runner, sample_agent_config):
        """When thinking is enabled, temperature MUST be 1.0 (Anthropic requirement)."""
        # Use a thinking-capable model
        sample_agent_config.model_primary = "openrouter/anthropic/claude-sonnet-4.6"
        sample_agent_config.model_fallbacks = []

        response = MagicMock()
        response.model = "anthropic/claude-sonnet-4.6"
        response.choices = [MagicMock()]
        response.choices[0].message.content = "Thought about it."
        response.choices[0].message.tool_calls = None
        response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion", new_callable=AsyncMock, return_value=response
                    ) as mock_llm:
                        await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        call_kwargs = mock_llm.call_args.kwargs
        assert call_kwargs["temperature"] == 1.0
        assert call_kwargs["thinking"]["type"] == "enabled"
        assert call_kwargs["thinking"]["budget_tokens"] == 10_000
        # Model should stay as OpenRouter path (no prefix stripping)
        assert call_kwargs["model"] == "openrouter/anthropic/claude-sonnet-4.6"

    @pytest.mark.asyncio
    async def test_thinking_blocks_filtered_from_output(self, runner, sample_agent_config):
        """Thinking blocks in response content should be filtered from output text."""
        sample_agent_config.model_primary = "openrouter/anthropic/claude-sonnet-4.6"
        sample_agent_config.model_fallbacks = []

        response = MagicMock()
        response.model = "anthropic/claude-sonnet-4.6"
        response.choices = [MagicMock()]
        # Simulate content blocks with thinking + text
        response.choices[0].message.content = [
            {"type": "thinking", "thinking": "Let me think about this..."},
            {"type": "text", "text": "Here is my answer."},
        ]
        response.choices[0].message.tool_calls = None
        response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion", new_callable=AsyncMock, return_value=response
                    ):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "Here is my answer."

    @pytest.mark.asyncio
    async def test_non_thinking_model_no_thinking_param(self, runner, sample_agent_config):
        """Non-thinking models should not get thinking parameter."""
        sample_agent_config.model_primary = "openrouter/xiaomi/mimo-v2-pro"
        sample_agent_config.model_fallbacks = []

        response = MagicMock()
        response.model = "openrouter/xiaomi/mimo-v2-pro"
        response.choices = [MagicMock()]
        response.choices[0].message.content = "Hello!"
        response.choices[0].message.tool_calls = None
        response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion", new_callable=AsyncMock, return_value=response
                    ) as mock_llm:
                        await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        call_kwargs = mock_llm.call_args.kwargs
        assert "thinking" not in call_kwargs
        assert call_kwargs["temperature"] == 0.3  # default, not forced to 1.0


class TestShouldVerify:
    """Tests for _should_verify logic."""

    def test_explicit_verification_enabled(self, runner, sample_agent_config):
        """verification_enabled=True always returns True."""
        sample_agent_config.verification_enabled = True
        assert runner._should_verify(sample_agent_config, None) is True

    def test_route_verification_true(self, runner, sample_agent_config):
        route = MagicMock()
        route.verification = True
        assert runner._should_verify(sample_agent_config, route) is True

    def test_route_verification_false(self, runner, sample_agent_config):
        route = MagicMock()
        route.verification = False
        assert runner._should_verify(sample_agent_config, route) is False

    def test_skip_for_telegram_trigger(self, runner, sample_agent_config):
        """Verification should be skipped for interactive Telegram sessions."""
        from robothor.engine.session import AgentSession

        session = AgentSession("test-agent", trigger_type=TriggerType.TELEGRAM)
        route = MagicMock()
        route.verification = True
        assert runner._should_verify(sample_agent_config, route, session) is False

    def test_skip_for_webchat_trigger(self, runner, sample_agent_config):
        """Verification should be skipped for interactive webchat sessions."""
        from robothor.engine.session import AgentSession

        session = AgentSession("test-agent", trigger_type=TriggerType.WEBCHAT)
        route = MagicMock()
        route.verification = True
        assert runner._should_verify(sample_agent_config, route, session) is False

    def test_cron_trigger_allows_verification(self, runner, sample_agent_config):
        """Cron triggers should still allow route-based verification."""
        from robothor.engine.session import AgentSession

        session = AgentSession("test-agent", trigger_type=TriggerType.CRON)
        route = MagicMock()
        route.verification = True
        assert runner._should_verify(sample_agent_config, route, session) is True

    def test_explicit_enabled_overrides_telegram_skip(self, runner, sample_agent_config):
        """verification_enabled=True takes precedence even for Telegram."""
        from robothor.engine.session import AgentSession

        sample_agent_config.verification_enabled = True
        session = AgentSession("test-agent", trigger_type=TriggerType.TELEGRAM)
        assert runner._should_verify(sample_agent_config, None, session) is True


# ── Outcome Assessment ──────────────────────────────────────────────────


class TestAssessOutcome:
    """Tests for _assess_outcome — universal outcome assessment for all run types."""

    def test_cron_run_gets_assessed(self):
        """Cron-triggered runs should receive outcome_assessment."""
        from robothor.engine.models import AgentRun, RunStatus, TriggerType
        from robothor.engine.runner import AgentRunner

        run = AgentRun(
            id="run-1",
            agent_id="email-classifier",
            trigger_type=TriggerType.CRON,
            status=RunStatus.COMPLETED,
            output_text="Classified 5 emails successfully.",
        )
        AgentRunner._assess_outcome(run)
        assert run.outcome_assessment == "successful"

    def test_hook_run_gets_assessed(self):
        """Hook-triggered runs should receive outcome_assessment."""
        from robothor.engine.models import AgentRun, RunStatus, TriggerType
        from robothor.engine.runner import AgentRunner

        run = AgentRun(
            id="run-2",
            agent_id="calendar-monitor",
            trigger_type=TriggerType.HOOK,
            status=RunStatus.TIMEOUT,
        )
        AgentRunner._assess_outcome(run)
        assert run.outcome_assessment == "abandoned"

    def test_workflow_run_gets_assessed(self):
        """Workflow-triggered runs should receive outcome_assessment."""
        from robothor.engine.models import AgentRun, RunStatus, TriggerType
        from robothor.engine.runner import AgentRunner

        run = AgentRun(
            id="run-3",
            agent_id="email-responder",
            trigger_type=TriggerType.WORKFLOW,
            status=RunStatus.FAILED,
            error_message="API timeout",
        )
        AgentRunner._assess_outcome(run)
        assert run.outcome_assessment == "incorrect"

    def test_completed_with_errors_is_partial(self):
        from robothor.engine.models import AgentRun, RunStatus, TriggerType
        from robothor.engine.runner import AgentRunner

        run = AgentRun(
            id="run-4",
            agent_id="crm-hygiene",
            trigger_type=TriggerType.CRON,
            status=RunStatus.COMPLETED,
            output_text="Done with some issues.",
            error_message="Warning: 2 contacts skipped",
        )
        AgentRunner._assess_outcome(run)
        assert run.outcome_assessment == "partial"

    def test_completed_minimal_output_is_partial(self):
        from robothor.engine.models import AgentRun, RunStatus, TriggerType
        from robothor.engine.runner import AgentRunner

        run = AgentRun(
            id="run-5",
            agent_id="buddy-watch",
            trigger_type=TriggerType.CRON,
            status=RunStatus.COMPLETED,
            output_text="Done",
        )
        AgentRunner._assess_outcome(run)
        assert run.outcome_assessment == "partial"

    def test_sub_agent_runs_skipped(self):
        """Sub-agent runs should not be assessed."""
        from robothor.engine.models import AgentRun, RunStatus, TriggerType
        from robothor.engine.runner import AgentRunner

        run = AgentRun(
            id="run-6",
            agent_id="email-classifier",
            trigger_type=TriggerType.SUB_AGENT,
            status=RunStatus.COMPLETED,
            output_text="Sub-task result.",
            parent_run_id="parent-123",
        )
        AgentRunner._assess_outcome(run)
        assert run.outcome_assessment is None

    def test_telegram_still_assessed(self):
        """Telegram runs should still be assessed (backward compat)."""
        from robothor.engine.models import AgentRun, RunStatus, TriggerType
        from robothor.engine.runner import AgentRunner

        run = AgentRun(
            id="run-7",
            agent_id="main",
            trigger_type=TriggerType.TELEGRAM,
            status=RunStatus.COMPLETED,
            output_text="Here's your calendar for today with 3 meetings.",
        )
        AgentRunner._assess_outcome(run)
        assert run.outcome_assessment == "successful"


class TestPrimaryModelReached:
    """_check_primary_model_reached — surface silent fallback degradation.

    Regression for the 2026-05-29 audit: codex/gpt-5.5 was missing from the
    engine PATH, so every top agent silently completed on the mimo fallback
    with no error. The detector must flag any run whose used model isn't the
    configured primary.
    """

    def _make_run(self, **kw):
        from robothor.engine.models import AgentRun, RunStatus, TriggerType

        defaults = {
            "id": "run-x",
            "agent_id": "main",
            "trigger_type": TriggerType.CRON,
            "status": RunStatus.COMPLETED,
        }
        defaults.update(kw)
        return AgentRun(**defaults)

    def _config(self, primary):
        from unittest.mock import MagicMock

        cfg = MagicMock()
        cfg.model_primary = primary
        return cfg

    def test_flags_fallback_run(self, caplog):
        import logging

        from robothor.engine.runner import AgentRunner

        run = self._make_run(model_used="openrouter/xiaomi/mimo-v2.5-pro")
        cfg = self._config("codex/gpt-5.5")
        with caplog.at_level(logging.ERROR, logger="robothor.engine.runner"):
            AgentRunner._check_primary_model_reached(run, cfg)
        assert "DEGRADED model" in caplog.text
        assert run.outcome_notes and "fallback" in run.outcome_notes

    def test_silent_when_primary_used(self, caplog):
        import logging

        from robothor.engine.runner import AgentRunner

        run = self._make_run(model_used="codex/gpt-5.5")
        cfg = self._config("codex/gpt-5.5")
        with caplog.at_level(logging.ERROR, logger="robothor.engine.runner"):
            AgentRunner._check_primary_model_reached(run, cfg)
        assert "DEGRADED model" not in caplog.text
        assert run.outcome_notes is None

    def test_skips_sub_agent_runs(self, caplog):
        import logging

        from robothor.engine.runner import AgentRunner

        run = self._make_run(model_used="fallback-model", parent_run_id="parent-1")
        cfg = self._config("primary-model")
        with caplog.at_level(logging.ERROR, logger="robothor.engine.runner"):
            AgentRunner._check_primary_model_reached(run, cfg)
        assert "DEGRADED model" not in caplog.text

    def test_no_false_positive_on_normalized_primary(self, caplog):
        """litellm reports the primary without prefix/with a date — not degraded."""
        import logging

        from robothor.engine.runner import AgentRunner

        # manifest primary vs what litellm returns for a successful primary run
        run = self._make_run(model_used="claude-opus-4-7-20260416")
        cfg = self._config("openrouter/anthropic/claude-opus-4.7")
        with caplog.at_level(logging.ERROR, logger="robothor.engine.runner"):
            AgentRunner._check_primary_model_reached(run, cfg)
        assert "DEGRADED model" not in caplog.text
        assert run.outcome_notes is None


class TestPublishRunTelemetry:
    """_publish_run_telemetry — PR 4 run-level cache-hit-rate metrics.

    Observe-only: computes cache_read/cache_creation/prompt tokens +
    cache_hit_ratio from the run's cumulative totals, emits them as GenAI
    span attributes on a small run-level span, and forwards the same numbers
    to trace.publish_metrics (Redis + optional OTLP export).
    """

    def _make_run(self, **kw):
        from robothor.engine.models import AgentRun

        defaults = {
            "id": "run-x",
            "agent_id": "main",
            "model_used": "anthropic/claude-opus-4-8",
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_creation_tokens": 100,
            "cache_read_tokens": 400,
            "duration_ms": 5000,
        }
        defaults.update(kw)
        return AgentRun(**defaults)

    def test_emits_run_summary_span_with_cache_attributes(self):
        from robothor.engine.telemetry import TraceContext

        trace = TraceContext(run_id="r1", agent_id="main")
        run = self._make_run()

        AgentRunner._publish_run_telemetry(trace, run)

        assert len(trace.spans) == 1
        span = trace.spans[0]
        assert span.name == "run_summary"
        assert span.attributes["gen_ai.usage.input_tokens"] == 1000
        assert span.attributes["gen_ai.usage.output_tokens"] == 200
        assert span.attributes["gen_ai.usage.cache_read_input_tokens"] == 400
        assert span.attributes["gen_ai.usage.cache_creation_input_tokens"] == 100
        assert span.attributes["gen_ai.usage.cache_hit_ratio"] == pytest.approx(0.4)

    def test_forwards_cache_hit_ratio_to_publish_metrics(self):
        trace = MagicMock()
        run = self._make_run()

        AgentRunner._publish_run_telemetry(trace, run)

        trace.publish_metrics.assert_called_once()
        run_data = trace.publish_metrics.call_args[0][0]
        assert run_data["cache_creation_tokens"] == 100
        assert run_data["cache_read_tokens"] == 400
        assert run_data["cache_hit_ratio"] == pytest.approx(0.4)
        assert run_data["input_tokens"] == 1000
        assert run_data["output_tokens"] == 200
        assert run_data["status"] == "completed"

    def test_zero_prompt_tokens_does_not_raise(self):
        """Edge case pinned by the TDD contract: no prompt tokens yet."""
        trace = MagicMock()
        run = self._make_run(input_tokens=0, cache_creation_tokens=0, cache_read_tokens=0)

        AgentRunner._publish_run_telemetry(trace, run)

        run_data = trace.publish_metrics.call_args[0][0]
        assert run_data["cache_hit_ratio"] == 0.0

    def test_none_trace_is_a_noop(self):
        """Guard mirrors the existing ``if trace:`` check at the call site."""
        run = self._make_run()
        AgentRunner._publish_run_telemetry(None, run)  # must not raise

    def test_never_raises_on_broken_trace(self):
        """Best-effort — telemetry must never break a completed run."""
        trace = MagicMock()
        trace.span.side_effect = RuntimeError("boom")
        run = self._make_run()
        AgentRunner._publish_run_telemetry(trace, run)  # must not raise


class TestActiveWatchdogContextVar:
    """The active watchdog is per-task (ContextVar), not per-singleton — so a
    nested/concurrent run can't clobber another run's stall watchdog
    (audit 2026-05-29)."""

    def test_property_reads_contextvar(self, runner):
        from robothor.engine import runner as runner_mod

        sentinel = object()
        token = runner_mod._active_watchdog_var.set(sentinel)
        try:
            assert runner._active_watchdog is sentinel
        finally:
            runner_mod._active_watchdog_var.reset(token)
        assert runner._active_watchdog is None

    def test_nested_set_restores_parent(self, runner):
        from robothor.engine import runner as runner_mod

        parent = object()
        child = object()
        ptok = runner_mod._active_watchdog_var.set(parent)
        try:
            assert runner._active_watchdog is parent
            ctok = runner_mod._active_watchdog_var.set(child)  # nested run
            assert runner._active_watchdog is child
            runner_mod._active_watchdog_var.reset(ctok)  # child finishes
            assert runner._active_watchdog is parent  # parent restored
        finally:
            runner_mod._active_watchdog_var.reset(ptok)

    @pytest.mark.asyncio
    async def test_concurrent_tasks_isolated(self, runner):
        import asyncio

        from robothor.engine import runner as runner_mod

        seen = {}

        async def run_with(name, wd):
            runner_mod._active_watchdog_var.set(wd)
            await asyncio.sleep(0)  # yield so the other task interleaves
            seen[name] = runner._active_watchdog

        a, b = object(), object()
        await asyncio.gather(run_with("a", a), run_with("b", b))
        assert seen["a"] is a  # not clobbered by task b
        assert seen["b"] is b


class TestHandleModelErrorProviderDown:
    """_handle_model_error — provider-availability failures mark the model broken.

    A missing Codex CLI raises CodexProviderError (no HTTP status); it must be
    treated like other hard provider failures so the primary is marked broken
    and the PRIMARY-failed ERROR line fires (audit 2026-05-29).
    """

    def test_codex_missing_marks_primary_broken(self, caplog):
        import logging

        from robothor.engine.codex_provider import CodexProviderError
        from robothor.engine.runner import AgentRunner

        broken: set[str] = set()
        err = CodexProviderError("Codex CLI not found: codex")
        with caplog.at_level(logging.ERROR, logger="robothor.engine.runner"):
            AgentRunner._handle_model_error(err, "codex/gpt-5.5", broken)
        assert "codex/gpt-5.5" in broken
        assert "PRIMARY model" in caplog.text


class TestSynthesizeWrapupSummary:
    """When the force-wrapup LLM call comes back empty, the run must still
    produce a non-empty final text — synthesized from the tool actions taken.
    Regression: curiosity-engine ending on a memory_block_write at the
    iteration cap used to yield output_text=None."""

    def test_summary_lists_distinct_tool_actions(self):
        from robothor.engine.models import TriggerType
        from robothor.engine.runner import AgentRunner
        from robothor.engine.session import AgentSession

        session = AgentSession("curiosity-engine", trigger_type=TriggerType.CRON)
        session.record_tool_call("memory_search", {}, {"ok": True}, "tc-1")
        session.record_tool_call("store_memory", {}, {"ok": True}, "tc-2")
        session.record_tool_call("store_memory", {}, {"ok": True}, "tc-3")  # dup

        summary = AgentRunner._synthesize_wrapup_summary(session, "Iteration cap reached.")

        assert "memory_search" in summary
        assert "store_memory" in summary
        assert "2 tool action(s)" in summary  # deduplicated
        assert summary.strip()

    def test_summary_when_no_tools_called(self):
        from robothor.engine.models import TriggerType
        from robothor.engine.runner import AgentRunner
        from robothor.engine.session import AgentSession

        session = AgentSession("curiosity-engine", trigger_type=TriggerType.CRON)
        summary = AgentRunner._synthesize_wrapup_summary(session, "Iteration cap reached.")

        assert "No output was produced" in summary
        assert summary.strip()


class TestInterruptSteerWiring:
    """G3: the session interrupt/steer API (built in Rip 9) must actually be
    consumed by the loop. Before this, `_after_iteration` was a no-op and
    `_run_loop` never called `consume_interrupt`/`consume_pending_steer`, so the
    advertised live-steering capability did nothing.
    """

    @pytest.mark.asyncio
    async def test_after_iteration_drains_steer_into_user_message(self, runner):
        from robothor.engine.session import AgentSession

        session = AgentSession(agent_id="test-agent")
        session.steer("focus on the budget question")

        await runner._after_iteration(session, 1)

        # Steer is consumed (drained) and surfaced for the next API call.
        assert session.consume_pending_steer() is None
        assert any(
            m.get("role") == "user" and "budget question" in str(m.get("content", ""))
            for m in session.messages
        ), "steer text was not injected as a user message"

    @pytest.mark.asyncio
    async def test_steer_never_touches_system_prompt(self, runner):
        """Cache safety: steering must not mutate the system prompt prefix."""
        from robothor.engine.session import AgentSession

        session = AgentSession(agent_id="test-agent")
        session.messages = [{"role": "system", "content": "STATIC SYSTEM PROMPT"}]
        session.steer("new guidance")

        await runner._after_iteration(session, 1)

        assert session.messages[0] == {"role": "system", "content": "STATIC SYSTEM PROMPT"}

    @pytest.mark.asyncio
    async def test_interrupt_halts_loop_gracefully(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        sample_agent_config.max_iterations = 10  # check-in interval (won't fire)
        sample_agent_config.safety_cap = 8  # would run this far absent interrupt

        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        calls = {"n": 0}

        async def fake_do_llm_call(session, *args, **kwargs):
            calls["n"] += 1
            # Operator interrupts mid-run on the 2nd turn; the top-of-loop check
            # on the 3rd iteration must consume it and stop gracefully.
            if calls["n"] == 2:
                session.interrupt("operator says stop")
            resp = mock_litellm_response(content=None, tool_calls=[tc])
            resp.choices[0].message.content = None
            return resp

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_step"),
            patch.object(runner._llm, "_do_llm_call", new=AsyncMock(side_effect=fake_do_llm_call)),
        ):
            run = await runner.execute("test-agent", "hello", agent_config=sample_agent_config)

        # Stopped at the top of iteration 3 → exactly 2 LLM calls, not safety_cap.
        assert calls["n"] == 2, f"expected 2 LLM calls before interrupt, got {calls['n']}"
        assert run.status != RunStatus.FAILED
        assert "interrupt" in (run.outcome_notes or "").lower()

    @pytest.mark.asyncio
    async def test_interrupt_via_public_api_reaches_live_run(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """End-to-end: an external caller (Telegram/health API) halts a LIVE run
        by run_id via interrupt_session -> session_registry.lookup. Proves the
        runner registers the session (without registration this lookup fails and
        the loop runs to safety_cap)."""
        from robothor.engine.interrupt_api import interrupt_session

        sample_agent_config.max_iterations = 10
        sample_agent_config.safety_cap = 8

        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"
        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        calls = {"n": 0}
        captured = {"interrupt_ok": None}

        async def fake_do_llm_call(session, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                # External caller reaches the live run ONLY via the registry.
                captured["interrupt_ok"] = interrupt_session(session.run_id, "operator stop")
            resp = mock_litellm_response(content=None, tool_calls=[tc])
            resp.choices[0].message.content = None
            return resp

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_step"),
            patch.object(runner._llm, "_do_llm_call", new=AsyncMock(side_effect=fake_do_llm_call)),
        ):
            run = await runner.execute("test-agent", "hello", agent_config=sample_agent_config)

        assert captured["interrupt_ok"] is True, "session was not registered — lookup failed"
        assert calls["n"] == 2
        assert "interrupt" in (run.outcome_notes or "").lower()

    @pytest.mark.asyncio
    async def test_session_unregistered_after_run(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """The registry must not leak: after a run completes, lookup returns None."""
        from robothor.engine import session_registry

        seen = {"run_id": None}

        async def fake_do_llm_call(session, *args, **kwargs):
            seen["run_id"] = session.run_id
            assert session_registry.lookup(session.run_id) is session  # live during the run
            return mock_litellm_response(content="done")

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_step"),
            patch.object(runner._llm, "_do_llm_call", new=AsyncMock(side_effect=fake_do_llm_call)),
        ):
            await runner.execute("test-agent", "hello", agent_config=sample_agent_config)

        assert seen["run_id"] is not None
        assert session_registry.lookup(seen["run_id"]) is None  # unregistered in finally
