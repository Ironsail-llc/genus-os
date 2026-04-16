"""Platform-wide constants for Genus OS."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_TENANT = os.environ.get("ROBOTHOR_DEFAULT_TENANT", "default")


def _workspace_root() -> Path:
    """Instance workspace root. Never hardcode — always resolve through this."""
    return Path(os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor"))


def owner_config_path() -> Path:
    """Canonical, platform-hardcoded location of the operator identity file.

    The *path* is tracked in the platform; the *content* at that path is
    per-instance and gitignored (``.robothor/`` is in ``.gitignore``).
    """
    return _workspace_root() / ".robothor" / "owner.yaml"
