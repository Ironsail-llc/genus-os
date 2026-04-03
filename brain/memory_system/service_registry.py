"""
Service Registry — Single source of truth for all Robothor service endpoints.

Reads robothor-services.json and provides URL lookups. Environment variables
override manifest defaults (e.g., BRIDGE_URL overrides bridge port).

Usage:
    from service_registry import get_service_url, get_health_url

    bridge = get_service_url("bridge")         # http://127.0.0.1:9100
    health = get_health_url("bridge")          # http://127.0.0.1:9100/health
    url = get_service_url("bridge", "/api/people")  # http://127.0.0.1:9100/api/people
"""

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Manifest location — relative to project root
_MANIFEST_PATHS = [
    Path(__file__).parent.parent.parent / "robothor" / "robothor-services.json",
    Path.home() / "robothor" / "robothor-services.json",
    Path("/home/philip/robothor/robothor-services.json"),
]

# Environment variable overrides: SERVICE_NAME_URL → full URL override
# e.g., BRIDGE_URL=http://localhost:9100 overrides manifest for "bridge"
_ENV_OVERRIDES = {
    "bridge": "BRIDGE_URL",
    "orchestrator": "ORCHESTRATOR_URL",
    "vision": "VISION_URL",
    "ollama": "OLLAMA_URL",
    "redis": "REDIS_URL",
    "gateway": "GATEWAY_URL",
    "searxng": "SEARXNG_URL",
    "helm": "HELM_URL",
    "mediamtx": "RTSP_URL",
}

# Cache
_manifest: dict | None = None
_manifest_mtime: float = 0.0


def _find_manifest() -> Path | None:
    """Find the manifest file."""
    for p in _MANIFEST_PATHS:
        if p.exists():
            return p
    return None


def _load_manifest() -> dict:
    """Load and cache the service manifest. Reloads if file changed."""
    global _manifest, _manifest_mtime

    path = _find_manifest()
    if path is None:
        if _manifest is not None:
            return _manifest
        logger.warning("Service manifest not found at any expected path")
        return {"services": {}}

    try:
        mtime = path.stat().st_mtime
    except OSError:
        if _manifest is not None:
            return _manifest
        return {"services": {}}

    if _manifest is not None and mtime <= _manifest_mtime:
        return _manifest

    try:
        with open(path) as f:
            _manifest = json.load(f)
            _manifest_mtime = mtime
            return _manifest
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load service manifest: %s", e)
        if _manifest is not None:
            return _manifest
        return {"services": {}}


def get_service(name: str) -> dict | None:
    """Get full service definition by name."""
    manifest = _load_manifest()
    return manifest.get("services", {}).get(name)


def get_service_url(name: str, path: str = "") -> str | None:
    """
    Get the base URL for a service, optionally with a path appended.

    Environment variable overrides take precedence over manifest values.
    Returns None if service is unknown.

    Args:
        name: Service name (e.g., "bridge", "orchestrator")
        path: Optional path to append (e.g., "/api/people")
    """
    # Check env override first
    env_key = _ENV_OVERRIDES.get(name)
    if env_key:
        env_val = os.environ.get(env_key)
        if env_val:
            base = env_val.rstrip("/")
            return f"{base}{path}" if path else base

    service = get_service(name)
    if service is None:
        return None

    host = service.get("host", "127.0.0.1")
    port = service.get("port")
    protocol = service.get("protocol", "http")

    if protocol == "ws":
        base = f"ws://{host}:{port}"
    else:
        base = f"http://{host}:{port}"

    return f"{base}{path}" if path else base


def get_health_url(name: str) -> str | None:
    """Get the health check URL for a service. Returns None if no health endpoint."""
    service = get_service(name)
    if service is None:
        return None

    health_path = service.get("health")
    if health_path is None:
        return None

    return get_service_url(name, health_path)


def list_services() -> dict:
    """List all services from the manifest."""
    manifest = _load_manifest()
    return manifest.get("services", {})


def get_dependencies(name: str) -> list[str]:
    """Get dependency list for a service."""
    service = get_service(name)
    if service is None:
        return []
    return service.get("dependencies", [])


def get_systemd_unit(name: str) -> str | None:
    """Get the systemd unit name for a service."""
    service = get_service(name)
    if service is None:
        return None
    return service.get("systemd_unit")


def wait_for_service(name: str, timeout: float = 30.0, interval: float = 1.0) -> bool:
    """
    Wait for a service's health endpoint to respond.

    Args:
        name: Service name
        timeout: Maximum wait time in seconds
        interval: Polling interval in seconds

    Returns:
        True if service became healthy, False if timeout
    """
    import httpx

    health_url = get_health_url(name)
    if health_url is None:
        logger.warning("Service '%s' has no health endpoint — cannot wait", name)
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(health_url, timeout=5.0)
            if resp.status_code < 500:
                return True
        except (httpx.ConnectError, httpx.TimeoutException, OSError):
            pass
        time.sleep(interval)

    return False


def topological_sort() -> list[str]:
    """
    Return services in dependency order (leaves first).

    Raises ValueError if the dependency graph has a cycle.
    """
    services = list_services()
    visited: set[str] = set()
    in_stack: set[str] = set()
    order: list[str] = []

    def visit(name: str) -> None:
        if name in in_stack:
            raise ValueError(f"Circular dependency detected involving '{name}'")
        if name in visited:
            return
        in_stack.add(name)
        svc = services.get(name, {})
        for dep in svc.get("dependencies", []):
            visit(dep)
        in_stack.remove(name)
        visited.add(name)
        order.append(name)

    for name in services:
        visit(name)

    return order


# Module-level convenience: reset cache for testing
def _reset_cache() -> None:
    """Reset the manifest cache. For testing only."""
    global _manifest, _manifest_mtime
    _manifest = None
    _manifest_mtime = 0.0
