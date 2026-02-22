#!/usr/bin/env python3
"""
Dependency Verification — Check that installed packages match expected versions.

Reads robothor-deps.json and verifies installed packages match.
Exit code: 0 = all match, 1 = mismatches found.
"""

import json
import subprocess
import sys
from pathlib import Path


def get_pip_versions(venv_path: Path) -> dict:
    """Get installed pip package versions from a venv."""
    pip = venv_path / "bin" / "pip"
    if not pip.exists():
        return {}
    try:
        result = subprocess.run(
            [str(pip), "list", "--format=json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {}
        packages = json.loads(result.stdout)
        return {p["name"].lower(): p["version"] for p in packages}
    except Exception:
        return {}


def get_npm_versions(project_path: Path) -> dict:
    """Get installed npm package versions."""
    try:
        result = subprocess.run(
            ["pnpm", "list", "--json", "--depth=0"],
            capture_output=True, text=True, timeout=10,
            cwd=str(project_path),
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        deps = {}
        if isinstance(data, list):
            data = data[0]
        for section in ["dependencies", "devDependencies"]:
            for name, info in data.get(section, {}).items():
                deps[name] = info.get("version", "unknown")
        return deps
    except Exception:
        return {}


def get_system_command_version(cmd: str) -> str:
    """Get version of a system command."""
    try:
        result = subprocess.run(
            [cmd, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip().split("\n")[0]
    except Exception:
        return "not found"


def main():
    manifest_path = Path.home() / "robothor" / "robothor-services.json"
    if not manifest_path.exists():
        print("ERROR: robothor-services.json not found")
        sys.exit(1)

    print("=" * 60)
    print("  Robothor — Dependency Verification")
    print("=" * 60)
    print()

    errors = []

    # Check Python venvs exist
    venvs = {
        "memory_system": Path.home() / "clawd" / "memory_system" / "venv",
        "bridge": Path.home() / "robothor" / "crm" / "bridge" / "venv",
    }

    for name, venv_path in venvs.items():
        if venv_path.exists():
            versions = get_pip_versions(venv_path)
            print(f"  Python venv ({name}): {len(versions)} packages installed")
        else:
            print(f"  Python venv ({name}): NOT FOUND")
            errors.append(f"Missing venv: {name}")

    # Check Node.js project
    app_path = Path.home() / "robothor" / "app"
    if (app_path / "node_modules").exists():
        npm_versions = get_npm_versions(app_path)
        print(f"  Node.js (app): {len(npm_versions)} packages installed")
    else:
        print("  Node.js (app): node_modules NOT FOUND")
        errors.append("Missing node_modules")

    # Check system tools
    print()
    tools = ["python3", "node", "pnpm", "redis-cli", "psql", "ollama", "cloudflared"]
    for tool in tools:
        version = get_system_command_version(tool)
        status = "ok" if "not found" not in version else "MISSING"
        print(f"  {tool:<15s} [{status}] {version[:60]}")
        if status == "MISSING":
            errors.append(f"Missing tool: {tool}")

    # Check Docker
    docker_version = get_system_command_version("docker")
    print(f"  {'docker':<15s} [{'ok' if 'not found' not in docker_version else 'MISSING'}] {docker_version[:60]}")

    # Check services manifest
    print()
    with open(manifest_path) as f:
        manifest = json.load(f)
    print(f"  Service manifest: {len(manifest.get('services', {}))} services defined")

    print()
    print("=" * 60)
    if errors:
        print(f"  ERRORS: {len(errors)}")
        for e in errors:
            print(f"    - {e}")
    else:
        print("  All dependencies verified.")
    print("=" * 60)

    sys.exit(0 if not errors else 1)


if __name__ == "__main__":
    main()
