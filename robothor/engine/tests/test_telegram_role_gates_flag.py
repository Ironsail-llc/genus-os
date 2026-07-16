"""Tests for the Telegram role-gates rollout flag (Task 4, Unified Identity
Context).

``ROBOTHOR_TELEGRAM_ROLE_GATES`` is a single-var off/observe/enforce ladder,
same shape as ``ROBOTHOR_PER_USER_SESSIONS`` (Task 3) — there is no separate
"is this subsystem enabled at all" gate to flip independently of its rollout
stage.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from robothor.engine.feature_flags import (
    allow_unregistered_owner_fallback,
    open_onboarding_enabled,
    telegram_role_gates_mode,
)


class TestTelegramRoleGatesMode:
    def test_default_off_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert telegram_role_gates_mode() == "off"

    def test_explicit_off(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_TELEGRAM_ROLE_GATES": "off"}, clear=True):
            assert telegram_role_gates_mode() == "off"

    def test_observe(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_TELEGRAM_ROLE_GATES": "observe"}, clear=True):
            assert telegram_role_gates_mode() == "observe"

    def test_enforce(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_TELEGRAM_ROLE_GATES": "enforce"}, clear=True):
            assert telegram_role_gates_mode() == "enforce"

    def test_case_insensitive(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_TELEGRAM_ROLE_GATES": "ENFORCE"}, clear=True):
            assert telegram_role_gates_mode() == "enforce"

    def test_invalid_value_falls_back_to_off(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_TELEGRAM_ROLE_GATES": "bogus"}, clear=True):
            assert telegram_role_gates_mode() == "off"

    def test_whitespace_and_blank_treated_as_unset(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_TELEGRAM_ROLE_GATES": "  "}, clear=True):
            assert telegram_role_gates_mode() == "off"


class TestAllowUnregisteredOwnerFallback:
    def test_default_false(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert allow_unregistered_owner_fallback() is False

    def test_true_values(self) -> None:
        for val in ("1", "true", "yes", "on", "TRUE"):
            with patch.dict(
                os.environ, {"ROBOTHOR_ALLOW_UNREGISTERED_OWNER_FALLBACK": val}, clear=True
            ):
                assert allow_unregistered_owner_fallback() is True

    def test_false_values(self) -> None:
        for val in ("0", "false", "no", "off", ""):
            with patch.dict(
                os.environ, {"ROBOTHOR_ALLOW_UNREGISTERED_OWNER_FALLBACK": val}, clear=True
            ):
                assert allow_unregistered_owner_fallback() is False


class TestOpenOnboardingEnabled:
    def test_default_false(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert open_onboarding_enabled() is False

    def test_true_values(self) -> None:
        for val in ("1", "true", "yes", "on"):
            with patch.dict(os.environ, {"ROBOTHOR_OPEN_ONBOARDING": val}, clear=True):
                assert open_onboarding_enabled() is True

    def test_false_values(self) -> None:
        for val in ("0", "false", "no", ""):
            with patch.dict(os.environ, {"ROBOTHOR_OPEN_ONBOARDING": val}, clear=True):
                assert open_onboarding_enabled() is False
