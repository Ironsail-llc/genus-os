"""A missing instruction or bootstrap file is skipped, and said so once.

``build_system_prompt`` keeps going when a manifest names a file that is not
there; an agent with a stale path still runs. The skip was logged on every
rebuild, which buried it. It is now logged at WARNING once per agent and path,
naming both.
"""

from __future__ import annotations

import logging

import pytest

from robothor.engine import config as engine_config_module
from robothor.engine.models import AgentConfig

_LOGGER = "robothor.engine.config"


@pytest.fixture(autouse=True)
def _fresh_caches():
    engine_config_module._prompt_cache.clear()
    engine_config_module._warned_missing_prompt_files.clear()
    yield
    engine_config_module._prompt_cache.clear()
    engine_config_module._warned_missing_prompt_files.clear()


def _missing_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "not found" in r.getMessage()
    ]


def test_missing_files_are_skipped_and_named(tmp_path, caplog) -> None:
    (tmp_path / "brain").mkdir()
    (tmp_path / "brain" / "SOUL.md").write_text("I am a fixture agent.")
    config = AgentConfig(
        id="alice-agent",
        name="Alice Agent",
        instruction_file="brain/agents/ALICE.md",
        bootstrap_files=["brain/SOUL.md", "brain/MEMORY.md"],
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        parts = engine_config_module.build_system_prompt(config, tmp_path)

    assert "I am a fixture agent." in parts.full_text()
    lines = _missing_warnings(caplog)
    assert len(lines) == 2
    assert any("alice-agent" in m and "brain/agents/ALICE.md" in m for m in lines)
    assert any("alice-agent" in m and "brain/MEMORY.md" in m for m in lines)


def test_each_missing_file_is_warned_once_per_agent(tmp_path, caplog) -> None:
    config = AgentConfig(
        id="alice-agent",
        name="Alice Agent",
        instruction_file="brain/agents/ALICE.md",
        bootstrap_files=["brain/MEMORY.md"],
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        for _ in range(3):
            engine_config_module._prompt_cache.clear()  # force a rebuild each turn
            engine_config_module.build_system_prompt(config, tmp_path)

    assert len(_missing_warnings(caplog)) == 2


def test_another_agent_missing_the_same_file_is_warned_too(tmp_path, caplog) -> None:
    alice = AgentConfig(id="alice-agent", name="A", bootstrap_files=["brain/MEMORY.md"])
    bob = AgentConfig(id="bob-agent", name="B", bootstrap_files=["brain/MEMORY.md"])

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        engine_config_module.build_system_prompt(alice, tmp_path)
        engine_config_module.build_system_prompt(bob, tmp_path)

    lines = _missing_warnings(caplog)
    assert len(lines) == 2
    assert any("bob-agent" in m for m in lines)
