"""A refused tool call says which gate refused it and how to unlock it.

An agent that is told only "Tool 'x' is not available" has two moves left: say
"I can't do that", or go looking for a way round. Neither is what the person
needs. Every refusal the engine hands back keeps its ``error`` text unchanged
and adds four keys:

* ``refused``: ``True``, so a refusal is distinguishable from a tool failure;
* ``gate``: one of ``role``, ``flag``, ``allowlist``, ``runtime``, ``policy``;
* ``gate_name``: the specific control (``plan_mode``, ``rbac``, ``GITHUB_TOKEN``);
* ``unlock``: what a person would change for the call to succeed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, TriggerType

GATES = {"role", "flag", "allowlist", "runtime", "policy"}
NEW_KEYS = {"refused", "gate", "gate_name", "unlock"}


def _assert_gated(payload: dict[str, Any], gate: str) -> None:
    assert payload.get("error"), payload
    assert payload.get("refused") is True, payload
    assert payload.get("gate") == gate, payload
    assert payload["gate"] in GATES
    assert isinstance(payload.get("gate_name"), str) and payload["gate_name"], payload
    assert isinstance(payload.get("unlock"), str) and len(payload["unlock"]) > 10, payload


# ── The helper ────────────────────────────────────────────────────────


class TestRefusalHelper:
    def test_it_adds_the_four_keys_and_keeps_the_error(self) -> None:
        from robothor.engine.refusal import refusal

        out = refusal(
            "no", gate="runtime", name="GITHUB_TOKEN", unlock="Store GITHUB_TOKEN in the vault."
        )
        assert out == {
            "error": "no",
            "refused": True,
            "gate": "runtime",
            "gate_name": "GITHUB_TOKEN",
            "unlock": "Store GITHUB_TOKEN in the vault.",
        }

    def test_an_unknown_gate_is_a_programming_error(self) -> None:
        from robothor.engine.refusal import refusal

        with pytest.raises(ValueError):
            refusal("no", gate="vibes", name="x", unlock="y")


# ── Admission gates (tool_admission.py) ───────────────────────────────


def _session(trigger=TriggerType.TELEGRAM, **attrs: Any) -> Any:
    run = SimpleNamespace(id="run-1", steps=[], trigger_type=trigger, tenant_id="t-1")
    return SimpleNamespace(run=run, **attrs)


def _config(**kw: Any) -> AgentConfig:
    return AgentConfig(id="example-agent", name="Example", **kw)


async def _admit(tool: str = "write_file", **kw: Any):
    from robothor.engine.tool_admission import ToolAdmissionMixin

    args = {
        "tc": SimpleNamespace(id="c1"),
        "tool_name": tool,
        "tool_args": {},
        "session": _session(),
        "agent_config": _config(),
        "guardrail_engine": None,
        "hook_registry": None,
        "readonly_mode": False,
        "readonly_tool_set": frozenset(),
        "allowed_tool_set": frozenset(),
    }
    args.update(kw)
    return await ToolAdmissionMixin()._admit_tool_call(**args)


def _recorded(verdict: Any) -> dict[str, Any]:
    """What `_record_refusal` and the code proxy put in the step's tool_output."""
    return {"error": verdict.message, **verdict.output}


class _Stop:
    explained: set[str] = set()

    def remaining(self) -> float:
        return 12.0


async def _plan_mode():
    return await _admit(readonly_mode=True, readonly_tool_set=frozenset({"read_file"}))


async def _tools_allowed():
    return await _admit(allowed_tool_set=frozenset({"read_file"}))


async def _wrapup():
    # write_file is one of the tools wrap-up keeps; exec is withdrawn.
    return await _admit(tool="exec", session=_session(wrapup_stop=_Stop()))


async def _hook():
    from robothor.engine.hook_registry import HookAction, HookResult

    hooks = MagicMock()
    hooks.dispatch = AsyncMock(return_value=HookResult(action=HookAction.BLOCK, reason="no"))
    return await _admit(hook_registry=hooks)


async def _guardrail():
    from robothor.engine.guardrails import GuardrailResult

    engine = MagicMock()
    engine.check_pre_execution.return_value = GuardrailResult(
        allowed=False, action="blocked", reason="nope", guardrail_name="write_path_restrict"
    )
    with (
        patch("robothor.engine.tracking.log_guardrail_event"),
        patch("robothor.engine.tracking.log_tool_event"),
    ):
        return await _admit(guardrail_engine=engine)


