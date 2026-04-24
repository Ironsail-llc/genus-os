"""Tests for the max_iterations clamp in ``manifest_to_agent_config``.

max_iterations=0 is the manifest sentinel for "no check-in interval"
(main.yaml heartbeat + worker both use 0 per operator directive
2026-04-20). The run loop guards with ``_checkin_interval > 0``, so 0
at this layer is valid.

A floor-at-1 clamp here regressed the main heartbeat on 2026-04-24 —
"Safety limit reached (0 iterations)" — because the clamp interacted
badly with safety_cap=0. Sub-agent spawns that genuinely need a
positive floor apply their own clamp in ``tools/handlers/spawn.py``.
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
    def test_zero_preserved_as_unlimited_sentinel(self) -> None:
        config = manifest_to_agent_config(_base_manifest(max_iterations=0))
        assert config.max_iterations == 0

    def test_negative_clamps_to_zero(self) -> None:
        config = manifest_to_agent_config(_base_manifest(max_iterations=-5))
        assert config.max_iterations == 0

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
