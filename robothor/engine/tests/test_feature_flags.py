"""Tests for the Tier 1-4 upgrade feature-flag scaffolding."""

from __future__ import annotations

import os
from unittest.mock import patch

from robothor.engine.feature_flags import (
    is_rip_enabled,
    rip_7_enforcement_mode,
    trajectory_sample_rate,
)


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


class TestRip7EnforcementMode:
    def test_default_off_when_rip_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert rip_7_enforcement_mode() == "off"

    def test_off_when_mode_set_but_rip_disabled(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_RIP_7_MODE": "enforce"}, clear=True):
            assert rip_7_enforcement_mode() == "off"

    def test_observe_is_default_when_enabled(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_RIP_7_ENABLED": "1"}, clear=True):
            assert rip_7_enforcement_mode() == "observe"

    def test_explicit_modes(self) -> None:
        for mode in ("observe", "alert", "enforce"):
            with patch.dict(
                os.environ,
                {"ROBOTHOR_RIP_7_ENABLED": "1", "ROBOTHOR_RIP_7_MODE": mode},
                clear=True,
            ):
                assert rip_7_enforcement_mode() == mode

    def test_case_insensitive_mode(self) -> None:
        with patch.dict(
            os.environ,
            {"ROBOTHOR_RIP_7_ENABLED": "1", "ROBOTHOR_RIP_7_MODE": "ENFORCE"},
            clear=True,
        ):
            assert rip_7_enforcement_mode() == "enforce"

    def test_invalid_mode_falls_back_to_observe(self) -> None:
        with patch.dict(
            os.environ,
            {"ROBOTHOR_RIP_7_ENABLED": "1", "ROBOTHOR_RIP_7_MODE": "bogus"},
            clear=True,
        ):
            assert rip_7_enforcement_mode() == "observe"

    def test_global_panic_overrides_to_off(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ROBOTHOR_RIP_7_ENABLED": "1",
                "ROBOTHOR_RIP_7_MODE": "enforce",
                "ROBOTHOR_DISABLE_ALL_RIPS": "1",
            },
            clear=True,
        ):
            assert rip_7_enforcement_mode() == "off"
