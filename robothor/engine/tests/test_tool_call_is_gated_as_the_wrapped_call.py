"""A ``tool_call`` is admitted, and checked afterwards, as the call it wraps.

Found by review on 2026-11-02. With deferred tool loading on by default, a
tool outside an agent's core is reachable only as ``tool_call(name=X,
arguments=A)``. Every control keyed on a tool's NAME saw the wrapper instead:
the PRE_TOOL_USE hook, the guardrail engine (``human_approval``,
``exec_allowlist``, ``inbound_only``, ``desktop_safety`` and the rest), the
guardrail event log, the system-run RBAC check, and after the call the
post-execution guardrail and the POST_TOOL_USE hook. Nothing matched
``tool_call``, so an agent whose manifest gates ``issue_refund`` behind human
approval could send ``tool_call(name="issue_refund", ...)`` and the refund
went out unapproved. The wrapper's handler checks the deferred allow-list and
dispatch applies RBAC, but neither runs the guardrail engine or the hooks.

The rule: every gate judges the wrapped call, ``X`` with ``A``, exactly as it
would judge a direct call of ``X``. The verdict stays the wrapper's own, so
the model reads the refusal as the result of the ``tool_call`` it sent, and a
hook that rewrites the arguments rewrites the wrapped call's arguments. A
wrapper that names no runnable tool is judged as the wrapper, and the handler
refuses it as before. A direct call is judged exactly as it always was.
"""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import tool_admission
from robothor.engine.guardrails import GuardrailEngine, GuardrailResult
from robothor.engine.hook_registry import HookAction, HookEvent, HookResult
from robothor.engine.models import AgentConfig, DeliveryMode, TriggerType
from robothor.engine.tool_admission import ToolAdmissionMixin

AGENT = "steward"
REFUND = {"order_id": "o-1", "amount": 50}


class _Run:
    def __init__(self, trigger_type: Any = "manual") -> None:
        self.id = "run-1"
        self.steps: list[Any] = []
        self.tenant_id = "default"
        self.trigger_type = trigger_type


class _Session:
    def __init__(self, trigger_type: Any = "manual") -> None:
        self.run = _Run(trigger_type)
        self.run_id = "run-1"


class _AgentConfig:
    id = AGENT
    service_role = "service"
    human_approval_timeout = 1.0
    human_approval_fail_open = False


class _Admitter(ToolAdmissionMixin):
    """The mixin's gates, with nothing else of the runner attached."""


class _Hooks:
    """A hook registry that records what it was shown and answers ``result``."""

    def __init__(self, result: HookResult | None = None) -> None:
        self.result = result or HookResult()
        self.seen: list[tuple[str, str, dict[str, Any]]] = []

    async def dispatch(self, event: Any, ctx: Any) -> HookResult:
        self.seen.append((str(event), ctx.tool_name, dict(ctx.tool_args)))
        return self.result


class _RecordingEngine:
    """A guardrail engine that records the call it judged and allows it."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict[str, Any]]] = []

    def check_pre_execution(self, tool_name: str, tool_args: dict[str, Any], **_: Any) -> Any:
        self.seen.append((tool_name, dict(tool_args)))
        return GuardrailResult()


def _wrap(name: Any, arguments: Any = None) -> dict[str, Any]:
    args: dict[str, Any] = {"name": name}
    if arguments is not None:
        args["arguments"] = arguments
    return args


async def _admit(
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    guardrail_engine: Any = None,
    hook_registry: Any = None,
    session: Any = None,
) -> Any:
    return await _Admitter()._admit_tool_call(
        tc=MagicMock(id="call_1"),
        tool_name=tool_name,
        tool_args=tool_args,
        session=session or _Session(),
        agent_config=_AgentConfig(),  # type: ignore[arg-type]
        guardrail_engine=guardrail_engine,
        hook_registry=hook_registry,
        readonly_mode=False,
        readonly_tool_set=frozenset(),
        allowed_tool_set=frozenset(),
    )


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every guardrail event admission records, kept off the database."""
    logged: list[dict[str, Any]] = []
    monkeypatch.setattr(tool_admission, "_log_guardrail_event", lambda **k: logged.append(k))
    monkeypatch.setattr("robothor.engine.tracking.log_tool_event", lambda **_k: None)
    return logged


