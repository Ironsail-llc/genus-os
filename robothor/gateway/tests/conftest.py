"""Shared fixtures for gateway tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def tmp_gateway_dir(tmp_path: Path) -> Path:
    """Create a minimal gateway directory structure for testing."""
    gateway = tmp_path / "gateway"
    gateway.mkdir()

    # package.json
    (gateway / "package.json").write_text(
        json.dumps({"name": "openclaw", "version": "2026.2.26"})
    )

    # dist/ with index.js (simulates built state)
    dist = gateway / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("// built gateway entry point\n")

    return gateway


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "extensions").mkdir()
    return config


@pytest.fixture
def unbuilt_gateway_dir(tmp_path: Path) -> Path:
    """Create a gateway directory without dist/ (not built)."""
    gateway = tmp_path / "gateway"
    gateway.mkdir()
    (gateway / "package.json").write_text(
        json.dumps({"name": "openclaw", "version": "2026.2.26"})
    )
    return gateway
