"""Check Node.js and pnpm prerequisites for building the gateway."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class PrereqResult:
    name: str
    found: bool
    version: str
    required_min: str
    hint: str

    @property
    def ok(self) -> bool:
        return self.found


def check_node() -> PrereqResult:
    """Check that Node.js >= 20 is available."""
    path = shutil.which("node")
    if not path:
        return PrereqResult(
            name="Node.js",
            found=False,
            version="",
            required_min="20.0.0",
            hint="https://nodejs.org/ or: curl -fsSL https://fnm.vercel.app/install | bash",
        )

    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5
        )
        version = result.stdout.strip().lstrip("v")
        major = int(version.split(".")[0])
        return PrereqResult(
            name="Node.js",
            found=major >= 20,
            version=version,
            required_min="20.0.0",
            hint=f"Node.js {version} found but >= 20 required" if major < 20 else "",
        )
    except Exception:
        return PrereqResult(
            name="Node.js",
            found=False,
            version="",
            required_min="20.0.0",
            hint="Failed to detect Node.js version",
        )


def check_pnpm() -> PrereqResult:
    """Check that pnpm is available."""
    path = shutil.which("pnpm")
    if not path:
        return PrereqResult(
            name="pnpm",
            found=False,
            version="",
            required_min="9.0.0",
            hint="npm install -g pnpm",
        )

    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5
        )
        version = result.stdout.strip()
        return PrereqResult(
            name="pnpm",
            found=True,
            version=version,
            required_min="9.0.0",
            hint="",
        )
    except Exception:
        return PrereqResult(
            name="pnpm",
            found=False,
            version="",
            required_min="9.0.0",
            hint="Failed to detect pnpm version",
        )


def check_all() -> list[PrereqResult]:
    """Check all gateway prerequisites."""
    return [check_node(), check_pnpm()]
