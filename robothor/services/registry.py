"""
Service Registry — Single source of truth for all Genus OS service endpoints.

Reads robothor-services.json and provides URL lookups. Environment variables
override manifest defaults (e.g., BRIDGE_URL overrides bridge port).

Usage:
    from robothor.services import get_service_url, get_health_url

    bridge = get_service_url("bridge")                # http://127.0.0.1:9100
    health = get_health_url("bridge")                 # http://127.0.0.1:9100/health
    url = get_service_url("bridge", "/api/people")    # http://127.0.0.1:9100/api/people
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from robothor.config import get_config

logger = logging.getLogger(__name__)

# Environment variable overrides: service name -> the names that override its
# manifest entry, in precedence order.
#
# Ollama is NOT here. Its endpoint is a declared setting, so it resolves
# through robothor.settings.get_settings() instead -- see _settings_url(). A
# second precedence list beside the settings package is how the two stop
# agreeing: this module read only the bare `OLLAMA_URL`, so an operator who
# set the documented `ROBOTHOR_OLLAMA_URL` got the manifest default and
# nothing to explain why, and neither name could be set in config.yaml at all.
_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "bridge": ("BRIDGE_URL",),
    "orchestrator": ("ORCHESTRATOR_URL",),
    "vision": ("VISION_URL",),
    "redis": ("REDIS_URL",),
    "searxng": ("SEARXNG_URL",),
    "helm": ("HELM_URL",),
    "mediamtx": ("RTSP_URL",),
}

#: Services whose endpoint the settings registry declares. The value is the
#: dotted path of the field that holds the full base URL.
_SETTINGS_URLS: dict[str, tuple[str, str]] = {"ollama": ("ollama", "url")}

# Cache
_manifest: dict[str, Any] | None = None
_manifest_mtime: float = 0.0


def _get_manifest_paths() -> list[Path]:
    """Build list of candidate manifest paths."""
    cfg = get_config()
    return [
        cfg.workspace / "robothor-services.json",
        Path.cwd() / "robothor-services.json",
    ]


def _find_manifest() -> Path | None:
    """Find the manifest file."""
    # Allow explicit path via env var
    explicit = os.environ.get("ROBOTHOR_SERVICES_MANIFEST")
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p

    for p in _get_manifest_paths():
        if p.exists():
            return p
    return None


def _load_manifest() -> dict[str, Any]:
    """Load and cache the service manifest. Reloads if file changed."""
    global _manifest, _manifest_mtime

    path = _find_manifest()
    if path is None:
        if _manifest is not None:
            return _manifest
        logger.debug("Service manifest not found at any expected path")
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
        with path.open() as f:
            _manifest = json.load(f)
            _manifest_mtime = mtime
            return _manifest
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load service manifest: %s", e)
        if _manifest is not None:
            return _manifest
        return {"services": {}}


def get_service(name: str) -> dict[str, Any] | None:
    """Get full service definition by name."""
    manifest = _load_manifest()
    result: dict[str, Any] | None = manifest.get("services", {}).get(name)
    return result


def _settings_url(name: str) -> str:
    """The endpoint the settings registry resolves for ``name``, or "".

    Routed through ``get_settings()`` rather than read from ``os.environ``
    here, which buys three things this module used to lack: config.yaml works
    as a source (so ``genus config set ROBOTHOR_OLLAMA_URL`` takes effect),
    the deprecated bare ``OLLAMA_URL`` warns once naming its replacement
    instead of being silently preferred, and the precedence between the two
    names is described in one place instead of two that can disagree.

    The import is local: ``robothor.services`` is imported by scripts that
    never resolve a setting, and pydantic-settings is not a cheap import.
    """
    field = _SETTINGS_URLS.get(name)
    if field is None:
        return ""
    from robothor.settings import get_settings

    group, attribute = field
    try:
        return str(getattr(getattr(get_settings(), group), attribute) or "")
    except Exception:  # a settings failure must not take the manifest with it
        logger.debug("settings lookup for %s failed; falling back", name, exc_info=True)
        return ""


def get_service_url(name: str, path: str = "") -> str | None:
    """Get the base URL for a service, optionally with a path appended.

    A declared setting wins, then an environment override, then the manifest.
    Returns None if service is unknown.
    """
    configured = _settings_url(name)
    if configured:
        base = configured.rstrip("/")
        return f"{base}{path}" if path else base

    for env_key in _ENV_OVERRIDES.get(name, ()):
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
    """Get the health check URL for a service."""
    service = get_service(name)
    if service is None:
        return None

    health_path = service.get("health")
    if health_path is None:
        return None

    return get_service_url(name, health_path)


def list_services() -> dict[str, Any]:
    """List all services from the manifest."""
    manifest = _load_manifest()
    result: dict[str, Any] = manifest.get("services", {})
    return result


def get_dependencies(name: str) -> list[str]:
    """Get dependency list for a service."""
    service = get_service(name)
    if service is None:
        return []
    deps: list[str] = service.get("dependencies", [])
    return deps


def get_systemd_unit(name: str) -> str | None:
    """Get the systemd unit name for a service."""
    service = get_service(name)
    if service is None:
        return None
    return service.get("systemd_unit")


def wait_for_service(name: str, timeout: float = 30.0, interval: float = 1.0) -> bool:
    """Wait for a service's health endpoint to respond."""
    import httpx

    health_url = get_health_url(name)
    if health_url is None:
        logger.warning("Service '%s' has no health endpoint", name)
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
    """Return services in dependency order (leaves first).

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


def _reset_cache() -> None:
    """Reset the manifest cache (for testing)."""
    global _manifest, _manifest_mtime
    _manifest = None
    _manifest_mtime = 0.0
