"""One-time migration from split layout (~/moltbot/ + ~/.openclaw/) to unified repo.

Usage:
    robothor gateway migrate

Steps:
    1. Verify gateway/ exists in repo (subtree import done)
    2. Verify gateway builds
    3. Create ~/.robothor/config/ structure (or use OPENCLAW_HOME)
    4. Copy agent session state from ~/.openclaw/agents/
    5. Generate config from manifests
    6. Generate and install robothor-gateway.service
    7. Start new service, verify health
    8. Disable old moltbot-gateway.service
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def migrate(
    *,
    gateway_dir: Path | None = None,
    old_config_dir: Path | None = None,
    new_config_dir: Path | None = None,
    manifest_dir: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Run the full migration.

    Args:
        gateway_dir: Path to gateway/ in repo
        old_config_dir: Path to ~/.openclaw/
        new_config_dir: Path to new config dir (defaults to old_config_dir for in-place)
        manifest_dir: Path to docs/agents/
        dry_run: If True, print actions without executing

    Returns:
        0 on success, 1 on failure
    """
    repo_root = Path(__file__).parents[2]
    gateway_dir = gateway_dir or repo_root / "gateway"
    old_config_dir = old_config_dir or Path.home() / ".openclaw"
    new_config_dir = new_config_dir or old_config_dir  # in-place by default
    manifest_dir = manifest_dir or repo_root / "docs" / "agents"

    steps = [
        ("Verify gateway/ exists", lambda: _verify_gateway(gateway_dir)),
        ("Check gateway can build", lambda: _verify_build(gateway_dir, dry_run)),
        ("Prepare config directory", lambda: _prepare_config(new_config_dir, dry_run)),
        (
            "Copy session state",
            lambda: _copy_sessions(old_config_dir, new_config_dir, dry_run),
        ),
        (
            "Generate config from manifests",
            lambda: _generate_config(manifest_dir, new_config_dir, dry_run),
        ),
        (
            "Install systemd service",
            lambda: _install_service(gateway_dir, new_config_dir, dry_run),
        ),
        ("Start new service", lambda: _start_service(dry_run)),
        ("Disable old service", lambda: _disable_old_service(dry_run)),
    ]

    print()
    print("  Robothor Gateway Migration")
    print("  " + "=" * 26)
    print()

    for i, (name, fn) in enumerate(steps, 1):
        print(f"  Step {i}/8: {name}...", end=" ", flush=True)
        try:
            result = fn()
            if result:
                print("done")
            else:
                print("skipped")
        except MigrationError as e:
            print(f"FAILED — {e}")
            return 1
        except Exception as e:
            print(f"ERROR — {e}")
            return 1

    print()
    print("  Migration complete!")
    print(f"    Gateway source: {gateway_dir}")
    print(f"    Config: {new_config_dir}")
    if old_config_dir != new_config_dir:
        print(f"    Old config ({old_config_dir}) can be removed.")
    old_moltbot = Path.home() / "moltbot"
    if old_moltbot.exists():
        print(f"    Old source ({old_moltbot}) can be removed.")
    print()
    return 0


class MigrationError(Exception):
    pass


def _verify_gateway(gateway_dir: Path) -> bool:
    if not (gateway_dir / "package.json").exists():
        raise MigrationError(
            f"gateway/ not found at {gateway_dir}. "
            "Run the subtree import first."
        )
    return True


def _verify_build(gateway_dir: Path, dry_run: bool) -> bool:
    if dry_run:
        return True

    from robothor.gateway.manager import GatewayManager

    mgr = GatewayManager(gateway_dir=gateway_dir)

    if not mgr.is_built():
        prereqs = mgr.check_prerequisites()
        if not all(p.ok for p in prereqs):
            missing = [p.name for p in prereqs if not p.ok]
            raise MigrationError(f"Missing prerequisites: {', '.join(missing)}")
        if not mgr.build():
            raise MigrationError("Gateway build failed")

    return True


def _prepare_config(config_dir: Path, dry_run: bool) -> bool:
    if dry_run:
        return True

    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "extensions").mkdir(exist_ok=True)
    (config_dir / "agents").mkdir(exist_ok=True)
    (config_dir / "cron").mkdir(exist_ok=True)
    return True


def _copy_sessions(old_dir: Path, new_dir: Path, dry_run: bool) -> bool:
    if old_dir == new_dir:
        return True  # In-place, nothing to copy

    old_agents = old_dir / "agents"
    if not old_agents.exists():
        return True  # No sessions to copy

    if dry_run:
        return True

    new_agents = new_dir / "agents"
    if old_agents.exists() and old_agents.is_dir():
        shutil.copytree(old_agents, new_agents, dirs_exist_ok=True)
    return True


def _generate_config(manifest_dir: Path, config_dir: Path, dry_run: bool) -> bool:
    if dry_run:
        return True

    from robothor.gateway.config_gen import generate_and_deploy

    result = generate_and_deploy(manifest_dir, config_dir)
    if result != 0:
        raise MigrationError("Config generation failed")
    return True


def _install_service(gateway_dir: Path, config_dir: Path, dry_run: bool) -> bool:
    if dry_run:
        return True

    from robothor.gateway.process import GatewayProcess

    proc = GatewayProcess(gateway_dir=gateway_dir, config_dir=config_dir)
    try:
        proc.install_systemd_unit()
    except Exception as e:
        raise MigrationError(f"Service install failed: {e}")
    return True


def _start_service(dry_run: bool) -> bool:
    if dry_run:
        return True

    try:
        subprocess.run(
            ["sudo", "systemctl", "start", "robothor-gateway"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except Exception as e:
        raise MigrationError(f"Service start failed: {e}")
    return True


def _disable_old_service(dry_run: bool) -> bool:
    if dry_run:
        return True

    # Check if old service exists
    result = subprocess.run(
        ["sudo", "systemctl", "is-active", "moltbot-gateway"],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip() == "active":
        subprocess.run(
            ["sudo", "systemctl", "stop", "moltbot-gateway"],
            capture_output=True,
        )
        subprocess.run(
            ["sudo", "systemctl", "disable", "moltbot-gateway"],
            capture_output=True,
        )
        return True

    return True  # Already stopped/doesn't exist
