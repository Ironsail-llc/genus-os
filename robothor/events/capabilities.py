"""
Agent RBAC — Capability checking for Robothor agents.

Reads agent_capabilities.json and provides fast lookup for:
- Tool access: can agent X use tool Y?
- Endpoint access: can agent X call endpoint Z?
- Stream access: can agent X read/write stream S?

Default policy is "allow" — missing agents get full access (backward compatible).
"""

import fnmatch
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Cached manifest
_manifest: dict | None = None


def _get_manifest_path() -> str:
    """Resolve the capabilities manifest path.

    Checks in order:
    1. ROBOTHOR_CAPABILITIES_MANIFEST env var (explicit override)
    2. {ROBOTHOR_WORKSPACE}/agent_capabilities.json
    3. {workspace}/agent_capabilities.json (from config)
    """
    explicit = os.environ.get("ROBOTHOR_CAPABILITIES_MANIFEST")
    if explicit:
        return explicit

    workspace = os.environ.get("ROBOTHOR_WORKSPACE")
    if workspace:
        return os.path.join(workspace, "agent_capabilities.json")

    try:
        from robothor.config import get_config

        return str(get_config().workspace / "agent_capabilities.json")
    except Exception:
        return os.path.join(os.path.expanduser("~"), "robothor", "agent_capabilities.json")


def load_capabilities(path: str | None = None) -> dict:
    """Load the agent capabilities manifest.

    Args:
        path: Optional path override (default: auto-resolved)

    Returns:
        Parsed manifest dict. Empty dict on load failure.
    """
    global _manifest
    manifest_path = path or _get_manifest_path()
    try:
        with open(manifest_path) as f:
            _manifest = json.load(f)
        return _manifest
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("Failed to load capabilities manifest: %s", e)
        _manifest = {}
        return _manifest


def _get_manifest() -> dict:
    """Get cached manifest, loading if needed."""
    if _manifest is None:
        load_capabilities()
    return _manifest or {}


def _get_agent(agent_id: str) -> dict | None:
    """Look up agent config by ID. Returns None if not found."""
    manifest = _get_manifest()
    agents: dict[str, Any] = manifest.get("agents", {})
    return agents.get(agent_id)


def get_default_policy() -> str:
    """Get the default policy for unknown agents."""
    policy: str = _get_manifest().get("default_policy", "allow")
    return policy


def check_tool_access(agent_id: str, tool_name: str) -> bool:
    """Check if an agent can use a specific tool.

    Args:
        agent_id: Agent identifier (e.g., "email-classifier")
        tool_name: Tool name (e.g., "create_person")

    Returns:
        True if access is allowed, False if denied.
        Unknown agents get full access (default_policy: allow).
    """
    agent = _get_agent(agent_id)
    if agent is None:
        return get_default_policy() == "allow"
    return tool_name in agent.get("tools", [])


def check_endpoint_access(agent_id: str, method: str, path: str) -> bool:
    """Check if an agent can call a Bridge endpoint.

    Args:
        agent_id: Agent identifier
        method: HTTP method (GET, POST, PATCH, DELETE)
        path: Request path (e.g., "/api/people/123")

    Returns:
        True if access is allowed, False if denied.
        Supports wildcard patterns (e.g., "GET /api/*").
    """
    agent = _get_agent(agent_id)
    if agent is None:
        return get_default_policy() == "allow"

    request = f"{method.upper()} {path}"
    for pattern in agent.get("bridge_endpoints", []):
        if fnmatch.fnmatch(request, pattern):
            return True
    return False


def check_stream_access(agent_id: str, stream: str, mode: str = "read") -> bool:
    """Check if an agent can read/write a stream.

    Args:
        agent_id: Agent identifier
        stream: Stream name (e.g., "email", "crm")
        mode: "read" or "write"

    Returns:
        True if access is allowed.
    """
    agent = _get_agent(agent_id)
    if agent is None:
        return get_default_policy() == "allow"

    if mode == "read":
        return stream in agent.get("streams_read", [])
    elif mode == "write":
        return stream in agent.get("streams_write", [])
    return False


def get_agent_tools(agent_id: str) -> list[str]:
    """Get the list of tools available to an agent.

    Returns all tools if agent is unknown (backward compat).
    """
    agent = _get_agent(agent_id)
    if agent is None:
        return []  # Unknown agent — empty but default_policy handles access
    tools: list[str] = agent.get("tools", [])
    return tools


def list_agents() -> list[str]:
    """List all known agent IDs."""
    manifest = _get_manifest()
    agents: dict[str, Any] = manifest.get("agents", {})
    return list(agents.keys())


def reset():
    """Reset the cached manifest (for testing)."""
    global _manifest
    _manifest = None
