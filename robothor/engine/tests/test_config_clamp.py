"""Tests that out-of-range max_iterations is clamped, not just warned about.

Previously, config validation only emitted a warning for max_iterations=0;
nothing enforced the range. `main` sub_agent spawns were running with 0
iterations (which means the loop never reaches the LLM, reported to the
operator as a timeout).
"""

from __future__ import annotations

from robothor.engine.config import manifest_to_agent_config


def _base_manifest(**overrides) -> dict:
    m = {
        "id": "test-agent",
        "name": "Test Agent",
        "description": "Test",
        "model": {"primary": "gpt-4"},
        "schedule": {"max_iterations": 20},
    }
    for k, v in overrides.items():
        if k == "max_iterations":
            m["schedule"][k] = v
        else:
            m[k] = v
    return m


class TestMaxIterationsClamp:
    def test_zero_clamps_to_one(self) -> None:
        config = manifest_to_agent_config(_base_manifest(max_iterations=0))
        assert config.max_iterations == 1

    def test_negative_clamps_to_one(self) -> None:
        config = manifest_to_agent_config(_base_manifest(max_iterations=-5))
        assert config.max_iterations == 1

    def test_valid_value_preserved(self) -> None:
        config = manifest_to_agent_config(_base_manifest(max_iterations=42))
        assert config.max_iterations == 42

    def test_too_large_clamped_to_ten_thousand(self) -> None:
        config = manifest_to_agent_config(_base_manifest(max_iterations=1_000_000))
        assert config.max_iterations == 10_000

    def test_continuous_mode_floor_still_applies(self) -> None:
        # Continuous mode raises max_iterations to 100; clamp happens after,
        # so 100 stays 100 (well inside [1, 10000])
        m = _base_manifest(max_iterations=20)
        m["v2"] = {"continuous": True}
        config = manifest_to_agent_config(m)
        assert config.max_iterations == 100
