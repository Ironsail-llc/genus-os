"""
Root-level shared test fixtures.

Inherited by engine, health, and any other test suites that run from the
repo root.  Bridge tests run from their own rootdir and are unaffected.
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def test_prefix():
    """Unique prefix for test isolation."""
    return f"test_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def clean_env(monkeypatch):
    """Remove common env vars that leak between tests."""
    for key in [
        "ROBOTHOR_DB_HOST",
        "ROBOTHOR_DB_PORT",
        "ROBOTHOR_DB_NAME",
        "ROBOTHOR_DB_USER",
        "ROBOTHOR_DB_PASSWORD",
    ]:
        monkeypatch.delenv(key, raising=False)
