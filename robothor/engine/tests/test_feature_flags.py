"""Tests for the Tier 1-4 upgrade feature-flag scaffolding."""

from __future__ import annotations

import os
from unittest.mock import patch

from robothor.engine.feature_flags import is_rip_enabled, trajectory_sample_rate


class TestIsRipEnabled:
    def test_default_off(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert is_rip_enabled(1) is False

    def test_explicit_on(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_RIP_1_ENABLED": "1"}, clear=True):
            assert is_rip_enabled(1) is True

    def test_truthy_values(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on"):
            with patch.dict(os.environ, {"ROBOTHOR_RIP_5_ENABLED": value}, clear=True):
                assert is_rip_enabled(5) is True, f"failed for {value!r}"

    def test_falsy_values(self) -> None:
        for value in ("0", "false", "no", "off", ""):
            with patch.dict(os.environ, {"ROBOTHOR_RIP_5_ENABLED": value}, clear=True):
                assert is_rip_enabled(5) is False, f"failed for {value!r}"

    def test_per_rip_isolation(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_RIP_1_ENABLED": "1"}, clear=True):
            assert is_rip_enabled(1) is True
            assert is_rip_enabled(2) is False

    def test_global_panic_overrides_individual_flags(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ROBOTHOR_RIP_1_ENABLED": "1",
                "ROBOTHOR_RIP_7_ENABLED": "1",
                "ROBOTHOR_DISABLE_ALL_RIPS": "1",
            },
            clear=True,
        ):
            assert is_rip_enabled(1) is False
            assert is_rip_enabled(7) is False


class TestTrajectorySampleRate:
    def test_default_zero(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert trajectory_sample_rate() == 0.0

    def test_set_rate(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_TRAJECTORY_SAMPLE": "0.5"}, clear=True):
            assert trajectory_sample_rate() == 0.5

    def test_clamp_above_one(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_TRAJECTORY_SAMPLE": "5.0"}, clear=True):
            assert trajectory_sample_rate() == 1.0

    def test_clamp_below_zero(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_TRAJECTORY_SAMPLE": "-0.1"}, clear=True):
            assert trajectory_sample_rate() == 0.0

    def test_invalid_string_defaults_to_zero(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_TRAJECTORY_SAMPLE": "nonsense"}, clear=True):
            assert trajectory_sample_rate() == 0.0

    def test_global_panic_zeros_rate(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ROBOTHOR_TRAJECTORY_SAMPLE": "1.0",
                "ROBOTHOR_DISABLE_ALL_RIPS": "1",
            },
            clear=True,
        ):
            assert trajectory_sample_rate() == 0.0
