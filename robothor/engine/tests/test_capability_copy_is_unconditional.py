"""The capability block and the gated refusal are the only behaviour.

No setting decides whether an agent is told what it can do, or whether a
refusal names its gate and unlock: both always happen. Rollback is a revert.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robothor.engine import config as engine_config_module
from robothor.engine.models import AgentConfig

GONE = ("ROBOTHOR_CAPABILITY_BLOCK_ENABLED", "ROBOTHOR_REFUSAL_PAYLOAD_V2")
HEADER = "## What you can do here"


@pytest.fixture
def both_zero(monkeypatch):
    from robothor.settings import reset_settings

    for name in GONE:
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("ROBOTHOR_RUN_VERIFICATION_MODE", "off")
    engine_config_module._prompt_cache.clear()
    reset_settings()
    yield
    engine_config_module._prompt_cache.clear()
    reset_settings()


def test_feature_flags_has_no_reader_for_either() -> None:
    from robothor.engine import feature_flags

    assert not hasattr(feature_flags, "capability_block_enabled")
    assert not hasattr(feature_flags, "refusal_payload_v2_enabled")
    source = Path(feature_flags.__file__).read_text()
    for name in GONE:
        assert name not in source, name


def test_the_settings_model_declares_neither() -> None:
    import robothor.settings.model as model

    source = Path(model.__file__).read_text()
    for name in GONE:
        assert name not in source, name


def test_a_refusal_carries_its_gate_and_unlock(both_zero) -> None:
    from robothor.engine.refusal import refusal

    out = refusal("no", gate="runtime", name="GITHUB_TOKEN", unlock="store a token")
    assert out == {
        "error": "no",
        "refused": True,
        "gate": "runtime",
        "gate_name": "GITHUB_TOKEN",
        "unlock": "store a token",
    }


def test_the_rules_tell_the_model_to_relay_a_refusal(both_zero) -> None:
    from robothor.engine.prompts import behavioral_rules

    assert "\n21. " in behavioral_rules()


def test_the_prompt_carries_the_capability_block(both_zero, tmp_path) -> None:
    (tmp_path / "brain").mkdir()
    (tmp_path / "brain" / "example.md").write_text("You are example-agent.")
    config = AgentConfig(
        id="example-agent",
        name="Example",
        instruction_file="brain/example.md",
        tools_allowed=["exec", "read_file"],
        timezone="UTC",
    )
    body = engine_config_module.build_system_prompt(config, tmp_path).static_body
    block = body[body.index(HEADER) :]
    assert "- exec:" in block
    assert "names the gate and the unlock" in block


def test_capabilities_name_no_credential_intake_setting() -> None:
    """Credential intake, where the build has it, is simply on: no unlock
    sends an operator to a setting."""
    import robothor.engine.capabilities as capabilities

    source = Path(capabilities.__file__).read_text()
    assert "ROBOTHOR_CREDENTIAL_INTAKE" not in source


def test_intake_in_the_build_admits_vault_set_for_the_chat_agent(monkeypatch) -> None:
    from robothor.engine import capabilities
    from robothor.engine.tools.handlers import vault

    monkeypatch.setenv("ROBOTHOR_CREDENTIAL_INTAKE", "off")
    monkeypatch.setattr(vault, "_intake_gate", lambda ctx: None, raising=False)
    assert capabilities.credential_intake() is True
    rows = {
        c.key: c
        for c in capabilities.assess(
            {"vault_set"}, capabilities.Facts(agent_id="main", credential_intake=True)
        )
    }
    assert rows["vault_set"].available is True