def _approval_engine() -> GuardrailEngine:
    engine = GuardrailEngine(enabled_policies=["human_approval"])
    engine.set_human_approval_patterns(AGENT, ["issue_refund"])
    return engine


def _approver(monkeypatch: pytest.MonkeyPatch, *, approves: bool) -> list[dict[str, Any]]:
    asked: list[dict[str, Any]] = []

    class _Manager:
        @staticmethod
        async def request_approval(**kwargs: Any) -> bool:
            asked.append(kwargs)
            return approves

    monkeypatch.setattr(
        "robothor.engine.permission_escalation.get_permission_manager", lambda: _Manager()
    )
    return asked


# ── human approval ────────────────────────────────────────────────────


async def test_human_approval_asks_about_the_wrapped_tool(
    monkeypatch: pytest.MonkeyPatch, events: list[Any]
) -> None:
    asked = _approver(monkeypatch, approves=False)
    wrapper = _wrap("issue_refund", REFUND)

    verdict = await _admit("tool_call", wrapper, guardrail_engine=_approval_engine())

    assert [(a["tool_name"], a["tool_args"]) for a in asked] == [("issue_refund", REFUND)], (
        "the operator must be asked about the refund, not about tool_call"
    )
    assert verdict.allowed is False
    assert verdict.tool_args == wrapper, "the refusal is the wrapper's own"


async def test_an_approved_wrapped_call_runs_as_the_wrapper(
    monkeypatch: pytest.MonkeyPatch, events: list[Any]
) -> None:
    asked = _approver(monkeypatch, approves=True)
    wrapper = _wrap("issue_refund", json.dumps(REFUND))

    verdict = await _admit("tool_call", wrapper, guardrail_engine=_approval_engine())

    assert [a["tool_name"] for a in asked] == ["issue_refund"]
    assert verdict.allowed is True
    assert verdict.tool_args == wrapper


async def test_the_wrapped_denial_reads_as_the_direct_one(
    monkeypatch: pytest.MonkeyPatch, events: list[Any]
) -> None:
    _approver(monkeypatch, approves=False)
    engine = _approval_engine()

    direct = await _admit("issue_refund", dict(REFUND), guardrail_engine=engine)
    wrapped = await _admit("tool_call", _wrap("issue_refund", REFUND), guardrail_engine=engine)

    assert (wrapped.allowed, wrapped.message, wrapped.output) == (
        direct.allowed,
        direct.message,
        direct.output,
    )
    assert (wrapped.escalate, wrapped.count_as_iteration_error) == (
        direct.escalate,
        direct.count_as_iteration_error,
    )


# ── blocking guardrails ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("policy", "inner", "arguments"),
    [
        ("inbound_only", "gws_gmail_send", {"to": "alice@example.com", "body": "hi"}),
        ("exec_allowlist", "exec", {"command": "rm -rf /tmp/x"}),
        ("desktop_safety", "desktop_launch", {"app": "xterm"}),
    ],
)
async def test_a_blocking_guardrail_blocks_the_wrapped_call(
    events: list[dict[str, Any]], policy: str, inner: str, arguments: dict[str, Any]
) -> None:
    engine = GuardrailEngine(enabled_policies=[policy])
    engine._exec_allowlists[AGENT] = [re.compile(r"^git status$")]
    wrapper = _wrap(inner, arguments)

    direct = await _admit(inner, dict(arguments), guardrail_engine=engine)
    direct_events = list(events)
    events.clear()
    wrapped = await _admit("tool_call", wrapper, guardrail_engine=engine)

    assert direct.allowed is False, "the fixture must block the direct call"
    assert wrapped.allowed is False, f"{policy} must block {inner} reached through tool_call"
    assert wrapped.output == {"guardrail": policy}
    assert wrapped.message == direct.message
    assert wrapped.escalate is True
    assert wrapped.tool_args == wrapper
    assert [e["tool_name"] for e in events] == [inner], "the event log names the wrapped tool"
    assert [e["tool_name"] for e in direct_events] == [inner]


