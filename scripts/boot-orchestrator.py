#!/usr/bin/env python3
"""
Boot Orchestrator — Start Robothor services in dependency order.

Uses robothor-services.json (via service_registry) for topology.
Health-gated: waits for each service's health endpoint before starting dependents.

Usage:
    boot-orchestrator.py             Start all services in order
    boot-orchestrator.py --dry-run   Show boot order without starting
    boot-orchestrator.py --status    Show current status of all services
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

# Add memory_system to path for service_registry import
sys.path.insert(0, str(Path(__file__).parent.parent / "brain" / "memory_system"))
sys.path.insert(0, str(Path.home() / "clawd" / "memory_system"))

from service_registry import (
    get_health_url,
    get_service,
    get_systemd_unit,
    list_services,
    topological_sort,
)


def check_systemd_active(unit: str) -> bool:
    """Check if a systemd unit is active."""
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() == "active"
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False


def check_health(name: str) -> bool:
    """Check service health via HTTP endpoint."""
    url = get_health_url(name)
    if url is None:
        return True  # No health endpoint — assume healthy

    try:
        import httpx

        resp = httpx.get(url, timeout=5.0)
        return resp.status_code < 500
    except Exception:
        return False


def start_service(name: str) -> bool:
    """Start a systemd service."""
    unit = get_systemd_unit(name)
    if unit is None:
        return True  # Docker-managed or no unit

    if check_systemd_active(unit):
        return True  # Already running

    print(f"  Starting {unit}...")
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "start", unit],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        print(f"  ERROR: Failed to start {unit}: {e}")
        return False


def wait_for_health(name: str, timeout: float = 30.0) -> bool:
    """Wait for a service to become healthy."""
    url = get_health_url(name)
    if url is None:
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check_health(name):
            return True
        time.sleep(1.0)
    return False


def boot(dry_run: bool = False) -> bool:
    """Boot all services in dependency order."""
    order = topological_sort()

    if dry_run:
        print("Boot order (dry-run):")
        for i, name in enumerate(order, 1):
            svc = get_service(name) or {}
            unit = svc.get("systemd_unit", "-")
            deps = svc.get("dependencies", [])
            dep_str = f" (after: {', '.join(deps)})" if deps else ""
            print(f"  {i:2d}. {name:<20s} unit={unit or '-'}{dep_str}")
        return True

    print(f"Booting {len(order)} services...\n")
    failed = []

    for name in order:
        svc = get_service(name) or {}
        unit = svc.get("systemd_unit")

        if unit is None:
            # Docker-managed or system service — skip
            status = "skip (no unit)"
        elif check_systemd_active(unit):
            status = "already running"
        else:
            if not start_service(name):
                failed.append(name)
                status = "FAILED to start"
            elif not wait_for_health(name, timeout=30.0):
                failed.append(name)
                status = "FAILED health check"
            else:
                status = "started"

        health = (
            "healthy"
            if check_health(name)
            else "no health"
            if not get_health_url(name)
            else "unhealthy"
        )
        print(f"  {name:<20s} {status:<25s} [{health}]")

    print()
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return False
    else:
        print("All services booted successfully.")
        return True


def status() -> None:
    """Show current status of all services."""
    services = list_services()
    print(f"{'Service':<20s} {'Port':>5s} {'Unit':<30s} {'Active':<8s} {'Health'}")
    print("-" * 90)

    for name, svc in sorted(services.items()):
        port = str(svc.get("port", ""))
        unit = svc.get("systemd_unit") or "(docker/system)"

        if svc.get("systemd_unit"):
            active = "yes" if check_systemd_active(svc["systemd_unit"]) else "no"
        else:
            active = "-"

        if get_health_url(name):
            healthy = "ok" if check_health(name) else "FAIL"
        else:
            healthy = "-"

        print(f"  {name:<20s} {port:>5s} {unit:<30s} {active:<8s} {healthy}")


def main():
    parser = argparse.ArgumentParser(description="Robothor Boot Orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="Show boot order without starting")
    parser.add_argument("--status", action="store_true", help="Show current service status")
    args = parser.parse_args()

    if args.status:
        status()
    elif args.dry_run:
        boot(dry_run=True)
    else:
        success = boot()
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
