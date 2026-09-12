"""Shared fixtures for the auth test suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """The settings singleton is cached per process; a test that sets an env var
    must see it, so the cache is cleared before and after every auth test."""
    from robothor.settings import reset_settings

    reset_settings()
    yield
    reset_settings()