# ── the lifecycle hook ────────────────────────────────────────────────


async def test_the_pre_tool_hook_is_shown_the_wrapped_call() -> None:
    hooks = _Hooks()
    verdict = await _admit("tool_call", _wrap("issue_refund", REFUND), hook_registry=hooks)

    assert hooks.seen == [(str(HookEvent.PRE_TOOL_USE), "issue_refund", REFUND)]
    assert verdict.allowed is True


async def test_a_hook_block_refuses_the_wrapper() -> None:
    hooks = _Hooks(HookResult(action=HookAction.BLOCK, reason="refunds are paused"))
    wrapper = _wrap("issue_refund", REFUND)

    verdict = await _admit("tool_call", wrapper, hook_registry=hooks)

    assert verdict.allowed is False
    assert verdict.output == {"hook": "pre_tool_use"}
    assert "refunds are paused" in verdict.message
    assert verdict.tool_args == wrapper


async def test_a_hook_rewrite_is_wrapped_back_into_the_call() -> None:
    hooks = _Hooks(HookResult(action=HookAction.MODIFY, modified_args={"order_id": "o-1"}))
    engine = _RecordingEngine()

    verdict = await _admit(
        "tool_call",
        _wrap("issue_refund", REFUND),
        hook_registry=hooks,
        guardrail_engine=engine,
    )

    assert verdict.allowed is True
    assert verdict.tool_args == {"name": "issue_refund", "arguments": {"order_id": "o-1"}}, (
        "the handler must run the rewritten call, still as a tool_call"
    )
    assert engine.seen == [("issue_refund", {"order_id": "o-1"})], (
        "the guardrails judge the call the hook rewrote"
    )


# ── system-run RBAC ───────────────────────────────────────────────────


async def test_system_run_rbac_judges_the_wrapped_tool() -> None:
    judged: list[str] = []

    def classify(_role: str, _tenant: str, tool_name: str, _mode: str) -> tuple[str, str]:
        judged.append(tool_name)
        return "allow", ""

    with patch("robothor.engine.permissions.classify_system_tool_access", classify):
        verdict = await _admit(
            "tool_call",
            _wrap("issue_refund", REFUND),
            session=_Session(trigger_type=TriggerType.CRON),
        )

    assert judged == ["issue_refund"]
    assert verdict.allowed is True


# ── what must not change ──────────────────────────────────────────────


async def test_a_direct_call_is_judged_exactly_as_before(
    monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]]
) -> None:
    asked = _approver(monkeypatch, approves=False)
    hooks = _Hooks()

    verdict = await _admit(
        "issue_refund", dict(REFUND), guardrail_engine=_approval_engine(), hook_registry=hooks
    )

    assert hooks.seen == [(str(HookEvent.PRE_TOOL_USE), "issue_refund", REFUND)]
    assert [(a["tool_name"], a["tool_args"]) for a in asked] == [("issue_refund", REFUND)]
    assert verdict.allowed is False
    assert verdict.message.startswith(
        "Denied by operator (human_approval): Tool 'issue_refund' requires human approval. "
    )
    assert verdict.output == {}
    assert verdict.escalate is False
    assert verdict.count_as_iteration_error is False
    assert verdict.tool_args == REFUND


