"""Platform-wide constants for Genus OS."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_TENANT = os.environ.get("ROBOTHOR_DEFAULT_TENANT", "default")


def owner_config_path() -> Path:
    """Canonical, platform-hardcoded location of the operator identity file.

    Resolves to ``~/.robothor/owner.yaml`` — a conventional user-level
    dotfile, independent of ``ROBOTHOR_WORKSPACE`` (which holds project
    data, not identity). The *path* is tracked in the platform; the
    *content* at that path is per-instance and gitignored.
    """
    return Path.home() / ".robothor" / "owner.yaml"
