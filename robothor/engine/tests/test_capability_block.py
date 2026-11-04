"""The system prompt says what this agent can do here, and what gates the rest.

Without it the model learns its abilities only from tool schemas, and an agent
that has `exec`, `write_file` and the git tools still tells the person "I can't
edit code". Every system prompt carries one block, built from the agent's
granted tools and the engine's settings, after the bootstrap files.

The block must be SESSION-STABLE: it reads no clock, no vault and no per-turn
state, so the static body stays byte-identical from one turn to the next (the
property prompt-cache layout v2 depends on). It is part of the prompt cache
stamp, so a settings change that alters it rebuilds the prompt.
"""

from __future__ import annotations

from datetime import datetime as _real_datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from robothor.engine import config as engine_config_module
from robothor.engine.models import AgentConfig

if TYPE_CHECKING:
    from pathlib import Path

HEADER = "## What you can do here"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from robothor.settings import reset_settings

    monkeypatch.delenv("ROBOTHOR_CODING_REPO_ROOTS", raising=False)
    monkeypatch.delenv("ROBOTHOR_HOST_EXEC_SOCKET", raising=False)
    engine_config_module._prompt_cache.clear()
    reset_settings()
    yield
    engine_config_module._prompt_cache.clear()
    reset_settings()


def _agent(tmp_path: Path, **kw) -> AgentConfig:
    (tmp_path / "brain").mkdir(exist_ok=True)
    (tmp_path / "brain" / "example.md").write_text("You are example-agent.")
    defaults = {
        "id": "example-agent",
        "name": "Example",
        "instruction_file": "brain/example.md",
        "tools_allowed": ["exec", "write_file", "read_file", "git_commit", "create_pull_request"],
        "timezone": "UTC",
    }
    defaults.update(kw)
    return AgentConfig(**defaults)


