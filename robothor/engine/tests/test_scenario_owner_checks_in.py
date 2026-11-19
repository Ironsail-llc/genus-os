"""Scenario: the owner of a parent tenant with two children checks in.

The owner of Acme Holdings asked its main agent "how are my fleets and
businesses doing?" and got nothing useful: the agent could not see the child
tenants, asked the owner for lists and links, was refused by the per-minute
limit, and named the gate that refused it. This is the permanent regression
test for that conversation. It drives a real ``AgentRunner`` with a scripted
model (``litellm.acompletion``) and asserts engine properties, never prose:

* the model is shown the ORGANISATION block, naming both children and
  pointing at ``org_overview``, on the first turn and on a follow-up;
* ``org_overview`` answers for every tenant;
* a read with ``tenant`` set to a child runs in that child for the owner, and
  is refused with one and the same sentence for a member, a sibling and an
  unknown tenant;
* forty tool calls inside one minute are paced, never refused;
* the system prompt carries the chat rules.

Tool calls go through the real dispatch (``tools.dispatch._execute_tool``)
with the handlers faked, as ``test_cross_tenant_reads`` does; the tenant tree
is ``test_org_context``'s and the clock ``test_tool_rate_limit_waits``'s.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode, RunStatus, TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.tests.test_cross_tenant_reads import _Seen
from robothor.engine.tests.test_org_context import _get_tenant, _list_tenants
from robothor.engine.tests.test_tool_rate_limit_waits import FakeClock
from robothor.identity import IdentityContext

PARENT, CHILD_A, CHILD_B = "acme-parent", "acme-child-a", "acme-child-b"
EVERYTHING = (PARENT, CHILD_A, CHILD_B)
QUESTION = "How are my fleets and businesses doing?"
ANSWER = "Acme Child A needs attention."

#: Rules 21 and 22 of ``prompts.BEHAVIORAL_RULES``. The module keeps the rules
#: in one string and exposes no constant per rule, so these are the shortest
#: phrases that carry each rule's meaning: rewording around them is harmless,
#: removing a rule fails the test.
CHAT_RULE_PHRASES = (
    "lead with the answer",
    "never ask the person for facts the platform holds",
    "never mention runtime throttles, internal gate names or field names",
)


def _resolve(tenant_id: str, user_role: str | None = None, **_: Any) -> tuple[str, ...]:
    """The hierarchy walk: an owner or admin of the parent reaches its children."""
    if tenant_id == PARENT and user_role in ("owner", "admin"):
        return EVERYTHING
    return (tenant_id,)


class _Rollup:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, tenant_ids: Any, **_: Any) -> dict[str, Any]:
        ids = list(tenant_ids)
        self.calls.append(ids)
        return {
            "as_of": "2026-11-18T12:00:00+00:00",
            "tenants": [{"tenant_id": t, "status": "ok"} for t in ids],
        }


def _config(**overrides: Any) -> AgentConfig:
    config = AgentConfig(
        id="main",
        name="Main",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        timeout_seconds=3600,
        delivery_mode=DeliveryMode.NONE,
        tools_allowed=["org_overview", "list_tasks"],
        planning_enabled=False,
        # The default limit (0 means the platform default) is the one on trial.
        guardrails=["rate_limit"],
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _call(name: str, args: dict[str, Any] | None = None, i: int = 0) -> MagicMock:
    tc = MagicMock()
    tc.id = f"call_{i}"
    tc.function.name = name
    tc.function.arguments = json.dumps(args or {})
    return tc


class _Model:
    """A scripted model: one list of tool calls per turn, then the answer."""

    def __init__(self, respond: Any, turns: list[list[MagicMock]]) -> None:
        self._respond = respond
        self._turns = list(turns)
        self.sent: list[list[dict[str, Any]]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.sent.append([dict(m) for m in kwargs["messages"]])
        if self._turns:
            response = self._respond(content=None, tool_calls=self._turns.pop(0))
            response.choices[0].message.content = None
            return response
        return self._respond(content=ANSWER)

    def tool_results(self) -> list[dict[str, Any]]:
        return [json.loads(m["content"]) for m in self.sent[-1] if m.get("role") == "tool"]

    def text(self, request: int = 0) -> str:
        return "\n".join(
            m["content"] for m in self.sent[request] if isinstance(m.get("content"), str)
        )


@pytest.fixture
def world(engine_config):
    """A real runner whose tool calls go through the real dispatch."""
    from robothor.engine.tools.dispatch import _execute_tool
    from robothor.engine.tools.handlers.org import HANDLERS as ORG

    seen, rollup, clock = _Seen(), _Rollup(), FakeClock()

    async def execute(name: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
        kw.pop("timeout", None)
        return await _execute_tool(name, dict(args), **kw)

    with patch("robothor.engine.runner.get_registry") as get_registry:
        registry = MagicMock()
        registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "org_overview"}},
            {"type": "function", "function": {"name": "list_tasks"}},
        ]
        registry.get_tool_names.return_value = ["org_overview", "list_tasks"]
        registry.get_adapter_route.return_value = None
        registry.execute = AsyncMock(side_effect=execute)
        get_registry.return_value = registry
        runner = AgentRunner(engine_config)
        runner.registry = registry

    handlers = {"org_overview": ORG["org_overview"], "list_tasks": seen.handler()}
    with (
        patch("robothor.crm.dal.get_tenant", side_effect=_get_tenant),
        patch("robothor.crm.dal.list_tenants", side_effect=_list_tenants),
        patch("robothor.engine.permissions.resolve_accessible_tenants", side_effect=_resolve),
        patch("robothor.engine.permissions.check_tool_permission", return_value=None),
        patch("robothor.engine.tools.dispatch._runtime_denial", AsyncMock(return_value=None)),
        patch("robothor.engine.tools.dispatch._get_handlers", return_value=handlers),
        patch("robothor.engine.tools.get_registry", return_value=registry),
        patch("robothor.engine.tools.dispatch._audit_tool_call"),
        patch("robothor.engine.tools.cross_tenant.audit_cross_tenant"),
        patch("robothor.tenancy.rollup.tenant_rollup", side_effect=rollup),
        patch("robothor.identity.enrichment.enrich_identity_for_session", return_value=None),
        # main's fleet roster: no database here.
        patch("robothor.engine.tracking.list_schedules", return_value=[]),
        patch("robothor.crm.dal.list_tasks", return_value=[]),
        patch("robothor.engine.guardrails.time", SimpleNamespace(monotonic=clock.monotonic)),
        patch("robothor.engine.tool_throttle.asyncio", SimpleNamespace(sleep=clock.sleep)),
        patch.object(runner, "_apply_routing", return_value=None),
    ):
        yield SimpleNamespace(runner=runner, seen=seen, rollup=rollup, clock=clock)


async def _turn(
    world: Any,
    respond: Any,
    turns: list[list[MagicMock]],
    *,
    role: str = "owner",
    tenant: str = PARENT,
    history: list[dict[str, Any]] | None = None,
    **config: Any,
) -> tuple[Any, _Model]:
    who = "alice" if role == "owner" else "bob"
    identity = IdentityContext(
        tenant_id=tenant,
        channel="webchat",
        identifier=who,
        verified=True,
        display_name=who.title(),
        role=role,
    )
    model = _Model(respond, turns)

    async def completion(**kwargs: Any) -> Any:
        return await model(**kwargs)

    with patch("litellm.acompletion", side_effect=completion):
        run = await world.runner.execute(
            "main",
            QUESTION,
            agent_config=_config(**config),
            trigger_type=TriggerType.WEBCHAT,
            conversation_history=history,
            tenant_id=tenant,
            user_id=who,
            user_role=role,
            identity=identity,
        )
    return run, model


def _errors(model: _Model) -> list[str]:
    return [str(r.get("error") or "") for r in model.tool_results()]


@pytest.mark.usefixtures("_mock_run_persistence")
@pytest.mark.asyncio
class TestTheOwnerChecksIn:
    async def test_the_model_is_shown_the_organisation_and_the_overview_tool(
        self, world, mock_litellm_response
    ):
        run, model = await _turn(world, mock_litellm_response, [])

        assert run.status == RunStatus.COMPLETED
        text = model.text()
        assert text.count("--- ORGANISATION ---") == 1
        org = text.split("--- ORGANISATION ---", 1)[1]
        assert "Acme Holdings" in org
        assert "Acme Child A" in org and "Acme Child B" in org
        assert "org_overview" in org

    async def test_a_follow_up_turn_is_shown_the_organisation_again(
        self, world, mock_litellm_response
    ):
        history = [
            {"role": "user", "content": QUESTION},
            {"role": "assistant", "content": ANSWER},
        ]
        with patch("robothor.engine.runner._seconds_since_last_interactive_run", return_value=5.0):
            run, model = await _turn(world, mock_litellm_response, [], history=history)

        assert run.status == RunStatus.COMPLETED
        text = model.text()
        assert text.count("--- ORGANISATION ---") == 1
        org = text.split("--- ORGANISATION ---", 1)[1]
        assert "Acme Child A" in org and "Acme Child B" in org
        assert "org_overview" in org

    async def test_org_overview_answers_for_every_tenant(self, world, mock_litellm_response):
        run, model = await _turn(world, mock_litellm_response, [[_call("org_overview")]])

        assert run.status == RunStatus.COMPLETED
        (result,) = model.tool_results()
        assert [t["tenant_id"] for t in result["tenants"]] == list(EVERYTHING)
        assert world.rollup.calls == [list(EVERYTHING)]

    async def test_the_owner_reads_inside_a_child(self, world, mock_litellm_response):
        call = _call("list_tasks", {"tenant": CHILD_A, "overdue": True})
        run, model = await _turn(world, mock_litellm_response, [[call]])

        assert run.status == RunStatus.COMPLETED
        (seen,) = world.seen.calls
        assert seen["tenant_id"] == CHILD_A
        assert seen["scope"] == CHILD_A and seen["scope_in_thread"] == CHILD_A
        assert seen["args"] == {"overdue": True}
        assert _errors(model) == [""]

    async def test_a_member_a_sibling_and_an_unknown_tenant_get_one_refusal(
        self, world, mock_litellm_response
    ):
        from robothor.engine.tools.cross_tenant import UNREACHABLE

        member = [[_call("list_tasks", {"tenant": CHILD_A})]]
        sibling = [[_call("list_tasks", {"tenant": CHILD_B})]]
        unknown = [[_call("list_tasks", {"tenant": "acme-unknown"})]]

        _, as_member = await _turn(world, mock_litellm_response, member, role="member")
        _, as_child_admin = await _turn(
            world, mock_litellm_response, sibling, role="admin", tenant=CHILD_A
        )
        _, as_owner = await _turn(world, mock_litellm_response, unknown)

        assert world.seen.calls == [], "a refused call must run nothing"
        assert _errors(as_member) == [UNREACHABLE]
        assert _errors(as_child_admin) == [UNREACHABLE]
        assert _errors(as_owner) == [UNREACHABLE]

    async def test_forty_calls_inside_a_minute_are_never_refused(
        self, world, mock_litellm_response
    ):
        burst = [[_call("list_tasks", {"page": i}, i)] for i in range(40)]
        run, model = await _turn(
            world, mock_litellm_response, burst, max_iterations=100, safety_cap=100
        )

        assert run.status == RunStatus.COMPLETED
        assert len(world.seen.calls) == 40
        assert world.clock.now - 1000.0 < 60, "the forty calls fit inside one minute"
        tool_messages = [m for m in model.sent[-1] if m.get("role") == "tool"]
        assert len(tool_messages) == 40
        assert all("rate" not in str(m.get("content", "")).lower() for m in tool_messages)
        assert not [s for s in run.steps if s.error_message and "rate" in s.error_message.lower()]

    async def test_the_system_prompt_carries_the_chat_rules(self, world, mock_litellm_response):
        _, model = await _turn(world, mock_litellm_response, [])

        system = model.sent[0][0]
        assert system["role"] == "system"
        for phrase in CHAT_RULE_PHRASES:
            assert phrase in system["content"].lower(), phrase
