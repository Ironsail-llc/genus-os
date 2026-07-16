"""Tests for the data-scoping rollout flag (Task 5, Unified Identity Context).

``ROBOTHOR_DATA_SCOPING`` is a single-var off/observe/enforce ladder, same
shape as ``ROBOTHOR_PER_USER_SESSIONS`` (Task 3) and
``ROBOTHOR_TELEGRAM_ROLE_GATES`` (Task 4): there is no separate "is this
subsystem enabled at all" gate to flip independently of its rollout stage.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from robothor.engine.feature_flags import data_scoping_mode


class TestDataScopingMode:
    def test_default_off_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert data_scoping_mode() == "off"

    def test_explicit_off(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_DATA_SCOPING": "off"}, clear=True):
            assert data_scoping_mode() == "off"

    def test_observe(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_DATA_SCOPING": "observe"}, clear=True):
            assert data_scoping_mode() == "observe"

    def test_enforce(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_DATA_SCOPING": "enforce"}, clear=True):
            assert data_scoping_mode() == "enforce"

    def test_case_insensitive(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_DATA_SCOPING": "ENFORCE"}, clear=True):
            assert data_scoping_mode() == "enforce"

    def test_invalid_value_falls_back_to_off(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_DATA_SCOPING": "bogus"}, clear=True):
            assert data_scoping_mode() == "off"

    def test_whitespace_and_blank_treated_as_unset(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_DATA_SCOPING": "  "}, clear=True):
            assert data_scoping_mode() == "off"
