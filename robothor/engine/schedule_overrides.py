"""Manifest-derived worker and heartbeat overrides, independent of scheduling.

Both builders use ``dataclasses.replace``: they name what the override BLOCK
changes, and everything else is inherited.

They used to construct a fresh ``AgentConfig`` field by field instead, which
means every field nobody remembered to list silently fell back to the
dataclass default — and a dataclass default is the *permissive* end of every
security field in this system. That has bitten twice:

* 2026-07-13 — the worker override omitted ``guardrails`` and ``sandbox``, so
  every drain run executed with zero guardrails and ``sandbox="local"``. Those
  four fields were then added to ``_build_worker_config`` only; the heartbeat
  builder was left dropping them.
* This change — the new spawn admission controls (``spawn_allowed_agents``,
  ``max_spawn_total``) plus ``response_format``, ``provider_order`` and
  ``fleet_release_id`` were in neither builder, so the allowlist reset to
  ``[]`` (unrestricted) and the total allowance to ``0`` (unlimited) on exactly
  the runs nobody is watching.

Whitelists of security fields drift. Inheritance does not. Do not go back to
listing fields: ``test_schedule_overrides_carry_admission.py`` pins both the
admission fields and the complement (what an override is allowed to change).
"""

from dataclasses import replace

from robothor.engine.models import AgentConfig


def _build_worker_config(agent_config: AgentConfig) -> AgentConfig:
    """Build override AgentConfig for drain/worker runs.

    Mirrors _build_heartbeat_config but for the drain cycle: full tool
    inheritance by default (worker executes work; it needs spawn_agent,
    gws_*, exec, etc.). Set `worker.tools_allowed` in the manifest to
    restrict further if needed.
    """
    w = agent_config.worker
    assert w is not None

    return replace(
        agent_config,
        cron_expr=w.cron_expr,
        timezone=w.timezone,
        timeout_seconds=w.timeout_seconds,
        max_iterations=w.max_iterations,
        safety_cap=w.safety_cap,
        session_target=w.session_target,
        delivery_mode=w.delivery_mode,
        delivery_channel=w.delivery_channel,
        delivery_to=w.delivery_to,
        tools_allowed=w.tools_allowed or agent_config.tools_allowed,
        instruction_file=w.instruction_file,
        bootstrap_files=w.bootstrap_files,
        warmup_memory_blocks=w.warmup_memory_blocks or agent_config.warmup_memory_blocks,
        warmup_context_files=w.warmup_context_files or agent_config.warmup_context_files,
        warmup_peer_agents=w.warmup_peer_agents or agent_config.warmup_peer_agents,
        stall_timeout_seconds=w.stall_timeout_seconds,
        early_stall_timeout_seconds=w.early_stall_timeout_seconds,
        persistent_history_limit=w.persistent_history_limit,
        max_cost_usd=w.cost_budget_usd or agent_config.max_cost_usd,
        hard_budget=w.cost_budget_usd > 0 or agent_config.hard_budget,
        # Drain runs do NOT override task authorship — filed tasks stay
        # attributed to 'main' (the agent identity).
        task_author_override="",
    )


def _build_heartbeat_config(agent_config: AgentConfig) -> AgentConfig:
    """Build override AgentConfig for heartbeat runs.

    Inherits model + tools from parent agent, overrides instruction file,
    delivery, warmup, and budget from heartbeat config.
    Falls back to parent warmup config if heartbeat doesn't specify its own.
    """
    hb = agent_config.heartbeat
    assert hb is not None

    return replace(
        agent_config,
        # Model override: use heartbeat's model if set, else inherit.
        model_primary=hb.model_primary or agent_config.model_primary,
        model_fallbacks=hb.model_fallbacks or agent_config.model_fallbacks,
        cron_expr=hb.cron_expr,
        timezone=hb.timezone,
        timeout_seconds=hb.timeout_seconds,
        max_iterations=hb.max_iterations,
        safety_cap=hb.safety_cap,
        session_target=hb.session_target,
        delivery_mode=hb.delivery_mode,
        delivery_channel=hb.delivery_channel,
        delivery_to=hb.delivery_to,
        tools_allowed=hb.tools_allowed or agent_config.tools_allowed,
        instruction_file=hb.instruction_file,
        bootstrap_files=hb.bootstrap_files,
        warmup_memory_blocks=hb.warmup_memory_blocks or agent_config.warmup_memory_blocks,
        warmup_context_files=hb.warmup_context_files or agent_config.warmup_context_files,
        warmup_peer_agents=hb.warmup_peer_agents or agent_config.warmup_peer_agents,
        stall_timeout_seconds=hb.stall_timeout_seconds,
        early_stall_timeout_seconds=hb.early_stall_timeout_seconds,
        persistent_history_limit=hb.persistent_history_limit,
        # Cost cap: heartbeat override wins; fall back to the parent's cap.
        # When the heartbeat sets its own budget we force hard-budget semantics
        # so the override actually bites.
        max_cost_usd=hb.cost_budget_usd or agent_config.max_cost_usd,
        hard_budget=hb.cost_budget_usd > 0 or agent_config.hard_budget,
        # Scout filings attributed to `hb.task_authorship_agent` (if set)
        # for CRM timeline clarity — agent_id on the run stays 'main'.
        task_author_override=hb.task_authorship_agent,
    )
