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
    # protocol: "2026-07-28"   # optional — opt this server into the stateless
    #                          # MCP core once it has upgraded (default legacy)
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

ADAPTER_DIR = Path(
    os.environ.get("ROBOTHOR_ADAPTER_DIR", str(Path.home() / ".config" / "robothor" / "adapters"))
)


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
    # "legacy" (default) or "2026-07-28" (stateless) — see mcp_client.py.
    protocol: str = "legacy"
    # Metadata (optional, used by extension management API)
    version: str = ""
    author: str = ""
    description: str = ""
    # ── Bundle contract (2026-08-24) ──
    # tools_allowed: the ONLY tools this adapter may expose. Anything else the
    # server offers is DRIFT — not registered, logged loudly. Empty = legacy
    # allow-all (warned once at registration). This is the supply-chain
    # protection the field's plugin marketplaces lack: a compromised or
    # silently-updated server cannot sprout new capabilities into the fleet.
    tools_allowed: list[str] = field(default_factory=list)
    # command_sha256: pins the stdio executable (command[0]). A binary swap
    # under the same path refuses the adapter outright — fail-closed.
    command_sha256: str = ""


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
        protocol=data.get("protocol", "legacy"),
        version=data.get("version", ""),
        author=data.get("author", ""),
        description=data.get("description", ""),
        tools_allowed=list(data.get("tools_allowed", []) or []),
        # str() BEFORE the falsiness check: YAML parses an unquoted all-zeros
        # (or all-digits) hash as an INTEGER, and `int(0) or ""` silently
        # became "no pin declared" — a fail-open path for exactly the value an
        # attacker would love. None stays "", every other scalar is stringified.
        command_sha256=("" if data.get("command_sha256") is None else str(data["command_sha256"])),
    )


def verify_adapter_integrity(adapter: AdapterConfig) -> tuple[bool, str]:
    """Check a stdio adapter's pinned executable hash. ``(ok, reason)``.

    No pin declared -> passes with no claim: integrity is opt-in per adapter,
    and a pass here never asserts more than the config asked for. With a pin,
    the check is fail-closed: an unresolvable or unreadable binary refuses the
    adapter exactly like a mismatch — "could not check" must never degrade to
    "allowed", or the attacker's easiest move is breaking the check.
    """
    if adapter.transport != "stdio" or not adapter.command_sha256:
        return True, "no integrity pin declared"
    if not adapter.command:
        return False, "command_sha256 declared but no command to verify"

    import hashlib
    import shutil

    exe = adapter.command[0]
    resolved = exe if Path(exe).is_absolute() else (shutil.which(exe) or "")
    if not resolved or not Path(resolved).is_file():
        return False, f"pinned executable not found: {exe}"
    try:
        digest = hashlib.sha256(Path(resolved).read_bytes()).hexdigest()
    except OSError as e:
        return False, f"pinned executable unreadable: {e}"
    if digest != adapter.command_sha256.lower():
        return False, (
            f"sha256 mismatch for {resolved}: expected {adapter.command_sha256[:12]}…, "
            f"got {digest[:12]}… — the binary changed since it was pinned"
        )
    return True, "sha256 verified"


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


# ── Hot-reload cache ─────────────────────────────────────────────────────────

_loaded_adapters: list[AdapterConfig] = []


def get_loaded_adapters() -> list[AdapterConfig]:
    """Return the most recently loaded adapter configs (from cache)."""
    return _loaded_adapters


def refresh_adapters(adapter_dir: Path | None = None) -> list[AdapterConfig]:
    """Reload all adapters from disk and update the module-level cache."""
    global _loaded_adapters
    _loaded_adapters = load_adapters(adapter_dir)
    return _loaded_adapters
