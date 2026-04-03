"""Business Adapters — load external MCP server configs for agent tool discovery.

Adapters let you plug business-specific MCP servers (healthcare, CRM, ERP, etc.)
into the engine without hardcoding handlers. Each adapter is a YAML file in
``~/.config/robothor/adapters/`` that declares an MCP server connection.

On agent startup the engine loads adapters, connects to their MCP servers,
discovers available tools via ``tools/list``, and registers them as first-class
tools in the ToolRegistry. Agents reference tool names in their manifest's
``tools_allowed`` list as usual — no special syntax needed.

Adapter YAML format::

    name: my-adapter
    transport: http            # "http" or "stdio"
    url: "${BASE_URL}/_mcp"    # HTTP transport
    headers:
      Authorization: "Bearer ${API_TOKEN}"
    # OR for stdio:
    # transport: stdio
    # command: ["node", "bridge.mjs"]
    # env: { TOKEN: "${MY_TOKEN}" }
    timeout_seconds: 30
    agents: ["main"]           # or ["*"] for all agents
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)}")

ADAPTER_DIR = Path.home() / ".config" / "robothor" / "adapters"


@dataclass(frozen=True)
class AdapterConfig:
    """Configuration for one business adapter (external MCP server)."""

    name: str
    transport: str  # "http" or "stdio"
    # HTTP transport
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # stdio transport
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # Common
    timeout_seconds: int = 30
    agents: list[str] = field(default_factory=lambda: ["*"])


def _resolve_env(value: str) -> str:
    """Replace ``${VAR}`` placeholders with environment variable values."""

    def _repl(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), "")

    return _ENV_VAR_RE.sub(_repl, value)


def _resolve_dict(d: dict[str, str]) -> dict[str, str]:
    return {k: _resolve_env(v) for k, v in d.items()}


def _resolve_list(lst: list[str]) -> list[str]:
    return [_resolve_env(v) for v in lst]


def _parse_adapter(data: dict[str, Any]) -> AdapterConfig | None:
    """Parse a single adapter YAML dict into an AdapterConfig."""
    name = data.get("name", "")
    transport = data.get("transport", "stdio")
    if not name:
        logger.warning("Adapter config missing 'name', skipping")
        return None
    if transport not in ("http", "stdio"):
        logger.warning("Adapter '%s' has unknown transport '%s', skipping", name, transport)
        return None

    return AdapterConfig(
        name=name,
        transport=transport,
        url=_resolve_env(data.get("url", "")),
        headers=_resolve_dict(data.get("headers", {})),
        command=_resolve_list(data.get("command", [])),
        env=_resolve_dict(data.get("env", {})),
        timeout_seconds=int(data.get("timeout_seconds", 30)),
        agents=data.get("agents", ["*"]),
    )


def load_adapters(adapter_dir: Path | None = None) -> list[AdapterConfig]:
    """Load all adapter configs from the adapters directory.

    Returns an empty list if the directory doesn't exist (no adapters configured).
    """
    d = adapter_dir or ADAPTER_DIR
    if not d.is_dir():
        return []

    import yaml

    adapters: list[AdapterConfig] = []
    for path in sorted(d.glob("*.yaml")):
        try:
            with path.open() as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                logger.warning("Adapter file %s is not a YAML mapping, skipping", path)
                continue
            adapter = _parse_adapter(data)
            if adapter:
                adapters.append(adapter)
                logger.info(
                    "Loaded adapter '%s' (%s) from %s", adapter.name, adapter.transport, path
                )
        except Exception:
            logger.exception("Failed to load adapter config from %s", path)

    return adapters


def get_adapters_for_agent(
    agent_id: str,
    adapters: list[AdapterConfig] | None = None,
) -> list[AdapterConfig]:
    """Return adapters that should be available to the given agent."""
    if adapters is None:
        adapters = load_adapters()
    return [a for a in adapters if "*" in a.agents or agent_id in a.agents]