async def _system_rbac():
    with (
        patch("robothor.engine.feature_flags.rbac_enforcement_mode", return_value="enforce"),
        patch(
            "robothor.engine.permissions.classify_system_tool_access",
            return_value=("block", "Role 'service' denied 'write_file'"),
        ),
        patch("robothor.engine.tracking.log_guardrail_event"),
    ):
        return await _admit(session=_session(trigger=TriggerType.CRON))


ADMISSION_CASES = [
    ("plan_mode", _plan_mode, "policy", {"guard": "plan_mode"}),
    ("tools_allowed", _tools_allowed, "allowlist", {"guard": "tools_allowed"}),
    ("run_wrapup", _wrapup, "policy", {"guard": "run_wrapup"}),
    ("hook", _hook, "policy", {"hook": "pre_tool_use"}),
    ("guardrail", _guardrail, "policy", {"guardrail": "write_path_restrict"}),
    ("system_rbac", _system_rbac, "role", {"guardrail": "rbac"}),
]


class TestAdmissionGates:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("label", "make", "gate", "legacy"), ADMISSION_CASES)
    async def test_every_gate_names_itself_and_an_unlock(self, label, make, gate, legacy) -> None:
        verdict = await make()
        assert verdict.allowed is False, label
        recorded = _recorded(verdict)
        _assert_gated(recorded, gate)
        # The legacy key an operator already reads in the ledger is still there.
        for key, value in legacy.items():
            assert recorded[key] == value
        assert verdict.gate == gate
        assert verdict.unlock == recorded["unlock"]

    @pytest.mark.asyncio
    async def test_the_tools_allowed_unlock_names_the_manifest(self) -> None:
        verdict = await _tools_allowed()
        assert "docs/agents/example-agent.yaml" in verdict.unlock
        assert "write_file" in verdict.unlock

    @pytest.mark.asyncio
    async def test_the_system_rbac_unlock_names_the_service_role(self) -> None:
        verdict = await _system_rbac()
        assert "'service'" in verdict.unlock


# ── Before-handler refusals (tools/dispatch.py) ───────────────────────


class TestDispatchRefusals:
    @pytest.mark.asyncio
    async def test_user_rbac_denial(self) -> None:
        from robothor.engine.tools.dispatch import _refuse_before_handler

        with patch(
            "robothor.engine.permissions.check_tool_permission",
            return_value="Role 'member' denied 'exec' (pattern: *)",
        ):
            out = await _refuse_before_handler(
                "exec", agent_id="main", tenant_id="t-1", user_id="u-1", user_role="member"
            )
        assert out is not None
        assert out["error"] == "Role 'member' denied 'exec' (pattern: *)"
        _assert_gated(out, "role")
        assert "'member'" in out["unlock"]

    @pytest.mark.asyncio
    async def test_whitelist_denial(self, monkeypatch) -> None:
        from robothor.engine.tools import dispatch

        token = dispatch._thread_tool_whitelist.set(frozenset({"read_file"}))
        try:
            with patch("robothor.engine.permissions.check_tool_permission", return_value=None):
                out = await dispatch._refuse_before_handler(
                    "exec", agent_id="main", tenant_id="t-1", user_id="u-1", user_role="owner"
                )
        finally:
            dispatch._thread_tool_whitelist.reset(token)
        assert out is not None
        assert out["denied_by_whitelist"] is True
        _assert_gated(out, "allowlist")


# ── Handler refusals ──────────────────────────────────────────────────


def _ctx(**kw: Any):
    from robothor.engine.tools.dispatch import ToolContext

    return ToolContext(agent_id=kw.pop("agent_id", "main"), run_id="run-1", **kw)


class TestConfigToolRefusals:
    def test_not_the_owner(self, monkeypatch) -> None:
        from robothor.engine.tools.handlers import config_tools

        with patch("robothor.engine.owner_gate.is_verified_owner", return_value=False):
            out = config_tools._owner_denial(_ctx(), "config_get")
        assert out is not None
        assert out["denied_by"] == "owner_role"
        _assert_gated(out, "role")

    @pytest.mark.asyncio
    async def test_apply_without_host_exec(self, monkeypatch) -> None:
        from robothor.engine import host_execution
        from robothor.engine.tools.handlers import config_tools
        from robothor.settings import operator

        monkeypatch.setattr(operator, "validate", lambda row, value: value)
        monkeypatch.setattr(host_execution, "socket_path", lambda: "")
        out = await config_tools._set_apply({"env": "ROBOTHOR_X"}, "1", _ctx())
        assert out["error_type"] == "host_exec_required"
        _assert_gated(out, "runtime")
        assert "ROBOTHOR_HOST_EXEC_SOCKET" in out["unlock"]


