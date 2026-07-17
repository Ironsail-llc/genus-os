"""Tests for the per-user webchat session-key rollout flag (Task 3, Unified
Identity Context).

``ROBOTHOR_PER_USER_SESSIONS`` is a single-var off/observe/enforce ladder
(unlike the two-var ``*_ENABLED`` + ``*_MODE`` ladders elsewhere in this
module) because there is no separate "is this subsystem enabled at all"
gate to flip independently of its rollout stage.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from robothor.engine.feature_flags import per_user_sessions_mode


class TestPerUserSessionsMode:
    def test_default_off_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert per_user_sessions_mode() == "off"

    def test_explicit_off(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_PER_USER_SESSIONS": "off"}, clear=True):
            assert per_user_sessions_mode() == "off"

    def test_observe(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_PER_USER_SESSIONS": "observe"}, clear=True):
            assert per_user_sessions_mode() == "observe"

    def test_enforce(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_PER_USER_SESSIONS": "enforce"}, clear=True):
            assert per_user_sessions_mode() == "enforce"

    def test_case_insensitive(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_PER_USER_SESSIONS": "ENFORCE"}, clear=True):
            assert per_user_sessions_mode() == "enforce"

    def test_invalid_value_falls_back_to_off(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_PER_USER_SESSIONS": "bogus"}, clear=True):
            assert per_user_sessions_mode() == "off"

    def test_whitespace_and_blank_treated_as_unset(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_PER_USER_SESSIONS": "  "}, clear=True):
            assert per_user_sessions_mode() == "off"
