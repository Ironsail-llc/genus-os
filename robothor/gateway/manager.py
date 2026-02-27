"""Gateway build and lifecycle management."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from robothor.gateway.prerequisites import PrereqResult, check_all


@dataclass
class GatewayStatus:
    version: str
    built: bool
    config_dir: Path
    gateway_dir: Path
    prereqs: list[PrereqResult]


class GatewayManager:
    """Manages the OpenClaw gateway source tree: build, status, plugins."""

    def __init__(
        self,
        gateway_dir: Path | None = None,
        config_dir: Path | None = None,
    ):
        self.gateway_dir = gateway_dir or Path(__file__).parents[2] / "gateway"
        self.config_dir = config_dir or Path(
            os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw")
        )

    def check_prerequisites(self) -> list[PrereqResult]:
        """Check Node.js and pnpm are available."""
        return check_all()

    def get_version(self) -> str:
        """Read version from gateway/package.json."""
        pkg = self.gateway_dir / "package.json"
        if not pkg.exists():
            return "unknown"
        try:
            data = json.loads(pkg.read_text())
            return data.get("version", "unknown")
        except Exception:
            return "unknown"

    def is_built(self) -> bool:
        """Check if the gateway has been built (dist/ exists with content)."""
        dist = self.gateway_dir / "dist"
        if not dist.exists():
            return False
        # Check for at least an index.js
        return (dist / "index.js").exists()

    def build(self) -> bool:
        """Run pnpm install && pnpm build. Returns True on success."""
        try:
            subprocess.run(
                ["pnpm", "install", "--frozen-lockfile"],
                cwd=str(self.gateway_dir),
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            subprocess.run(
                ["pnpm", "build"],
                cwd=str(self.gateway_dir),
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def rebuild(self) -> bool:
        """Clean dist/ and rebuild."""
        dist = self.gateway_dir / "dist"
        if dist.exists():
            import shutil

            shutil.rmtree(dist)
        return self.build()

    def status(self) -> GatewayStatus:
        """Get comprehensive gateway status."""
        return GatewayStatus(
            version=self.get_version(),
            built=self.is_built(),
            config_dir=self.config_dir,
            gateway_dir=self.gateway_dir,
            prereqs=self.check_prerequisites(),
        )

    def install_plugins(self) -> bool:
        """Symlink bundled plugins to the config extensions directory.

        Creates symlinks from repo plugins to ~/.openclaw/extensions/ (or
        wherever OPENCLAW_HOME points).
        """
        plugins_src = Path(__file__).parents[0] / "plugins"
        if not plugins_src.exists():
            return True  # No bundled plugins

        extensions_dir = self.config_dir / "extensions"
        extensions_dir.mkdir(parents=True, exist_ok=True)

        for plugin_dir in plugins_src.iterdir():
            if not plugin_dir.is_dir():
                continue
            manifest = plugin_dir / "openclaw.plugin.json"
            if not manifest.exists():
                continue

            target = extensions_dir / plugin_dir.name
            if target.exists() or target.is_symlink():
                target.unlink() if target.is_symlink() else None
                if target.exists():
                    continue  # Real dir, don't overwrite

            target.symlink_to(plugin_dir.resolve())

        return True

    def sync_upstream(self) -> bool:
        """Pull latest OpenClaw changes via git subtree.

        Returns True on success. Conflicts must be resolved manually.
        """
        repo_root = self.gateway_dir.parent
        try:
            subprocess.run(
                [
                    "git", "subtree", "pull",
                    "--prefix=gateway",
                    "openclaw-upstream", "main",
                    "--squash",
                    "-m", "chore: sync OpenClaw upstream",
                ],
                cwd=str(repo_root),
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
