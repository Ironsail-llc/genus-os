"""A human's role decides what their session is offered, not only what it is refused.

The owner of an instance holds a ``*`` allow in RBAC, yet an agent that names no
``tools_allowed`` is offered every tool MINUS the opt-in families
(``claude_code_*``, ``web_render``, the Jira and GitHub review writes,
``run_workflow``) unless its manifest names them. So the owner asks the main
agent to start a coding job, the tool is not in the run's toolset, admission
answers "not available to this agent", and the agent tells the owner to do it
themselves. The role was known the whole time (``AgentRun.user_role``); nothing
turned it into a capability any gate could read.

The capability tier is that missing value. It only ever WIDENS what a run is
offered, only for an interactive run with a verified identity, and only under
``ROBOTHOR_ROLE_CAPABILITIES_MODE=enforce``. ``observe`` records what it would
have unlocked and changes nothing. Off is today, byte for byte.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, AgentRun, RunStatus, TriggerType
from robothor.engine.tools.constants import CLAUDE_CODE_TOOLS, OPT_IN_TOOLS
from robothor.identity import IdentityContext


@pytest.fixture(autouse=True)
def _clean_flags(monkeypatch):
    for var in (
        "ROBOTHOR_DISABLE_ALL_RIPS",
        "ROBOTHOR_ROLE_CAPABILITIES_ENABLED",
        "ROBOTHOR_ROLE_CAPABILITIES_MODE",
        "ROBOTHOR_RIP_16_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)


def _mode(monkeypatch, mode: str | None) -> None:
    if mode is None:
        return
    monkeypatch.setenv("ROBOTHOR_ROLE_CAPABILITIES_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_ROLE_CAPABILITIES_MODE", mode)


# ── The flag ─────────────────────────────────────────────────────────────


class TestTheFlag:
    def test_it_is_off_when_unset(self):
        from robothor.engine.feature_flags import role_capabilities_mode

        assert role_capabilities_mode() == "off"

    def test_enabling_it_lands_in_observe(self, monkeypatch):
        from robothor.engine.feature_flags import role_capabilities_mode

        monkeypatch.setenv("ROBOTHOR_ROLE_CAPABILITIES_ENABLED", "1")
        assert role_capabilities_mode() == "observe"

    def test_enforce_is_read_as_written(self, monkeypatch):
        from robothor.engine.feature_flags import role_capabilities_mode

        _mode(monkeypatch, "enforce")
        assert role_capabilities_mode() == "enforce"

    def test_the_panic_flag_turns_it_off(self, monkeypatch):
        from robothor.engine.feature_flags import role_capabilities_mode

        _mode(monkeypatch, "enforce")
        monkeypatch.setenv("ROBOTHOR_DISABLE_ALL_RIPS", "1")
        assert role_capabilities_mode() == "off"

    def test_its_evidence_is_the_role_capability_guardrail_row(self):
        from robothor.flags.evidence import EVIDENCE_SOURCES

        source = EVIDENCE_SOURCES["ROBOTHOR_ROLE_CAPABILITIES_MODE"]
        assert source.table == "agent_guardrail_events"
        assert "role_capability" in source.where


# ── Role -> tier ─────────────────────────────────────────────────────────


class TestResolveTier:
    @pytest.mark.parametrize(
        "role,tier",
        [
            ("owner", "unrestricted"),
            ("admin", "elevated"),
            ("member", "standard"),
            ("user", "standard"),
            ("service", "standard"),
            ("viewer", "restricted"),
            ("auditor", "restricted"),
            ("guest", "restricted"),
            ("some-custom-role", "standard"),
            ("", "standard"),
        ],
    )
    def test_each_role_maps_to_one_tier(self, role, tier):
        from robothor.engine.capability_tier import resolve_tier

        decision = resolve_tier(role, interactive=True, identity_verified=True, mode="enforce")
        assert decision.tier == tier
        assert decision.role == role

    def test_a_system_run_never_widens_even_for_an_owner_principal(self):
        from robothor.engine.capability_tier import resolve_tier

        decision = resolve_tier("owner", interactive=False, identity_verified=True, mode="enforce")
        assert decision.tier == "standard"
        assert "interactive" in decision.reason

    def test_an_unverified_owner_never_widens(self):
        """The Telegram default-chat fallback fabricates an owner for an
        unregistered sender; that identity is verified=False."""
        from robothor.engine.capability_tier import resolve_tier

        decision = resolve_tier("owner", interactive=True, identity_verified=False, mode="enforce")
        assert decision.tier == "standard"
        assert "verified" in decision.reason

    def test_the_effective_tier_is_empty_below_enforce(self):
        from robothor.engine.capability_tier import resolve_tier

        observed = resolve_tier("owner", interactive=True, identity_verified=True, mode="observe")
        enforced = resolve_tier("owner", interactive=True, identity_verified=True, mode="enforce")

        assert observed.tier == "unrestricted"
        assert observed.effective == ""
        assert enforced.effective == "unrestricted"


# ── The registry offers opt-ins by tier ──────────────────────────────────


@pytest.fixture(scope="module")
def registry():
    from robothor.engine.tools.registry import ToolRegistry

    return ToolRegistry()


def _default_agent(**kw) -> AgentConfig:
    return AgentConfig(id="probe", name="Probe", **kw)


class TestTheRegistry:
    def test_no_tier_is_todays_set(self, registry):
        config = _default_agent()
        assert registry.get_tool_names(config, tier="") == registry.get_tool_names(config)
        assert not set(registry.get_tool_names(config)) & OPT_IN_TOOLS

    @pytest.mark.parametrize("tier", ["standard", "restricted"])
    def test_standard_and_restricted_change_nothing(self, registry, tier):
        config = _default_agent()
        assert registry.get_tool_names(config, tier=tier) == registry.get_tool_names(config)

    def test_unrestricted_offers_the_opt_in_families(self, registry):
        names = set(registry.get_tool_names(_default_agent(), tier="unrestricted"))
        assert {"claude_code_start", "web_render", "jira_create_issue"} <= names

    def test_elevated_offers_web_render(self, registry):
        names = set(registry.get_tool_names(_default_agent(), tier="elevated"))
        assert "web_render" in names

    def test_build_for_agent_advertises_what_get_tool_names_lists(self, registry, monkeypatch):
        # Deferred loading is on by default; this pins the advertised set of a
        # run that is not deferred. A deferred run reaches the same tools
        # through its whitelist (test_a_deferred_run_can_tool_call_...).
        monkeypatch.setenv("ROBOTHOR_RIP_16_ENABLED", "0")
        schemas = registry.build_for_agent(_default_agent(), tier="unrestricted")
        advertised = {s["function"]["name"] for s in schemas}
        assert "claude_code_start" in advertised

    def test_tools_denied_still_wins(self, registry):
        config = _default_agent(tools_denied=["claude_code_*"])
        names = set(registry.get_tool_names(config, tier="unrestricted"))
        assert not names & CLAUDE_CODE_TOOLS
        assert "web_render" in names

    def test_an_explicit_tools_allowed_list_is_not_widened(self, registry):
        config = _default_agent(tools_allowed=["read_file", "web_fetch"])
        assert registry.get_tool_names(config, tier="unrestricted") == registry.get_tool_names(
            config
        )

    def test_a_deferred_run_can_tool_call_what_the_tier_unlocked(self, registry, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RIP_16_ENABLED", "1")
        config = _default_agent()
        assert "claude_code_status" in registry.deferred_whitelist(config, tier="unrestricted")
        assert "claude_code_status" not in registry.deferred_whitelist(config)


# ── prepare_toolset reports what the tier unlocked ───────────────────────


@pytest.mark.asyncio
class TestPrepareToolset:
    async def _prepare(self, registry, decision):
        from robothor.engine import toolset_prep

        with patch.object(toolset_prep, "_autonomy_active", AsyncMock(return_value=False)):
            return await toolset_prep.prepare_toolset(
                registry,
                _default_agent(),
                agent_id="probe",
                system_prompt="",
                readonly_mode=False,
                deep_plan=False,
                capability=decision,
            )

    async def test_observe_advertises_nothing_new_and_names_what_it_would_unlock(self, registry):
        from robothor.engine.capability_tier import resolve_tier

        decision = resolve_tier("owner", interactive=True, identity_verified=True, mode="observe")
        prepared = await self._prepare(registry, decision)

        assert "claude_code_start" not in prepared.tool_names
        assert "claude_code_start" in prepared.tier_unlocked

    async def test_enforce_advertises_what_it_unlocks(self, registry, monkeypatch):
        from robothor.engine.capability_tier import resolve_tier

        monkeypatch.setenv("ROBOTHOR_RIP_16_ENABLED", "0")  # not deferred: see above

        decision = resolve_tier("owner", interactive=True, identity_verified=True, mode="enforce")
        prepared = await self._prepare(registry, decision)

        advertised = {s["function"]["name"] for s in prepared.tool_schemas}
        assert "claude_code_start" in advertised
        assert "claude_code_start" in prepared.tier_unlocked

    async def test_enforce_on_a_deferred_run_makes_what_it_unlocks_reachable(self, registry):
        from robothor.engine.capability_tier import resolve_tier

        decision = resolve_tier("owner", interactive=True, identity_verified=True, mode="enforce")
        prepared = await self._prepare(registry, decision)

        assert "claude_code_start" in prepared.tool_names
        assert "claude_code_start" in prepared.tier_unlocked

    async def test_no_decision_is_todays_toolset(self, registry):
        prepared = await self._prepare(registry, None)
        assert prepared.tool_names == registry.get_tool_names(_default_agent())
        assert prepared.tier_unlocked == ()


# ── The run records its tier ─────────────────────────────────────────────


class TestRunIdentity:
    def test_a_run_without_a_tier_persists_no_tier_key(self):
        from robothor.engine.runtime.current import run_identity

        assert "capability_tier" not in run_identity(AgentRun(agent_id="probe"))

    def test_a_run_with_a_tier_persists_tier_mode_and_reason(self):
        from robothor.engine.runtime.current import run_identity

        run = AgentRun(agent_id="probe")
        run.capability_tier = "unrestricted"
        run.capability_tier_mode = "observe"
        run.capability_tier_reason = "role owner"

        stored = run_identity(run)["capability_tier"]
        assert stored == {"tier": "unrestricted", "mode": "observe", "reason": "role owner"}
        json.dumps(stored)


# ── Handlers can read the effective tier ─────────────────────────────────


@pytest.mark.asyncio
async def test_the_tool_context_carries_the_effective_tier():
    from robothor.engine.capability_tier import clear_effective_tier, set_effective_tier
    from robothor.engine.tools import dispatch

    seen = {}

    async def _capture(name, args, ctx):
        seen["tier"] = ctx.capability_tier
        return {"error": "stop here"}

    token = set_effective_tier("unrestricted")
    try:
        with (
            patch.object(dispatch, "_refuse_before_handler", AsyncMock(return_value=None)),
            patch.object(dispatch, "_runtime_denial", _capture),
        ):
            await dispatch._execute_tool("read_file", {})
    finally:
        clear_effective_tier(token)

    assert seen["tier"] == "unrestricted"


class TestTheTierIsPublishedWithTheToolset:
    def _registry(self):
        registry = MagicMock()
        registry.should_defer.return_value = False
        return registry

    def test_it_is_visible_while_published_and_gone_after(self):
        from robothor.engine.capability_tier import current_effective_tier, resolve_tier
        from robothor.engine.toolset_prep import publish_toolset, withdraw_toolset

        decision = resolve_tier("owner", interactive=True, identity_verified=True, mode="enforce")
        tokens = publish_toolset(
            self._registry(), _default_agent(), ["read_file"], capability=decision
        )
        try:
            assert current_effective_tier() == "unrestricted"
        finally:
            withdraw_toolset(tokens)
        assert current_effective_tier() == ""

    def test_observe_publishes_no_tier(self):
        from robothor.engine.capability_tier import current_effective_tier, resolve_tier
        from robothor.engine.toolset_prep import publish_toolset, withdraw_toolset

        decision = resolve_tier("owner", interactive=True, identity_verified=True, mode="observe")
        tokens = publish_toolset(
            self._registry(), _default_agent(), ["read_file"], capability=decision
        )
        try:
            assert current_effective_tier() == ""
        finally:
            withdraw_toolset(tokens)

    def test_a_child_run_inline_restores_its_parents_tier(self):
        """spawn_agent runs the child's execute inline in the parent's task."""
        from robothor.engine.capability_tier import current_effective_tier, resolve_tier
        from robothor.engine.toolset_prep import publish_toolset, withdraw_toolset

        owner = resolve_tier("owner", interactive=True, identity_verified=True, mode="enforce")
        child = resolve_tier("owner", interactive=False, identity_verified=True, mode="enforce")
        parent_tokens = publish_toolset(self._registry(), _default_agent(), [], capability=owner)
        try:
            child_tokens = publish_toolset(self._registry(), _default_agent(), [], capability=child)
            assert current_effective_tier() == "standard"
            withdraw_toolset(child_tokens)
            assert current_effective_tier() == "unrestricted"
        finally:
            withdraw_toolset(parent_tokens)


# ── End to end through the runner ────────────────────────────────────────


def _owner_identity(*, verified: bool = True, role: str = "owner") -> IdentityContext:
    return IdentityContext(
        tenant_id="test-tenant",
        channel="webchat",
        identifier="u-alice",
        verified=verified,
        display_name="Alice",
        role=role,
        user_account_id="u-alice" if verified else None,
    )


@pytest.fixture
def runner(engine_config):
    from robothor.engine.runner import AgentRunner

    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()

        def _names(config, tier=""):
            # The real registry widens only for these two tiers (TestTheRegistry).
            widening = tier in ("unrestricted", "elevated")
            return ["read_file", "claude_code_start"] if widening else ["read_file"]

        def _schemas(config, autonomy=False, tier=""):
            return [{"type": "function", "function": {"name": n}} for n in _names(config, tier)]

        registry.get_tool_names.side_effect = _names
        registry.build_for_agent.side_effect = _schemas
        registry.should_defer.return_value = False
        registry.tier_seen = []

        async def _execute(*args, **kwargs):
            from robothor.engine.capability_tier import current_effective_tier

            registry.tier_seen.append(current_effective_tier())
            return {"ok": True}

        registry.execute = AsyncMock(side_effect=_execute)
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


@pytest.fixture
def default_agent() -> AgentConfig:
    from robothor.engine.models import DeliveryMode

    return AgentConfig(
        id="test-agent",
        name="Test Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        delivery_mode=DeliveryMode.NONE,
        timeout_seconds=30,
    )


async def _run_one_tool_call(
    runner,
    agent,
    mock_litellm_response,
    *,
    tool="claude_code_start",
    trigger=TriggerType.WEBCHAT,
    role="owner",
    identity=None,
    user_id="u-alice",
):
    tc = MagicMock()
    tc.id = "c1"
    tc.function.name = tool
    tc.function.arguments = json.dumps({})
    first = mock_litellm_response(content=None, tool_calls=[tc])
    first.choices[0].message.content = None
    done = mock_litellm_response(content="done")
    seq = {"n": 0}

    async def _completion(**kwargs):
        seq["n"] += 1
        return first if seq["n"] == 1 else done

    events: list[dict] = []
    loop_thread = threading.get_ident()

    def _log(run_id, guardrail_name, action, **kw):
        events.append(
            {
                "guardrail_name": guardrail_name,
                "action": action,
                "off_loop": threading.get_ident() != loop_thread,
                **kw,
            }
        )

    with (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("robothor.engine.tracking.log_guardrail_event", side_effect=_log),
        patch("litellm.acompletion", side_effect=_completion),
    ):
        run = await runner.execute(
            agent.id,
            "start a coding job",
            trigger_type=trigger,
            agent_config=agent,
            user_id=user_id,
            user_role=role,
            identity=identity,
        )
    role_events = [e for e in events if e["guardrail_name"] == "role_capability"]
    return run, role_events


@pytest.mark.asyncio
class TestTheRunner:
    async def test_off_is_todays_run(self, runner, default_agent, mock_litellm_response):
        run, events = await _run_one_tool_call(
            runner, default_agent, mock_litellm_response, identity=_owner_identity()
        )

        assert run.capability_tier == ""
        assert events == []
        assert runner.registry.build_for_agent.call_args.kwargs.get("tier", "") == ""
        # claude_code_start was not offered, so admission refused it.
        runner.registry.execute.assert_not_awaited()

    async def test_observe_records_one_row_and_changes_nothing(
        self, runner, default_agent, mock_litellm_response, monkeypatch
    ):
        _mode(monkeypatch, "observe")
        run, events = await _run_one_tool_call(
            runner, default_agent, mock_litellm_response, identity=_owner_identity()
        )

        assert run.status == RunStatus.COMPLETED
        assert run.capability_tier == "unrestricted"
        assert run.capability_tier_mode == "observe"
        runner.registry.execute.assert_not_awaited()
        assert len(events) == 1
        assert events[0]["action"] == "observed"
        assert "claude_code_start" in events[0]["reason"]
        assert events[0]["off_loop"]

    async def test_enforce_owner_admits_opt_in_tool(
        self, runner, default_agent, mock_litellm_response, monkeypatch
    ):
        _mode(monkeypatch, "enforce")
        run, events = await _run_one_tool_call(
            runner, default_agent, mock_litellm_response, identity=_owner_identity()
        )

        assert run.capability_tier == "unrestricted"
        assert runner.registry.build_for_agent.call_args.kwargs["tier"] == "unrestricted"
        runner.registry.execute.assert_awaited_once()
        assert runner.registry.execute.await_args.args[0] == "claude_code_start"
        assert runner.registry.tier_seen == ["unrestricted"]
        assert [e["action"] for e in events] == ["allowed"]
        assert events[0]["mode"] == "enforce"
        assert events[0]["off_loop"]

    async def test_enforce_admin_is_elevated(
        self, runner, default_agent, mock_litellm_response, monkeypatch
    ):
        _mode(monkeypatch, "enforce")
        run, events = await _run_one_tool_call(
            runner,
            default_agent,
            mock_litellm_response,
            role="admin",
            identity=_owner_identity(role="admin"),
        )

        assert run.capability_tier == "elevated"
        assert runner.registry.tier_seen == ["elevated"]
        assert [e["action"] for e in events] == ["allowed"]

    async def test_enforce_member_gets_nothing_new(
        self, runner, default_agent, mock_litellm_response, monkeypatch
    ):
        _mode(monkeypatch, "enforce")
        run, events = await _run_one_tool_call(
            runner,
            default_agent,
            mock_litellm_response,
            role="member",
            identity=_owner_identity(role="member"),
        )

        assert run.capability_tier == "standard"
        runner.registry.execute.assert_not_awaited()
        assert events == []

    async def test_enforce_cron_gets_nothing_new(
        self, runner, default_agent, mock_litellm_response, monkeypatch
    ):
        _mode(monkeypatch, "enforce")
        run, events = await _run_one_tool_call(
            runner,
            default_agent,
            mock_litellm_response,
            trigger=TriggerType.CRON,
            role="owner",
            identity=None,
            user_id="",
        )

        assert run.capability_tier == "standard"
        assert events == []

    async def test_enforce_unregistered_telegram_owner_gets_nothing_new(
        self, runner, default_agent, mock_litellm_response, monkeypatch
    ):
        _mode(monkeypatch, "enforce")
        fabricated = IdentityContext(
            tenant_id="test-tenant",
            channel="telegram",
            identifier="999",
            verified=False,
            display_name="Bob",
            role="owner",
        )
        run, events = await _run_one_tool_call(
            runner,
            default_agent,
            mock_litellm_response,
            trigger=TriggerType.TELEGRAM,
            role="owner",
            identity=fabricated,
            user_id="telegram:12345",
        )

        assert run.capability_tier == "standard"
        runner.registry.execute.assert_not_awaited()
        assert events == []