@pytest.mark.parametrize(
    "wrapper",
    [
        {"arguments": {"order_id": "o-1"}},
        {"name": 7, "arguments": {}},
        {"name": "tool_call", "arguments": {"name": "issue_refund"}},
    ],
)
async def test_a_wrapper_naming_no_runnable_tool_is_judged_as_the_wrapper(
    monkeypatch: pytest.MonkeyPatch, events: list[Any], wrapper: dict[str, Any]
) -> None:
    asked = _approver(monkeypatch, approves=False)
    hooks = _Hooks()

    verdict = await _admit(
        "tool_call", wrapper, guardrail_engine=_approval_engine(), hook_registry=hooks
    )

    assert hooks.seen == [(str(HookEvent.PRE_TOOL_USE), "tool_call", wrapper)]
    assert asked == []
    assert verdict.allowed is True, "the handler refuses it, with how to send it"
    assert verdict.tool_args == wrapper


# ── after the call: through the real loop ─────────────────────────────


def _tc(name: str, args: dict[str, Any]) -> Any:
    tc = MagicMock()
    tc.id = "call_1"
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    return tc


def _response(content: Any = None, tool_calls: Any = None) -> Any:
    response = MagicMock()
    response.model = "test-model"
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    return response


async def _loop_one_call(engine_config: Any, tool_name: str, tool_args: dict[str, Any]) -> Any:
    """One call through ``AgentRunner.execute``; returns what was observed."""
    from robothor.engine.runner import AgentRunner

    with patch("robothor.engine.runner.get_registry") as get_registry:
        registry = MagicMock()
        registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": tool_name}}
        ]
        registry.get_tool_names.return_value = [tool_name]
        registry.execute = AsyncMock(return_value={"ok": True})
        get_registry.return_value = registry
        runner = AgentRunner(engine_config)
        runner.registry = registry

    agent_config = AgentConfig(
        id=AGENT,
        name="Steward",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        timeout_seconds=30,
        delivery_mode=DeliveryMode.NONE,
        planning_enabled=False,
        scratchpad_enabled=False,
        error_feedback=False,
    )
    agent_config.guardrails = ["no_sensitive_data"]
    turns = {"n": 0}

    async def completion(**_kwargs: Any) -> Any:
        turns["n"] += 1
        if turns["n"] == 1:
            return _response(tool_calls=[_tc(tool_name, tool_args)])
        return _response(content="done")

    hooks = _Hooks()
    post_checked: list[str] = []

    def check_post(_self: Any, name: str, _result: Any) -> GuardrailResult:
        post_checked.append(name)
        return GuardrailResult()

    with (
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=completion),
        patch("robothor.engine.hook_registry.get_hook_registry", return_value=hooks),
        patch.object(GuardrailEngine, "check_post_execution", check_post),
    ):
        run = await runner.execute(AGENT, "go", agent_config=agent_config)
    post_hook = [(n, a) for e, n, a in hooks.seen if e == str(HookEvent.POST_TOOL_USE)]
    return run, post_checked, post_hook


@pytest.mark.usefixtures("_mock_run_persistence")
async def test_post_execution_checks_see_the_wrapped_call(engine_config: Any) -> None:
    run, post_checked, post_hook = await _loop_one_call(
        engine_config, "tool_call", _wrap("read_file", {"path": "notes.md"})
    )

    assert post_checked == ["read_file"], "the result guardrail judges the wrapped tool"
    assert post_hook == [("read_file", {"path": "notes.md"})]
    assert [s for s in run.steps if getattr(s, "tool_name", None) == "tool_call"], (
        "the step stays the wrapper's own"
    )


@pytest.mark.usefixtures("_mock_run_persistence")
async def test_post_execution_of_a_direct_call_is_unchanged(engine_config: Any) -> None:
    _, post_checked, post_hook = await _loop_one_call(
        engine_config, "read_file", {"path": "notes.md"}
    )

    assert post_checked == ["read_file"]
    assert post_hook == [("read_file", {"path": "notes.md"})]
