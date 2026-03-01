#!/usr/bin/env python3
"""
Fork Readiness Verification — Checks all 6 hardening criteria.

Criteria:
  1. Services discoverable via registry (robothor-services.json)
  2. Agent permissions in capability manifest (agent_capabilities.json)
  3. Helm handles interactive actions (action execute API)
  4. Event bus connects components (Redis streams)
  5. System self-describes at runtime (system APIs)
  6. Boot is health-gated (boot-orchestrator.py --dry-run)

Exit code: 0 = all pass, 1 = failures
"""

import json
import subprocess
import sys
from pathlib import Path

# Add paths
sys.path.insert(0, str(Path.home() / "clawd" / "memory_system"))

CHECKS = []
FAILURES = []


def check(name, fn):
    """Run a check and record result."""
    try:
        result = fn()
        status = "PASS" if result else "FAIL"
        if not result:
            FAILURES.append(name)
    except Exception as e:
        status = f"ERROR: {e}"
        FAILURES.append(name)
    CHECKS.append((name, status))
    print(f"  [{status}] {name}")


def check_1_service_registry():
    """1. Services discoverable via registry."""
    from service_registry import get_health_url, get_service_url, list_services, topological_sort

    services = list_services()
    if len(services) < 15:
        return False

    # Key services must be present
    for name in ["bridge", "orchestrator", "vision", "helm", "redis", "postgres"]:
        if name not in services:
            return False

    # URLs resolve
    if not get_service_url("bridge"):
        return False
    if not get_health_url("bridge"):
        return False

    # Topological sort works
    order = topological_sort()
    return len(order) == len(services)


def check_2_agent_capabilities():
    """2. Agent permissions in capability manifest."""
    manifest_path = Path.home() / "clawd" / "agent_capabilities.json"
    if not manifest_path.exists():
        return False

    with open(manifest_path) as f:
        manifest = json.load(f)

    agents = manifest.get("agents", {})
    if len(agents) < 8:
        return False

    # Key agents must exist
    for name in ["email-classifier", "main", "helm-user", "crm-steward"]:
        if name not in agents:
            return False

    # Each agent has tools and bridge_endpoints
    for _name, agent in agents.items():
        if "tools" not in agent or "bridge_endpoints" not in agent:
            return False

    return True


def check_3_helm_actions():
    """3. Helm handles interactive actions."""
    import httpx

    # Check action execute API exists and rejects unknown tools
    try:
        resp = httpx.post(
            "http://localhost:3004/api/actions/execute",
            json={"tool": "nonexistent_tool", "params": {}},
            timeout=5.0,
        )
        if resp.status_code != 400:
            return False
        data = resp.json()
        if "Unknown tool" not in data.get("error", ""):
            return False
    except Exception:
        return False

    # Check action execute API works with valid tool
    try:
        resp = httpx.post(
            "http://localhost:3004/api/actions/execute",
            json={"tool": "crm_health", "params": {}},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        if not data.get("success"):
            return False
    except Exception:
        return False

    # Check session persistence API
    try:
        resp = httpx.get("http://localhost:3004/api/session", timeout=5.0)
        if resp.status_code != 200:
            return False
    except Exception:
        return False

    return True


def check_4_event_bus():
    """4. Event bus connects components."""
    import redis

    try:
        r = redis.from_url("redis://localhost:6379/0")
        # Check that event bus streams exist or can be created
        r.ping()

        # Verify event_bus module is importable
        import event_bus

        if not hasattr(event_bus, "publish"):
            return False

        return hasattr(event_bus, "subscribe")
    except Exception:
        return False


def check_5_self_description():
    """5. System self-describes at runtime."""
    import httpx

    # Check system services API
    try:
        resp = httpx.get("http://localhost:3004/api/system/services", timeout=10.0)
        if resp.status_code != 200:
            return False
        data = resp.json()
        if data.get("total", 0) < 15:
            return False
        if "services" not in data:
            return False
    except Exception:
        return False

    # Check topology API
    try:
        resp = httpx.get("http://localhost:3004/api/system/topology", timeout=5.0)
        if resp.status_code != 200:
            return False
        data = resp.json()
        if len(data.get("nodes", [])) < 15:
            return False
        if len(data.get("edges", [])) < 5:
            return False
    except Exception:
        return False

    # Check audit API on Bridge
    try:
        resp = httpx.get("http://localhost:9100/api/audit?limit=1", timeout=5.0)
        if resp.status_code != 200:
            return False
    except Exception:
        return False

    return True


def check_6_boot_orchestration():
    """6. Boot is health-gated."""
    # Verify boot-orchestrator exists and --dry-run works
    script = Path.home() / "robothor" / "scripts" / "boot-orchestrator.py"
    if not script.exists():
        return False

    try:
        result = subprocess.run(
            [sys.executable, str(script), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False
        if "Boot order" not in result.stdout:
            return False
        # Verify dependency ordering in output
        lines = result.stdout.strip().split("\n")
        if len(lines) < 15:  # Should have 20+ services
            return False
    except Exception:
        return False

    # Verify manifest exists
    manifest = Path.home() / "robothor" / "robothor-services.json"
    return manifest.exists()


def main():
    print("=" * 60)
    print("  Robothor — Fork Readiness Verification")
    print("=" * 60)
    print()

    check("1. Service Registry", check_1_service_registry)
    check("2. Agent Capabilities", check_2_agent_capabilities)
    check("3. Helm Interactive Actions", check_3_helm_actions)
    check("4. Event Bus (Redis Streams)", check_4_event_bus)
    check("5. System Self-Description", check_5_self_description)
    check("6. Boot Orchestration", check_6_boot_orchestration)

    print()
    print("=" * 60)
    passed = len(CHECKS) - len(FAILURES)
    print(f"  Result: {passed}/{len(CHECKS)} checks passed")
    if FAILURES:
        print(f"  FAILED: {', '.join(FAILURES)}")
        print("  STATUS: NOT READY FOR FORK")
    else:
        print("  STATUS: FORK READY")
    print("=" * 60)

    sys.exit(0 if not FAILURES else 1)


if __name__ == "__main__":
    main()
