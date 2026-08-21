"""Tests for the observe→alert→enforce enforcement-mode flags (Wave-1 hardening, PR-1).

These three flags clone the existing ``rip_7_enforcement_mode`` ladder so the
sandbox-default, RBAC-over-fleet, and fail-closed-approval rollouts can be
promoted off → observe → alert → enforce via ``systemctl set-environment``
with no redeploy, and rolled back instantly.
"""

from __future__ import annotations

import pytest

from robothor.engine.feature_flags import (
    approval_mode,
    completion_contract_mode,
    rbac_enforcement_mode,
    run_verification_mode,
    sandbox_default_mode,
)

# Each entry pairs a mode accessor with its enabled flag and mode env var.
MODE_FLAGS = [
    (sandbox_default_mode, "ROBOTHOR_SANDBOX_DEFAULT_ENABLED", "ROBOTHOR_SANDBOX_DEFAULT_MODE"),
    (rbac_enforcement_mode, "ROBOTHOR_RBAC_ENABLED", "ROBOTHOR_RBAC_MODE"),
    (approval_mode, "ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "ROBOTHOR_APPROVAL_MODE"),
    (
        completion_contract_mode,
        "ROBOTHOR_COMPLETION_CONTRACTS_ENABLED",
        "ROBOTHOR_COMPLETION_CONTRACTS_MODE",
    ),
    (
        run_verification_mode,
        "ROBOTHOR_RUN_VERIFICATION_ENABLED",
        "ROBOTHOR_RUN_VERIFICATION_MODE",
    ),
]


@pytest.fixture(autouse=True)
def _clear_panic(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_DISABLE_ALL_RIPS", raising=False)


@pytest.mark.parametrize("fn,enabled_var,mode_var", MODE_FLAGS)
class TestEnforcementModes:
    def test_off_when_flag_unset(self, fn, enabled_var, mode_var, monkeypatch):
        monkeypatch.delenv(enabled_var, raising=False)
        monkeypatch.delenv(mode_var, raising=False)
        assert fn() == "off"

    def test_off_when_flag_false_even_with_mode(self, fn, enabled_var, mode_var, monkeypatch):
        monkeypatch.setenv(enabled_var, "0")
        monkeypatch.setenv(mode_var, "enforce")
        assert fn() == "off"

    def test_observe_default_when_enabled(self, fn, enabled_var, mode_var, monkeypatch):
        monkeypatch.setenv(enabled_var, "1")
        monkeypatch.delenv(mode_var, raising=False)
        assert fn() == "observe"

    def test_explicit_modes_round_trip(self, fn, enabled_var, mode_var, monkeypatch):
        monkeypatch.setenv(enabled_var, "1")
        for mode in ("observe", "alert", "enforce"):
            monkeypatch.setenv(mode_var, mode)
            assert fn() == mode

    def test_invalid_mode_falls_back_to_observe(self, fn, enabled_var, mode_var, monkeypatch):
        monkeypatch.setenv(enabled_var, "1")
        monkeypatch.setenv(mode_var, "bogus")
        assert fn() == "observe"

    def test_panic_flag_forces_off(self, fn, enabled_var, mode_var, monkeypatch):
        monkeypatch.setenv(enabled_var, "1")
        monkeypatch.setenv(mode_var, "enforce")
        monkeypatch.setenv("ROBOTHOR_DISABLE_ALL_RIPS", "1")
        assert fn() == "off"
