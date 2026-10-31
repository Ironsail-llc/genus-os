"""The system prompt says what this agent can do here, and what gates the rest.

Without it the model learns its abilities only from tool schemas, and an agent
that has `exec`, `write_file` and the git tools still tells the person "I can't
edit code". ``ROBOTHOR_CAPABILITY_BLOCK_ENABLED`` adds one block, built from the
agent's granted tools and the engine's settings, after the bootstrap files.

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

FLAG = "ROBOTHOR_CAPABILITY_BLOCK_ENABLED"
HEADER = "## What you can do here"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from robothor.settings import reset_settings

    monkeypatch.delenv(FLAG, raising=False)
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


class TestFlagOff:
    def test_the_static_body_is_unchanged(self, tmp_path) -> None:
        config = _agent(tmp_path)
        body = _static(config, tmp_path)
        assert HEADER not in body
        # Byte-identical to the body built without the capability module at all.
        from robothor.engine.prompts import behavioral_rules

        assert body.startswith(engine_config_module.SECURITY_PREAMBLE)
        assert behavioral_rules() in body


class TestFlagOn:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv(FLAG, "1")

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
            Facts(agent_id="a", service_role="service", github_token=False),
        )
        git = next(c for c in rows if c.key == "git_pr")
        assert git.available is False
        assert git.gate == "runtime"
        assert git.gate_name == "GITHUB_TOKEN"
        assert "vault_set" in git.unlock
