"""
Engine configuration — loads agent configs from YAML manifests and env vars.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from robothor.engine.models import AgentConfig, AgentHook, DeliveryMode, HeartbeatConfig

logger = logging.getLogger(__name__)

# Bootstrap file limits
BOOTSTRAP_MAX_CHARS_PER_FILE = 12_000
BOOTSTRAP_TOTAL_MAX_CHARS = 30_000


@dataclass(frozen=True)
class EngineConfig:
    """Top-level engine configuration from environment variables."""

    # Telegram
    bot_token: str = ""
    default_chat_id: str = ""

    # Engine
    port: int = 18800
    tenant_id: str = "robothor-primary"

    # Paths
    workspace: Path = field(default_factory=lambda: Path.home() / "robothor")
    manifest_dir: Path = field(default_factory=lambda: Path.home() / "robothor" / "docs" / "agents")
    workflow_dir: Path = field(
        default_factory=lambda: Path.home() / "robothor" / "docs" / "workflows"
    )

    # Scheduler
    max_concurrent_agents: int = 3
    default_timezone: str = "America/New_York"

    # LLM
    max_iterations: int = 20

    # Default agent for interactive chat (Telegram + webchat)
    default_chat_agent: str = "main"

    @classmethod
    def from_env(cls) -> EngineConfig:
        workspace = Path(os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor"))
        return cls(
            bot_token=os.environ.get("ROBOTHOR_TELEGRAM_BOT_TOKEN", "")
            or os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            default_chat_id=os.environ.get("ROBOTHOR_TELEGRAM_CHAT_ID", "")
            or os.environ.get("TELEGRAM_CHAT_ID", "7636850023"),
            port=int(os.environ.get("ROBOTHOR_ENGINE_PORT", "18800")),
            tenant_id=os.environ.get("ROBOTHOR_TENANT_ID", "robothor-primary"),
            workspace=workspace,
            manifest_dir=Path(
                os.environ.get("ROBOTHOR_MANIFEST_DIR", workspace / "docs" / "agents")
            ),
            workflow_dir=Path(
                os.environ.get("ROBOTHOR_WORKFLOW_DIR", workspace / "docs" / "workflows")
            ),
            max_concurrent_agents=int(os.environ.get("ROBOTHOR_MAX_CONCURRENT_AGENTS", "3")),
            default_timezone=os.environ.get("ROBOTHOR_TIMEZONE", "America/New_York"),
            max_iterations=int(os.environ.get("ROBOTHOR_MAX_ITERATIONS", "20")),
            default_chat_agent=os.environ.get("ROBOTHOR_DEFAULT_CHAT_AGENT", "main"),
        )


def load_manifest(manifest_path: Path) -> dict | None:  # type: ignore[type-arg]
    """Load a single YAML manifest file."""
    try:
        with open(manifest_path) as f:
            data = yaml.safe_load(f)
        if data and isinstance(data, dict) and "id" in data:
            return data  # type: ignore[no-any-return]
        return None
    except Exception as e:
        logger.error("Failed to load manifest %s: %s", manifest_path, e)
        return None


def load_all_manifests(manifest_dir: Path) -> list[dict]:
    """Load all YAML manifests from a directory."""
    manifests: list[dict] = []
    if not manifest_dir.is_dir():
        logger.warning("Manifest directory not found: %s", manifest_dir)
        return manifests
    for f in sorted(manifest_dir.glob("*.yaml")):
        data = load_manifest(f)
        if data:
            manifests.append(data)
    return manifests


def manifest_to_agent_config(manifest: dict) -> AgentConfig:
    """Convert a YAML manifest dict to an AgentConfig."""
    model = manifest.get("model", {})
    schedule = manifest.get("schedule", {})
    delivery = manifest.get("delivery", {})
    streams = manifest.get("streams", {})
    warmup = manifest.get("warmup", {})

    delivery_mode_str = delivery.get("mode", "none")
    try:
        delivery_mode = DeliveryMode(delivery_mode_str)
    except ValueError:
        delivery_mode = DeliveryMode.NONE

    # Parse hooks
    raw_hooks = manifest.get("hooks", [])
    parsed_hooks: list[AgentHook] = []
    for h in raw_hooks:
        if isinstance(h, dict) and h.get("stream") and h.get("event_type"):
            parsed_hooks.append(
                AgentHook(
                    stream=h["stream"],
                    event_type=h["event_type"],
                    message=h.get("message", ""),
                )
            )
        else:
            logger.warning(
                "Invalid hook entry in %s (needs stream + event_type): %s",
                manifest.get("id", "?"),
                h,
            )

    # Parse heartbeat config
    raw_heartbeat = manifest.get("heartbeat", {})
    heartbeat: HeartbeatConfig | None = None
    if raw_heartbeat and raw_heartbeat.get("cron"):
        hb_delivery = raw_heartbeat.get("delivery", {})
        hb_delivery_mode_str = hb_delivery.get("mode", "announce")
        try:
            hb_delivery_mode = DeliveryMode(hb_delivery_mode_str)
        except ValueError:
            hb_delivery_mode = DeliveryMode.ANNOUNCE
        heartbeat = HeartbeatConfig(
            cron_expr=raw_heartbeat["cron"],
            timezone=raw_heartbeat.get("timezone", schedule.get("timezone", "America/New_York")),
            instruction_file=raw_heartbeat.get("instruction_file", ""),
            session_target=raw_heartbeat.get("session_target", "isolated"),
            max_iterations=int(raw_heartbeat.get("max_iterations", 15)),
            timeout_seconds=int(raw_heartbeat.get("timeout_seconds", 600)),
            delivery_mode=hb_delivery_mode,
            delivery_channel=hb_delivery.get("channel", ""),
            delivery_to=hb_delivery.get("to", ""),
            warmup_context_files=raw_heartbeat.get("context_files", []),
            warmup_peer_agents=raw_heartbeat.get("peer_agents", []),
            warmup_memory_blocks=raw_heartbeat.get("memory_blocks", []),
            bootstrap_files=raw_heartbeat.get("bootstrap_files", []),
            token_budget=int(raw_heartbeat.get("token_budget", 0)),
            cost_budget_usd=float(raw_heartbeat.get("cost_budget_usd", 0.0)),
        )

    # v2 enhancement fields
    v2 = manifest.get("v2", {})

    return AgentConfig(
        id=manifest["id"],
        name=manifest.get("name", manifest["id"]),
        description=manifest.get("description", ""),
        model_primary=model.get("primary", ""),
        model_fallbacks=model.get("fallbacks", []),
        cron_expr=schedule.get("cron", ""),
        timezone=schedule.get("timezone", "America/New_York"),
        timeout_seconds=schedule.get("timeout_seconds", 600),
        max_iterations=schedule.get("max_iterations", 20),
        temperature=float(model.get("temperature", 0.3)),
        session_target=schedule.get("session_target", "isolated"),
        delivery_mode=delivery_mode,
        delivery_channel=delivery.get("channel", ""),
        delivery_to=delivery.get("to", ""),
        tools_allowed=manifest.get("tools_allowed", []),
        tools_denied=manifest.get("tools_denied", []),
        instruction_file=manifest.get("instruction_file", ""),
        bootstrap_files=manifest.get("bootstrap_files", []),
        reports_to=manifest.get("reports_to", ""),
        department=manifest.get("department", ""),
        task_protocol=manifest.get("task_protocol", False),
        review_workflow=manifest.get("review_workflow", False),
        notification_inbox=manifest.get("notification_inbox", False),
        shared_working_state=manifest.get("shared_working_state", False),
        status_file=manifest.get("status_file", ""),
        sla=manifest.get("sla", {}),
        streams_read=streams.get("read", []),
        streams_write=streams.get("write", []),
        warmup_memory_blocks=warmup.get("memory_blocks", []),
        warmup_context_files=warmup.get("context_files", []),
        warmup_peer_agents=warmup.get("peer_agents", []),
        downstream_agents=manifest.get("downstream_agents", []),
        hooks=parsed_hooks,
        heartbeat=heartbeat,
        # v2 enhancements — sub-agent spawning
        can_spawn_agents=v2.get("can_spawn_agents", False),
        max_nesting_depth=min(int(v2.get("max_nesting_depth", 2)), 3),  # cap at 3
        sub_agent_max_iterations=int(v2.get("sub_agent_max_iterations", 10)),
        sub_agent_timeout_seconds=int(v2.get("sub_agent_timeout_seconds", 120)),
        # v2 enhancements
        error_feedback=v2.get("error_feedback", True),
        token_budget=int(v2.get("token_budget", 0)),
        cost_budget_usd=float(v2.get("cost_budget_usd", 0.0)),
        planning_enabled=v2.get("planning_enabled", False),
        planning_model=v2.get("planning_model", ""),
        scratchpad_enabled=v2.get("scratchpad_enabled", False),
        guardrails=v2.get("guardrails", []),
        checkpoint_enabled=v2.get("checkpoint_enabled", False),
        verification_enabled=v2.get("verification_enabled", False),
        verification_prompt=v2.get("verification_prompt", ""),
        difficulty_class=v2.get("difficulty_class", ""),
    )


def load_agent_config(agent_id: str, manifest_dir: Path) -> AgentConfig | None:
    """Load a single agent config by ID from the manifest directory."""
    manifest_path = manifest_dir / f"{agent_id}.yaml"
    if manifest_path.exists():
        data = load_manifest(manifest_path)
        if data:
            return manifest_to_agent_config(data)
    # Fallback: scan all manifests for matching ID
    for m in load_all_manifests(manifest_dir):
        if m["id"] == agent_id:
            return manifest_to_agent_config(m)
    return None


def build_system_prompt(config: AgentConfig, workspace: Path) -> str:
    """Build the full system prompt from instruction + bootstrap files.

    Respects per-file and total char limits.
    """
    parts: list[str] = []
    total_chars = 0

    # Load instruction file first (primary)
    if config.instruction_file:
        instruction_path = workspace / config.instruction_file
        if instruction_path.exists():
            content = instruction_path.read_text()
            if len(content) > BOOTSTRAP_MAX_CHARS_PER_FILE:
                content = content[:BOOTSTRAP_MAX_CHARS_PER_FILE]
                logger.warning(
                    "Instruction file %s truncated to %d chars",
                    config.instruction_file,
                    BOOTSTRAP_MAX_CHARS_PER_FILE,
                )
            parts.append(content)
            total_chars += len(content)
        else:
            logger.warning("Instruction file not found: %s", instruction_path)

    # Load bootstrap files
    for bs_file in config.bootstrap_files:
        if total_chars >= BOOTSTRAP_TOTAL_MAX_CHARS:
            logger.warning("Bootstrap total limit reached, skipping remaining files")
            break

        bs_path = workspace / bs_file
        if not bs_path.exists():
            logger.warning("Bootstrap file not found: %s", bs_path)
            continue

        content = bs_path.read_text()
        remaining = BOOTSTRAP_TOTAL_MAX_CHARS - total_chars
        max_this_file = min(BOOTSTRAP_MAX_CHARS_PER_FILE, remaining)
        if len(content) > max_this_file:
            content = content[:max_this_file]
            logger.warning("Bootstrap file %s truncated to %d chars", bs_file, max_this_file)
        parts.append(content)
        total_chars += len(content)

    return "\n\n---\n\n".join(parts)