class TestGithubTokenRefusal:
    @pytest.mark.asyncio
    async def test_missing_token(self, monkeypatch) -> None:
        from robothor.engine.tools.handlers import github_api

        monkeypatch.setattr(github_api, "_get_token", lambda tenant_id: "")
        # The unlock names vault_set only where vault_set would be admitted
        # (TestQaRoundUnlocks covers the other cases); this agent holds the tier.
        from robothor.engine.tools.handlers import vault

        monkeypatch.setattr(vault, "credential_tier", lambda agent_id: "operator")
        out = await github_api._github_list_prs({"repo": "octo/example"}, _ctx())
        assert out["error"] == "GITHUB_TOKEN not configured"
        _assert_gated(out, "runtime")
        assert out["gate_name"] == "GITHUB_TOKEN"
        assert "vault_set" in out["unlock"]

    def test_every_missing_token_site_goes_through_one_helper(self) -> None:
        """Eleven sites returned the same bare string; a twelfth must not
        reintroduce it."""
        from pathlib import Path

        import robothor.engine.tools.handlers.github_api as mod

        source = Path(mod.__file__).read_text()
        assert source.count('{"error": "GITHUB_TOKEN not configured"}') == 0


class TestAttachmentRedirectRefusal:
    @pytest.mark.asyncio
    async def test_a_member_sending_elsewhere(self, monkeypatch, tmp_path) -> None:
        from robothor.engine.tools.handlers import attachments

        (tmp_path / "report.txt").write_text("hello")

        async def _route(ctx):
            return "telegram", "100"

        monkeypatch.setattr(attachments, "_originating_route", _route)
        ctx = SimpleNamespace(
            workspace=str(tmp_path),
            agent_id="main",
            run_id="run-1",
            tenant_id="t-1",
            user_id="u-1",
            user_role="member",
            identity=None,
        )
        out = await attachments.send_file({"path": "report.txt", "target": "999"}, ctx)
        assert out["error"].startswith("refused: only an operator-tier agent")
        _assert_gated(out, "role")


