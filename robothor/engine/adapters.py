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
    # read_only: which of THIS adapter's tools have no side effects. Optional
    # and additive — an adapter that declares nothing declares nothing, and
    # absent means WRITE. Mirrors the plugin seam's `read_only` in
    # robothor/plugins/loader.py: safety classification used to be core's
    # hardcoded table, so an integration leaving core left a fact about one
    # instance behind in core. Every core deny-set (DESKTOP_TOOLS,
    # BENCHMARK_TOOLS, EXTERNAL_SIDE_EFFECT_TOOLS) is a list of literal core
    # tool names and can say nothing about `acme_delete_patient`, so a
    # benchmark harness must never infer "read-only" from mere membership in
    # tools_allowed. Must be a SUBSET of tools_allowed: classifying a tool
    # this adapter does not serve is privilege escalation, not extension, and
    # refuses the adapter at load.
    read_only: list[str] = field(default_factory=list)
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

    tools_allowed = list(data.get("tools_allowed", []) or [])

    # ── read_only: optional, additive, fail-closed ──
    # Validated here rather than at use so a malformed or over-reaching
    # declaration REFUSES the adapter, the same shape as an unknown transport.
    # A classification that is merely ignored is worse than absent: the
    # operator believes a boundary exists that nothing enforces.
    # Only None coalesces to "absent". `or []` would swallow `read_only: false`
    # and every other falsy scalar as "declared nothing" — the same fail-open
    # shape the command_sha256 comment below was written about.
    raw_read_only = data.get("read_only")
    read_only = [] if raw_read_only is None else raw_read_only
    if not isinstance(read_only, list | tuple) or not all(isinstance(x, str) for x in read_only):
        logger.warning(
            "Adapter '%s' has a non-list read_only — refused. It must be a list "
            "of tool names drawn from tools_allowed; use `read_only: []` (or "
            "omit the key) to declare nothing read-only.",
            name,
        )
        return None
    foreign = sorted(set(read_only) - set(tools_allowed))
    if foreign:
        # Empty tools_allowed is legacy allow-all, so it grounds no claim at
        # all — a read_only beside it is refused too, deliberately.
        logger.warning(
            "Adapter '%s' declares read_only %s not present in tools_allowed — refused. "
            "An adapter may only classify tools it actually serves.",
            name,
            foreign,
        )
        return None

    # ── An adapter may not name a CORE tool, in either list ──
    # The subset check above only proves read_only ⊆ tools_allowed. Both could
    # still name `delete_person` — a tool the adapter does not serve and cannot
    # speak for. Left open, an adapter YAML became a way to reclassify core's
    # own write tools as read-only, which is the whole boundary in one line.
    # The registry is the authority on what core owns; the plugin seam refuses
    # a name it does not provide for exactly this reason.
    claimed = set(tools_allowed) | set(read_only)
    if claimed:
        try:
            core_names = _core_tool_names()
        except Exception:
            logger.exception(
                "Adapter '%s' refused: could not read the core tool registry to "
                "check its declared tool names. Refusing rather than loading "
                "unchecked.",
                name,
            )
            return None
        stolen = sorted(claimed & core_names)
        if stolen:
            logger.warning(
                "Adapter '%s' declares core tool name(s) %s — refused. Those "
                "belong to core; an adapter may only name the tools its own "
                "server serves.",
                name,
                stolen,
            )
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
        tools_allowed=tools_allowed,
        read_only=list(read_only),
        # str() BEFORE the falsiness check: YAML parses an unquoted all-zeros
        # (or all-digits) hash as an INTEGER, and `int(0) or ""` silently
        # became "no pin declared" — a fail-open path for exactly the value an
        # attacker would love. None stays "", every other scalar is stringified.
        command_sha256=("" if data.get("command_sha256") is None else str(data["command_sha256"])),
    )


def _core_tool_names() -> frozenset[str]:
    """Every tool name CORE registers, from the registry itself.

    Same source ``test_every_registered_tool_is_classified`` reads, so the two
    can never disagree; a second hardcoded list here would rot the day someone
    adds a tool. Imported lazily because ``robothor.api.mcp`` and the engine
    schemas sit above this module.

    Fail-closed on purpose: if the registry cannot be read, ``_parse_adapter``
    refuses the adapters it was about to check rather than loading them
    unchecked. "Could not check" must never degrade to "allowed" — the same
    rule :func:`verify_adapter_integrity` follows for a pin it cannot verify.

    One residual limit, stated so nobody mistakes it for coverage: the engine
    schema set is flag-gated, so a core tool behind an OFF rip flag is not in
    here. That is also exactly the surface an adapter could collide with in
    this process, and the flag flipping ON is a restart, which re-runs this.
    """
    from robothor.api.mcp import get_tool_definitions
    from robothor.engine.tools.schemas import get_engine_schemas

    return frozenset({d["name"] for d in get_tool_definitions()} | set(get_engine_schemas()))


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
