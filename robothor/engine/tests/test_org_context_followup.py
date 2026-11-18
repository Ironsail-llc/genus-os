"""A follow-up interactive turn carries the ORGANISATION block too.

Warmup is skipped on a rapid follow-up, and only the CURRENT USER block used
to be resent. The organisation block now travels with it, its database read
runs off the event loop, and a failure costs the block, never the turn.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.models import RunStatus, TriggerType
from robothor.engine.runner import AgentRunner
from robothor.identity import IdentityContext

_ORG = "--- ORGANISATION ---\nYou serve: Acme Holdings (tenant id: acme-parent)."
_HISTORY = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello"},
]


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = []
        registry.get_tool_names.return_value = []
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


async def _follow_up(runner, sample_agent_config, mock_litellm_response, org_fn) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def mock_completion(**kwargs):
        captured["messages"] = [dict(m) for m in kwargs["messages"]]
        return mock_litellm_response(content="ok")

    identity = IdentityContext(
        tenant_id="acme-parent",
        channel="webchat",
        identifier="acct-1",
        verified=True,
        display_name="Alice",
        role="owner",
    )
    with (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("robothor.identity.enrichment.enrich_identity_for_session", return_value=None),
        patch("robothor.engine.runner._seconds_since_last_interactive_run", return_value=5.0),
        patch("robothor.engine.org_context.organisation_block", side_effect=org_fn),
        patch("litellm.acompletion", side_effect=mock_completion),
    ):
        run = await runner.execute(
            "test-agent",
            "follow-up",
            agent_config=sample_agent_config,
            trigger_type=TriggerType.WEBCHAT,
            conversation_history=list(_HISTORY),
            tenant_id="acme-parent",
            user_id="acct-1",
            user_role="owner",
            identity=identity,
        )
    captured["run"] = run
    return captured


@pytest.mark.usefixtures("_mock_run_persistence")
class TestFollowUpOrganisation:
    @pytest.mark.asyncio
    async def test_follow_up_turn_carries_identity_and_organisation(
        self, runner, sample_agent_config, mock_litellm_response
    ) -> None:
        import threading

        main_thread = threading.get_ident()
        seen: dict[str, Any] = {}

        def org_fn(tenant_id, accessible):
            seen["thread"] = threading.get_ident()
            seen["tenant"] = tenant_id
            return _ORG

        captured = await _follow_up(runner, sample_agent_config, mock_litellm_response, org_fn)

        assert captured["run"].status == RunStatus.COMPLETED
        whole = "\n".join(
            m["content"] for m in captured["messages"] if isinstance(m.get("content"), str)
        )
        assert "--- CURRENT USER ---" in whole
        assert "--- ORGANISATION ---" in whole
        assert whole.count("--- ORGANISATION ---") == 1
        assert seen["tenant"] == "acme-parent"
        # The DB read ran in a worker thread, not on the event loop.
        assert seen["thread"] != main_thread

    @pytest.mark.asyncio
    async def test_failed_organisation_lookup_keeps_the_turn(
        self, runner, sample_agent_config, mock_litellm_response
    ) -> None:
        def org_fn(tenant_id, accessible):
            raise RuntimeError("db down")

        captured = await _follow_up(runner, sample_agent_config, mock_litellm_response, org_fn)

        assert captured["run"].status == RunStatus.COMPLETED
        whole = "\n".join(
            m["content"] for m in captured["messages"] if isinstance(m.get("content"), str)
        )
        assert "--- CURRENT USER ---" in whole
        assert "--- ORGANISATION ---" not in whole