class TestRule21:
    @pytest.fixture(autouse=True)
    def _no_verification(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RUN_VERIFICATION_MODE", "off")

    def test_the_rules_tell_the_model_how_to_relay_a_refusal(self) -> None:
        from robothor.engine.prompts import behavioral_rules

        rules = behavioral_rules()
        assert "\n21. " in rules
        assert "refused: true" in rules
        assert "unlock" in rules


class TestRule21OnlyCoversGatedRefusals:
    """The repeat guard already sends ``refused: true`` with no gate or unlock,
    meaning "change the call". Rule 21 must not tell the model to relay a gate
    that result does not have, nor forbid the change it asks for."""

    @pytest.fixture(autouse=True)
    def _no_verification(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RUN_VERIFICATION_MODE", "off")

    def _rule21(self) -> str:
        from robothor.engine.prompts import behavioral_rules

        rules = behavioral_rules()
        return rules[rules.index("\n21. ") :]

    def test_the_rule_applies_only_when_the_result_carries_a_gate(self) -> None:
        assert "`refused: true` and a `gate`" in self._rule21()

    def test_a_refusal_without_a_gate_is_a_redirection(self) -> None:
        rule = self._rule21()
        assert "without a `gate`" in rule
        assert "change the call" in rule

    def test_the_repeat_guard_payload_has_no_gate(self) -> None:
        """Pins the shape rule 21 distinguishes: if the repeat guard ever
        gains a gate, the rule's carve-out needs revisiting."""
        from pathlib import Path

        import robothor.engine.repeat_guard as guard

        source = Path(guard.__file__).read_text()
        assert '"refused": True' in source
        assert '"gate"' not in source


class TestQaRoundUnlocks:
    """Unlocks must name something the reader can actually do."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", ["admin", "owner"])
    async def test_a_privileged_role_is_told_the_rule_not_to_ask_itself(self, role) -> None:
        from robothor.engine.tools.dispatch import _refuse_before_handler

        denial = f"Role '{role}' denied 'config_get' (pattern: config_*)"
        with patch("robothor.engine.permissions.check_tool_permission", return_value=denial):
            out = await _refuse_before_handler(
                "config_get", agent_id="main", tenant_id="t-1", user_id="u-1", user_role=role
            )
        assert out is not None
        assert "An owner or admin can do it" not in out["unlock"]
        assert "config_*" in out["unlock"]
        assert "role_permissions" in out["unlock"]

    @pytest.mark.asyncio
    async def test_github_unlock_names_vault_set_only_when_it_would_be_admitted(
        self, monkeypatch
    ) -> None:
        from robothor.engine.tools.handlers import github_api, vault

        monkeypatch.setattr(github_api, "_get_token", lambda tenant_id: "")
        monkeypatch.setattr(vault, "credential_tier", lambda agent_id: "")
        monkeypatch.delattr(vault, "CREDENTIAL_INTAKE", raising=False)
        out = await github_api._github_list_prs({"repo": "octo/example"}, _ctx(user_role="owner"))
        assert "vault_set" not in out["unlock"]
        assert "GITHUB_TOKEN" in out["unlock"]

        monkeypatch.setattr(vault, "CREDENTIAL_INTAKE", True, raising=False)
        out = await github_api._github_list_prs({"repo": "octo/example"}, _ctx(user_role="owner"))
        assert "vault_set" in out["unlock"]

        monkeypatch.delattr(vault, "CREDENTIAL_INTAKE", raising=False)
        monkeypatch.setattr(vault, "credential_tier", lambda agent_id: "operator")
        out = await github_api._github_list_prs({"repo": "octo/example"}, _ctx(user_role="member"))
        assert "vault_set" in out["unlock"]

    @pytest.mark.asyncio
    async def test_github_unlock_ignores_an_intake_env_this_build_does_not_read(
        self, monkeypatch
    ) -> None:
        from robothor.engine.tools.handlers import github_api, vault

        monkeypatch.setattr(github_api, "_get_token", lambda tenant_id: "")
        monkeypatch.setattr(vault, "credential_tier", lambda agent_id: "")
        monkeypatch.setenv("ROBOTHOR_CREDENTIAL_INTAKE", "enforce")
        monkeypatch.delattr(vault, "CREDENTIAL_INTAKE", raising=False)
        out = await github_api._github_list_prs({"repo": "octo/example"}, _ctx(user_role="owner"))
        assert "vault_set" not in out["unlock"]


class TestDeniedByPatternNamesThePattern:
    """QA D14: the manifest holds the pattern, so the unlock must name it."""

    @pytest.mark.asyncio
    async def test_a_pattern_deny_names_the_pattern(self) -> None:
        config = _config(tools_denied=["claude_code_*"])
        verdict = await _admit(
            tool="claude_code_start", agent_config=config, allowed_tool_set=frozenset({"read_file"})
        )
        assert "'claude_code_*'" in verdict.unlock
        assert "'claude_code_start' from tools_denied" not in verdict.unlock
        assert "matches 'claude_code_start'" in verdict.unlock

    @pytest.mark.asyncio
    async def test_an_exact_deny_still_names_the_tool(self) -> None:
        config = _config(tools_denied=["exec"])
        verdict = await _admit(
            tool="exec", agent_config=config, allowed_tool_set=frozenset({"read_file"})
        )
        assert "removes 'exec' from tools_denied" in verdict.unlock


async def _admit_deferred(names: set[str], **kw: Any):
    """Admit one call on a deferred run: ``names`` is what ``tool_call`` reaches."""
    from robothor.engine.tools.dispatch import clear_deferred_allowed, set_deferred_allowed

    token = set_deferred_allowed(frozenset(names))
    try:
        return await _admit(**kw)
    finally:
        clear_deferred_allowed(token)


class TestADeferredToolIsNotAnAllowlistRefusal:
    """A granted tool left out of this turn's schemas because the toolset is
    deferred is reachable through ``tool_call``. Its direct call is refused, but
    the unlock is to call it that way, not a manifest change: telling the agent
    (and through rule 21 the person) that an operator must grant a tool the
    agent already has is the false unlock this change exists to remove."""

    @pytest.mark.asyncio
    async def test_a_granted_deferred_tool_is_told_to_use_tool_call(self) -> None:
        verdict = await _admit_deferred(
            {"read_file", "write_file", "tool_call"},
            tool="write_file",
            allowed_tool_set=frozenset({"read_file"}),
        )
        assert verdict.allowed is False
        assert verdict.gate == "policy"
        assert verdict.output["gate_name"] == "deferred"
        assert "tool_call" in verdict.unlock
        assert "'write_file'" in verdict.unlock
        assert "tools_allowed" not in verdict.unlock
        assert "operator" not in verdict.unlock

    @pytest.mark.asyncio
    async def test_the_unlock_promises_only_what_this_gate_knows(self) -> None:
        """This gate knows the tool is in the deferred grant, so the manifest
        needs no change. It cannot know what admission, hooks, guardrails or
        RBAC will say to the wrapped call, so it must not promise that no one
        needs to be asked: rule 21 relays this sentence to the person."""
        verdict = await _admit_deferred(
            {"read_file", "write_file", "tool_call"},
            tool="write_file",
            allowed_tool_set=frozenset({"read_file"}),
        )
        assert "manifest" in verdict.unlock
        assert "asked" not in verdict.unlock

    @pytest.mark.asyncio
    async def test_a_tool_outside_the_grant_is_still_an_allowlist_refusal(self) -> None:
        verdict = await _admit_deferred(
            {"read_file", "tool_call"}, tool="write_file", allowed_tool_set=frozenset({"read_file"})
        )
        assert verdict.gate == "allowlist"
        assert verdict.output["gate_name"] == "tools_allowed"
        assert "tools_allowed" in verdict.unlock
        assert "tool_call" not in verdict.unlock

    @pytest.mark.asyncio
    async def test_the_error_text_is_unchanged_for_a_deferred_tool(self) -> None:
        verdict = await _admit_deferred(
            {"read_file", "write_file", "tool_call"},
            tool="write_file",
            allowed_tool_set=frozenset({"read_file"}),
        )
        assert verdict.output["guard"] == "tools_allowed"
        assert verdict.message == "Tool 'write_file' is not available to this agent."


class TestASubAgentDoesNotInheritItsParentsDeferredGrant:
    """``spawn_agent`` awaits the child's run inline in the parent's task, so
    the child starts with the parent's context, deferred allow-set included. A
    child that is not deferred must not be told a tool it was never granted is
    "granted but deferred", and the parent's set must be intact once the child
    has finished."""

    @staticmethod
    def _registry(defer: bool, grant: set[str]):
        return SimpleNamespace(
            should_defer=lambda config: defer,
            deferred_whitelist=lambda config: frozenset(grant),
        )

    @pytest.mark.asyncio
    async def test_a_non_deferred_child_of_a_deferred_parent_gets_the_allowlist_refusal(
        self,
    ) -> None:
        from robothor.engine.tools.dispatch import (
            clear_deferred_allowed,
            get_deferred_allowed,
            set_deferred_allowed,
        )
        from robothor.engine.toolset_prep import publish_toolset, withdraw_toolset

        parent = frozenset({"exec", "read_file", "tool_call", "tool_search"})
        parent_token = set_deferred_allowed(parent)
        try:
            child = _config(tools_allowed=["read_file"])
            tokens = publish_toolset(self._registry(False, set()), child, ["read_file"])
            try:
                verdict = await _admit(
                    tool="exec", agent_config=child, allowed_tool_set=frozenset({"read_file"})
                )
                assert verdict.gate == "allowlist"
                assert verdict.output["gate_name"] == "tools_allowed"
                assert "tool_call" not in verdict.unlock
                assert get_deferred_allowed() is None
            finally:
                withdraw_toolset(tokens)
            assert get_deferred_allowed() == parent
        finally:
            clear_deferred_allowed(parent_token)

    @pytest.mark.asyncio
    async def test_a_deferred_child_reads_its_own_grant_not_its_parents(self) -> None:
        from robothor.engine.tools.dispatch import (
            clear_deferred_allowed,
            get_deferred_allowed,
            set_deferred_allowed,
        )
        from robothor.engine.toolset_prep import publish_toolset, withdraw_toolset

        parent = frozenset({"exec", "read_file", "tool_call"})
        parent_token = set_deferred_allowed(parent)
        try:
            child = _config(tools_allowed=["read_file", "write_file"])
            registry = self._registry(True, {"read_file", "write_file", "tool_call"})
            tokens = publish_toolset(registry, child, ["read_file", "write_file"])
            try:
                exec_verdict = await _admit(
                    tool="exec", agent_config=child, allowed_tool_set=frozenset({"read_file"})
                )
                assert exec_verdict.gate == "allowlist"
                write = await _admit(
                    tool="write_file", agent_config=child, allowed_tool_set=frozenset({"read_file"})
                )
                assert write.output["gate_name"] == "deferred"
            finally:
                withdraw_toolset(tokens)
            assert get_deferred_allowed() == parent
        finally:
            clear_deferred_allowed(parent_token)