def _static(config: AgentConfig, workspace: Path, minute: int = 41) -> str:
    class _Frozen(_real_datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return _real_datetime(2026, 3, 4, 9, minute, tzinfo=tz)

    with patch.object(engine_config_module, "datetime", _Frozen):
        return engine_config_module.build_system_prompt(config, workspace).static_body


class TestTheBlock:
    def test_the_block_lists_what_the_agent_was_granted(self, tmp_path) -> None:
        body = _static(_agent(tmp_path), tmp_path)
        assert HEADER in body
        block = body[body.index(HEADER) :]
        for capability in ("exec", "write_files", "git_pr"):
            assert f"- {capability}:" in block, capability

    def test_it_sits_after_the_instructions(self, tmp_path) -> None:
        body = _static(_agent(tmp_path), tmp_path)
        assert body.index("You are example-agent.") < body.index(HEADER)

    def test_a_tool_not_granted_is_gated_with_the_manifest_unlock(self, tmp_path) -> None:
        body = _static(_agent(tmp_path, tools_allowed=["read_file"]), tmp_path)
        block = body[body.index(HEADER) :]
        gated = block[block.index("Gated") :]
        assert "- exec:" in gated
        assert "docs/agents/example-agent.yaml" in gated

    def test_coding_jobs_without_repo_roots_is_a_runtime_gate(self, tmp_path) -> None:
        config = _agent(tmp_path, tools_allowed=["claude_code_start", "read_file"])
        body = _static(config, tmp_path)
        gated = body[body.index("Gated") :]
        assert "- coding_jobs:" in gated
        assert "ROBOTHOR_CODING_REPO_ROOTS" in gated

    def test_it_names_who_the_agent_acts_for(self, tmp_path) -> None:
        body = _static(_agent(tmp_path, service_role="example-role"), tmp_path)
        assert "'example-role'" in body[body.index(HEADER) :]

    def test_it_tells_the_model_never_to_claim_a_false_inability(self, tmp_path) -> None:
        block = _static(_agent(tmp_path), tmp_path)
        block = block[block.index(HEADER) :]
        assert "Never say you cannot" in block

    def test_two_turns_a_minute_apart_have_a_byte_identical_static_body(self, tmp_path) -> None:
        config = _agent(tmp_path)
        first = _static(config, tmp_path, minute=41)
        second = _static(config, tmp_path, minute=42)
        assert first == second

    def test_a_rebuild_without_the_cache_is_byte_identical(self, tmp_path) -> None:
        config = _agent(tmp_path)
        first = _static(config, tmp_path)
        engine_config_module._prompt_cache.clear()
        assert _static(config, tmp_path) == first

    def test_the_block_is_part_of_the_cache_stamp(self, tmp_path, monkeypatch) -> None:
        """A settings change that alters the block rebuilds the prompt, even
        though no instruction file's mtime moved."""
        from robothor.settings import reset_settings

        config = _agent(tmp_path, tools_allowed=["claude_code_start", "read_file"])
        before = _static(config, tmp_path)
        monkeypatch.setenv("ROBOTHOR_CODING_REPO_ROOTS", str(tmp_path))
        reset_settings()
        after = _static(config, tmp_path)
        assert before != after
        assert "- coding_jobs:" in after[after.index(HEADER) : after.index("Gated")]

    def test_the_block_reads_no_vault(self, tmp_path) -> None:
        with patch("robothor.secrets.resolve_secret", side_effect=AssertionError("vault read")):
            _static(_agent(tmp_path), tmp_path)


class TestSharedAssessment:
    """The prompt and the doctor answer from one pure function."""

    def test_assess_is_pure_and_ordered(self) -> None:
        from robothor.engine.capabilities import Facts, assess

        facts = Facts(agent_id="example-agent", service_role="service")
        first = assess({"exec", "write_file"}, facts)
        assert first == assess({"write_file", "exec"}, facts)
        keys = [c.key for c in first]
        assert keys == sorted(keys, key=keys.index)  # stable, declared order
        assert {"exec", "write_files"} <= {c.key for c in first if c.available}

    def test_a_missing_token_fact_gates_git_pr(self) -> None:
        from robothor.engine.capabilities import Facts, assess

        rows = assess(
            {"git_commit", "create_pull_request"},
            # The tier is what makes vault_set the unlock (QA round, D1).
            Facts(
                agent_id="a", service_role="service", github_token=False, credential_tier="operator"
            ),
        )
        git = next(c for c in rows if c.key == "git_pr")
        assert git.available is False
        assert git.gate == "runtime"
        assert git.gate_name == "GITHUB_TOKEN"
        assert "vault_set" in git.unlock


def test_the_block_says_to_relay_a_refusal(tmp_path) -> None:
    """Every gated refusal names its gate and unlock, so the block says so."""
    block = _static(_agent(tmp_path), tmp_path)
    block = block[block.index(HEADER) :]
    assert "names the gate and the unlock" in block
    assert "Never say you cannot" in block


# ── QA round: vault_set and coding jobs follow what actually admits them ──
#
# Credential intake and the coding workspace are separate features. The report
# counts them as admitting anything only when the running tree has them: the
# vault handler's intake gate and the coding settings' ``workspace`` field. An
# environment variable by itself is not a gate.


def _without_intake(monkeypatch) -> None:
    """This build has no credential intake, whichever tree the test runs on."""
    from robothor.engine.tools.handlers import vault

    monkeypatch.delattr(vault, "CREDENTIAL_INTAKE", raising=False)


def _with_intake(monkeypatch) -> None:
    from robothor.engine.tools.handlers import vault

    monkeypatch.setattr(vault, "CREDENTIAL_INTAKE", True, raising=False)


class TestVaultSetFollowsTheCredentialGate:
    """``vault_set`` is refused unless the manifest declares the operator
    credential tier, or credential intake (where this build has it) serves an
    owner's or admin's chat with the default chat agent. Granting the
    tool is not enough."""

    def _vault(self, **facts):
        from robothor.engine.capabilities import Facts, assess

        rows = assess({"vault_set", "git_commit"}, Facts(**facts))
        return {c.key: c for c in rows}

    def test_without_intake_in_this_build_main_is_gated_on_the_tier_only(self) -> None:
        rows = self._vault(agent_id="main")
        vault = rows["vault_set"]
        assert vault.available is False
        assert vault.gate == "allowlist"
        assert vault.gate_name == "credential_tier"
        assert "credentials: operator" in vault.unlock
        assert "CREDENTIAL_INTAKE" not in vault.unlock

    def test_another_agent_without_a_tier_is_gated_on_the_tier(self) -> None:
        vault = self._vault(agent_id="example-agent", credential_intake=True)["vault_set"]
        assert vault.available is False
        assert vault.gate_name == "credential_tier"
        assert "credentials: operator" in vault.unlock

    def test_the_operator_tier_makes_it_available(self) -> None:
        rows = self._vault(agent_id="example-agent", credential_tier="operator")
        assert rows["vault_set"].available is True
        assert "vault_set" in rows["git_pr"].note

    def test_intake_makes_it_available_in_an_owner_or_admin_chat(self) -> None:
        vault = self._vault(agent_id="main", credential_intake=True)["vault_set"]
        assert vault.available is True
        assert "owner or admin" in vault.note

    def test_intake_follows_the_default_chat_agent_not_the_name_main(self) -> None:
        moved = {"default_chat_agent": "example-agent", "credential_intake": True}
        assert self._vault(agent_id="example-agent", **moved)["vault_set"].available is True
        assert self._vault(agent_id="main", **moved)["vault_set"].available is False

    def test_the_probe_reads_the_vault_handler(self, monkeypatch) -> None:
        from robothor.engine import capabilities

        _without_intake(monkeypatch)
        assert capabilities.credential_intake() is False
        _with_intake(monkeypatch)
        assert capabilities.credential_intake() is True

    def test_the_intake_env_alone_admits_nothing_without_intake_in_the_build(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ROBOTHOR_CREDENTIAL_INTAKE", "enforce")
        _without_intake(monkeypatch)
        config = _agent(tmp_path, id="main", tools_allowed=["vault_set", "read_file"])
        block = _static(config, tmp_path)
        block = block[block.index(HEADER) :]
        assert "- vault_set:" in block[block.index("Gated") :]
        assert "ROBOTHOR_CREDENTIAL_INTAKE" not in block

    def test_the_prompt_reads_the_intake_probe(self, tmp_path, monkeypatch) -> None:
        _without_intake(monkeypatch)
        config = _agent(tmp_path, id="main", tools_allowed=["vault_set", "read_file"])
        body = _static(config, tmp_path)
        assert "- vault_set:" in body[body.index("Gated") :]
        engine_config_module._prompt_cache.clear()
        _with_intake(monkeypatch)
        body = _static(config, tmp_path)
        assert "- vault_set:" in body[body.index(HEADER) : body.index("Gated")]


class TestCodingJobsAcceptTheCodingWorkspace:
    """Where this build's coding settings declare a workspace, it is an
    implicit repo root: with it set, a job starts. Where they do not, only
    ``ROBOTHOR_CODING_REPO_ROOTS`` admits a job and only it is named."""

    def test_the_workspace_alone_satisfies_the_roots(self) -> None:
        from robothor.engine.capabilities import Facts, assess

        rows = {
            c.key: c
            for c in assess(
                {"claude_code_start"}, Facts(agent_id="main", coding_workspace="/srv/coding")
            )
        }
        assert rows["coding_jobs"].available is True

    def test_with_neither_set_both_are_named(self) -> None:
        from robothor.engine.capabilities import Facts, assess

        rows = {
            c.key: c
            for c in assess({"claude_code_start"}, Facts(agent_id="main", coding_workspace=""))
        }
        coding = rows["coding_jobs"]
        assert coding.available is False
        assert "ROBOTHOR_CODING_REPO_ROOTS" in coding.unlock
        assert "ROBOTHOR_CODING_WORKSPACE" in coding.unlock

    def test_without_a_workspace_in_this_build_only_the_roots_are_named(self) -> None:
        from robothor.engine.capabilities import Facts, assess

        rows = {c.key: c for c in assess({"claude_code_start"}, Facts(agent_id="main"))}
        coding = rows["coding_jobs"]
        assert coding.available is False
        assert "ROBOTHOR_CODING_REPO_ROOTS" in coding.unlock
        assert "ROBOTHOR_CODING_WORKSPACE" not in coding.unlock

    def test_the_probe_reads_the_coding_settings_field(self) -> None:
        from types import SimpleNamespace

        from robothor.engine import capabilities

        without = SimpleNamespace(coding=SimpleNamespace(repo_roots=""))
        assert capabilities.coding_workspace(without) is None
        landed = SimpleNamespace(coding=SimpleNamespace(repo_roots="", workspace=" /srv/coding "))
        assert capabilities.coding_workspace(landed) == "/srv/coding"

    def test_the_probe_agrees_with_this_trees_settings_model(self, monkeypatch) -> None:
        from robothor.engine import capabilities
        from robothor.settings import get_settings, reset_settings
        from robothor.settings.model import CodingSettings

        monkeypatch.setenv("ROBOTHOR_CODING_WORKSPACE", "/srv/coding")
        reset_settings()
        found = capabilities.coding_workspace(get_settings())
        if "workspace" in CodingSettings.model_fields:
            assert found == "/srv/coding"
        else:
            assert found is None

    def test_the_prompt_reads_the_probed_workspace(self, tmp_path, monkeypatch) -> None:
        from robothor.engine import capabilities

        config = _agent(tmp_path, tools_allowed=["claude_code_start", "read_file"])
        monkeypatch.setattr(capabilities, "coding_workspace", lambda settings: None)
        body = _static(config, tmp_path)
        block = body[body.index(HEADER) :]
        assert "- coding_jobs:" in block[block.index("Gated") :]
        assert "ROBOTHOR_CODING_WORKSPACE" not in block
        engine_config_module._prompt_cache.clear()
        monkeypatch.setattr(
            capabilities, "coding_workspace", lambda settings: str(tmp_path / "coding")
        )
        body = _static(config, tmp_path)
        assert "- coding_jobs:" in body[body.index(HEADER) : body.index("Gated")]
