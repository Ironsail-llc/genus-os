"""A refused tool call says which gate refused it and how to unlock it.

An agent that is told only "Tool 'x' is not available" has two moves left: say
"I can't do that", or go looking for a way round. Neither is what the person
needs. With ``ROBOTHOR_REFUSAL_PAYLOAD_V2`` on, every refusal the engine hands
back keeps its ``error`` text unchanged and adds four keys:

* ``refused``: ``True``, so a refusal is distinguishable from a tool failure;
* ``gate``: one of ``role``, ``flag``, ``allowlist``, ``runtime``, ``policy``;
* ``gate_name``: the specific control (``plan_mode``, ``rbac``, ``GITHUB_TOKEN``);
* ``unlock``: what a person would change for the call to succeed.

With the flag off every payload is exactly what it was, key for key.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, TriggerType

FLAG = "ROBOTHOR_REFUSAL_PAYLOAD_V2"
GATES = {"role", "flag", "allowlist", "runtime", "policy"}
NEW_KEYS = {"refused", "gate", "gate_name", "unlock"}


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(FLAG, "1")


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)


def _assert_v2(payload: dict[str, Any], gate: str) -> None:
    assert payload.get("error"), payload
    assert payload.get("refused") is True, payload
    assert payload.get("gate") == gate, payload
    assert payload["gate"] in GATES
    assert isinstance(payload.get("gate_name"), str) and payload["gate_name"], payload
    assert isinstance(payload.get("unlock"), str) and len(payload["unlock"]) > 10, payload


# ── The helper ────────────────────────────────────────────────────────


class TestRefusalHelper:
    def test_on_it_adds_the_four_keys_and_keeps_the_error(self, flag_on) -> None:
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

    def test_off_it_is_todays_payload(self, flag_off) -> None:
        from robothor.engine.refusal import refusal

        out = refusal("no", gate="role", name="owner_role", unlock="x", denied_by="owner_role")
        assert out == {"error": "no", "denied_by": "owner_role"}

    def test_an_unknown_gate_is_a_programming_error(self, flag_on) -> None:
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
    async def test_on_every_gate_names_itself_and_an_unlock(
        self, flag_on, label, make, gate, legacy
    ) -> None:
        verdict = await make()
        assert verdict.allowed is False, label
        recorded = _recorded(verdict)
        _assert_v2(recorded, gate)
        # The legacy key an operator already reads in the ledger is still there.
        for key, value in legacy.items():
            assert recorded[key] == value
        assert verdict.gate == gate
        assert verdict.unlock == recorded["unlock"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("label", "make", "gate", "legacy"), ADMISSION_CASES)
    async def test_off_every_gate_records_what_it_did_before(
        self, flag_off, label, make, gate, legacy
    ) -> None:
        verdict = await make()
        assert verdict.allowed is False, label
        assert verdict.output == legacy

    @pytest.mark.asyncio
    async def test_the_tools_allowed_unlock_names_the_manifest(self, flag_on) -> None:
        verdict = await _tools_allowed()
        assert "docs/agents/example-agent.yaml" in verdict.unlock
        assert "write_file" in verdict.unlock

    @pytest.mark.asyncio
    async def test_the_system_rbac_unlock_names_the_service_role(self, flag_on) -> None:
        verdict = await _system_rbac()
        assert "'service'" in verdict.unlock


# ── Before-handler refusals (tools/dispatch.py) ───────────────────────


class TestDispatchRefusals:
    @pytest.mark.asyncio
    async def test_user_rbac_denial(self, flag_on) -> None:
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
        _assert_v2(out, "role")
        assert "'member'" in out["unlock"]

    @pytest.mark.asyncio
    async def test_user_rbac_denial_off(self, flag_off) -> None:
        from robothor.engine.tools.dispatch import _refuse_before_handler

        with patch("robothor.engine.permissions.check_tool_permission", return_value="denied"):
            out = await _refuse_before_handler(
                "exec", agent_id="main", tenant_id="t-1", user_id="u-1", user_role="member"
            )
        assert out == {"error": "denied"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("on", [True, False])
    async def test_whitelist_denial(self, monkeypatch, on) -> None:
        from robothor.engine.tools import dispatch

        if on:
            monkeypatch.setenv(FLAG, "1")
        else:
            monkeypatch.delenv(FLAG, raising=False)
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
        if on:
            _assert_v2(out, "allowlist")
        else:
            assert set(out) == {"error", "denied_by_whitelist"}


# ── Handler refusals ──────────────────────────────────────────────────


def _ctx(**kw: Any):
    from robothor.engine.tools.dispatch import ToolContext

    return ToolContext(agent_id=kw.pop("agent_id", "main"), run_id="run-1", **kw)


class TestConfigToolRefusals:
    @pytest.mark.parametrize("on", [True, False])
    def test_not_the_owner(self, monkeypatch, on) -> None:
        from robothor.engine.tools.handlers import config_tools

        if on:
            monkeypatch.setenv(FLAG, "1")
        else:
            monkeypatch.delenv(FLAG, raising=False)
        with patch("robothor.engine.owner_gate.is_verified_owner", return_value=False):
            out = config_tools._owner_denial(_ctx(), "config_get")
        assert out is not None
        assert out["denied_by"] == "owner_role"
        if on:
            _assert_v2(out, "role")
        else:
            assert set(out) == {"error", "denied_by"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("on", [True, False])
    async def test_apply_without_host_exec(self, monkeypatch, on) -> None:
        from robothor.engine import host_execution
        from robothor.engine.tools.handlers import config_tools
        from robothor.settings import operator

        if on:
            monkeypatch.setenv(FLAG, "1")
        else:
            monkeypatch.delenv(FLAG, raising=False)
        monkeypatch.setattr(operator, "validate", lambda row, value: value)
        monkeypatch.setattr(host_execution, "socket_path", lambda: "")
        out = await config_tools._set_apply({"env": "ROBOTHOR_X"}, "1", _ctx())
        assert out["error_type"] == "host_exec_required"
        if on:
            _assert_v2(out, "runtime")
            assert "ROBOTHOR_HOST_EXEC_SOCKET" in out["unlock"]
        else:
            assert set(out) == {"error", "error_type"}


class TestGithubTokenRefusal:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("on", [True, False])
    async def test_missing_token(self, monkeypatch, on) -> None:
        from robothor.engine.tools.handlers import github_api

        if on:
            monkeypatch.setenv(FLAG, "1")
        else:
            monkeypatch.delenv(FLAG, raising=False)
        monkeypatch.setattr(github_api, "_get_token", lambda: "")
        # The unlock names vault_set only where vault_set would be admitted
        # (TestQaRoundUnlocks covers the other cases); this agent holds the tier.
        from robothor.engine.tools.handlers import vault

        monkeypatch.setattr(vault, "credential_tier", lambda agent_id: "operator")
        out = await github_api._github_list_prs({"repo": "octo/example"}, _ctx())
        assert out["error"] == "GITHUB_TOKEN not configured"
        if on:
            _assert_v2(out, "runtime")
            assert out["gate_name"] == "GITHUB_TOKEN"
            assert "vault_set" in out["unlock"]
        else:
            assert out == {"error": "GITHUB_TOKEN not configured"}

    def test_every_missing_token_site_goes_through_one_helper(self) -> None:
        """Eleven sites returned the same bare string; a twelfth must not
        reintroduce it."""
        from pathlib import Path

        import robothor.engine.tools.handlers.github_api as mod

        source = Path(mod.__file__).read_text()
        assert source.count('{"error": "GITHUB_TOKEN not configured"}') == 0


class TestAttachmentRedirectRefusal:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("on", [True, False])
    async def test_a_member_sending_elsewhere(self, monkeypatch, tmp_path, on) -> None:
        from robothor.engine.tools.handlers import attachments

        if on:
            monkeypatch.setenv(FLAG, "1")
        else:
            monkeypatch.delenv(FLAG, raising=False)
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
        if on:
            _assert_v2(out, "role")
        else:
            assert out == {"error": out["error"]}


def test_flag_default_is_off(monkeypatch) -> None:
    monkeypatch.delenv(FLAG, raising=False)
    from robothor.engine.feature_flags import refusal_payload_v2_enabled

    assert refusal_payload_v2_enabled() is False
    monkeypatch.setenv(FLAG, "1")
    assert refusal_payload_v2_enabled() is True
    assert os.environ[FLAG] == "1"


class TestRule22:
    @pytest.fixture(autouse=True)
    def _no_verification(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RUN_VERIFICATION_MODE", "off")

    def test_on_the_rules_tell_the_model_how_to_relay_a_refusal(self, flag_on) -> None:
        from robothor.engine.prompts import behavioral_rules

        rules = behavioral_rules()
        assert "\n22. " in rules
        assert "refused: true" in rules
        assert "unlock" in rules

    def test_off_the_rules_are_unchanged(self, flag_off) -> None:
        from robothor.engine.prompts import BEHAVIORAL_RULES, behavioral_rules

        assert behavioral_rules() == BEHAVIORAL_RULES


class TestRule22OnlyCoversGatedRefusals:
    """The repeat guard already sends ``refused: true`` with no gate or unlock,
    meaning "change the call". Rule 22 must not tell the model to relay a gate
    that result does not have, nor forbid the change it asks for."""

    @pytest.fixture(autouse=True)
    def _no_verification(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RUN_VERIFICATION_MODE", "off")

    def _rule22(self) -> str:
        from robothor.engine.prompts import behavioral_rules

        rules = behavioral_rules()
        return rules[rules.index("\n22. ") :]

    def test_the_rule_applies_only_when_the_result_carries_a_gate(self, flag_on) -> None:
        assert "`refused: true` and a `gate`" in self._rule22()

    def test_a_refusal_without_a_gate_is_a_redirection(self, flag_on) -> None:
        rule = self._rule22()
        assert "without a `gate`" in rule
        assert "change the call" in rule

    def test_the_repeat_guard_payload_has_no_gate(self, flag_on) -> None:
        """Pins the shape rule 22 distinguishes: if the repeat guard ever
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
    async def test_a_privileged_role_is_told_the_rule_not_to_ask_itself(
        self, flag_on, role
    ) -> None:
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
        self, flag_on, monkeypatch
    ) -> None:
        from robothor.engine.tools.handlers import github_api, vault

        monkeypatch.setattr(github_api, "_get_token", lambda: "")
        monkeypatch.delenv("ROBOTHOR_CREDENTIAL_INTAKE", raising=False)
        monkeypatch.setattr(vault, "credential_tier", lambda agent_id: "")
        out = await github_api._github_list_prs({"repo": "octo/example"}, _ctx(user_role="owner"))
        assert "vault_set" not in out["unlock"]
        assert "GITHUB_TOKEN" in out["unlock"]

        monkeypatch.setenv("ROBOTHOR_CREDENTIAL_INTAKE", "enforce")
        out = await github_api._github_list_prs({"repo": "octo/example"}, _ctx(user_role="owner"))
        assert "vault_set" in out["unlock"]

        monkeypatch.delenv("ROBOTHOR_CREDENTIAL_INTAKE")
        monkeypatch.setattr(vault, "credential_tier", lambda agent_id: "operator")
        out = await github_api._github_list_prs({"repo": "octo/example"}, _ctx(user_role="member"))
        assert "vault_set" in out["unlock"]
