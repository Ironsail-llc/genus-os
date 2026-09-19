"""Manifest-derived worker and heartbeat overrides, independent of scheduling."""

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

    warmup_memory_blocks = w.warmup_memory_blocks or agent_config.warmup_memory_blocks
    warmup_context_files = w.warmup_context_files or agent_config.warmup_context_files
    warmup_peer_agents = w.warmup_peer_agents or agent_config.warmup_peer_agents

    max_cost_usd = w.cost_budget_usd or agent_config.max_cost_usd
    hard_budget = w.cost_budget_usd > 0 or agent_config.hard_budget

    tools_allowed = w.tools_allowed or agent_config.tools_allowed

    return AgentConfig(
        # SECURITY POSTURE — carried over verbatim. The worker override exists to
        # change budget and warmup for a drain cycle; it must never quietly relax
        # what the agent is allowed to do. These were previously omitted, so the
        # dataclass defaults applied and every worker run executed with ZERO
        # guardrails and sandbox="local" (2026-07-13).
        guardrails=agent_config.guardrails,
        guardrails_opt_out=agent_config.guardrails_opt_out,
        sandbox=agent_config.sandbox,
        exec_allowlist=agent_config.exec_allowlist,
        write_path_allowlist=agent_config.write_path_allowlist,
        human_approval_tools=agent_config.human_approval_tools,
        human_approval_timeout=agent_config.human_approval_timeout,
        id=agent_config.id,
        name=agent_config.name,
        description=agent_config.description,
        model_primary=agent_config.model_primary,
        model_fallbacks=agent_config.model_fallbacks,
        temperature=agent_config.temperature,
        cron_expr=w.cron_expr,
        timezone=w.timezone,
        timeout_seconds=w.timeout_seconds,
        max_iterations=w.max_iterations,
        safety_cap=w.safety_cap,
        session_target=w.session_target,
        delivery_mode=w.delivery_mode,
        delivery_channel=w.delivery_channel,
        delivery_to=w.delivery_to,
        tools_allowed=tools_allowed,
        tools_denied=agent_config.tools_denied,
        instruction_file=w.instruction_file,
        bootstrap_files=w.bootstrap_files,
        reports_to=agent_config.reports_to,
        department=agent_config.department,
        task_protocol=agent_config.task_protocol,
        review_workflow=agent_config.review_workflow,
        notification_inbox=agent_config.notification_inbox,
        shared_working_state=agent_config.shared_working_state,
        warmup_memory_blocks=warmup_memory_blocks,
        warmup_context_files=warmup_context_files,
        warmup_peer_agents=warmup_peer_agents,
        stall_timeout_seconds=w.stall_timeout_seconds,
        early_stall_timeout_seconds=w.early_stall_timeout_seconds,
        persistent_history_limit=w.persistent_history_limit,
        error_feedback=agent_config.error_feedback,
        max_cost_usd=max_cost_usd,
        hard_budget=hard_budget,
        can_spawn_agents=agent_config.can_spawn_agents,
        max_nesting_depth=agent_config.max_nesting_depth,
        sub_agent_max_iterations=agent_config.sub_agent_max_iterations,
        sub_agent_timeout_seconds=agent_config.sub_agent_timeout_seconds,
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

    # Inherit parent warmup if heartbeat doesn't specify its own
    warmup_memory_blocks = hb.warmup_memory_blocks or agent_config.warmup_memory_blocks
    warmup_context_files = hb.warmup_context_files or agent_config.warmup_context_files
    warmup_peer_agents = hb.warmup_peer_agents or agent_config.warmup_peer_agents

    # Cost cap: heartbeat override wins; fall back to parent agent's cap.
    # When the heartbeat sets its own budget we force hard-budget semantics so
    # the override actually bites; otherwise inherit whatever the parent agent
    # configured.
    max_cost_usd = hb.cost_budget_usd or agent_config.max_cost_usd
    hard_budget = hb.cost_budget_usd > 0 or agent_config.hard_budget

    # Model override: use heartbeat's model if set, else inherit from parent.
    beat_model_primary = hb.model_primary or agent_config.model_primary
    beat_model_fallbacks = hb.model_fallbacks or agent_config.model_fallbacks

    return AgentConfig(
        id=agent_config.id,
        name=agent_config.name,
        description=agent_config.description,
        model_primary=beat_model_primary,
        model_fallbacks=beat_model_fallbacks,
        temperature=agent_config.temperature,
        cron_expr=hb.cron_expr,
        timezone=hb.timezone,
        timeout_seconds=hb.timeout_seconds,
        max_iterations=hb.max_iterations,
        safety_cap=hb.safety_cap,
        session_target=hb.session_target,
        delivery_mode=hb.delivery_mode,
        delivery_channel=hb.delivery_channel,
        delivery_to=hb.delivery_to,
        tools_allowed=(hb.tools_allowed or agent_config.tools_allowed),
        tools_denied=agent_config.tools_denied,
        instruction_file=hb.instruction_file,
        bootstrap_files=hb.bootstrap_files,
        reports_to=agent_config.reports_to,
        department=agent_config.department,
        task_protocol=agent_config.task_protocol,
        review_workflow=agent_config.review_workflow,
        notification_inbox=agent_config.notification_inbox,
        shared_working_state=agent_config.shared_working_state,
        warmup_memory_blocks=warmup_memory_blocks,
        warmup_context_files=warmup_context_files,
        warmup_peer_agents=warmup_peer_agents,
        stall_timeout_seconds=hb.stall_timeout_seconds,
        early_stall_timeout_seconds=hb.early_stall_timeout_seconds,
        persistent_history_limit=hb.persistent_history_limit,
        error_feedback=agent_config.error_feedback,
        max_cost_usd=max_cost_usd,
        hard_budget=hard_budget,
        # Sub-agent config inherited from parent
        can_spawn_agents=agent_config.can_spawn_agents,
        max_nesting_depth=agent_config.max_nesting_depth,
        sub_agent_max_iterations=agent_config.sub_agent_max_iterations,
        sub_agent_timeout_seconds=agent_config.sub_agent_timeout_seconds,
        # Scout filings attributed to `hb.task_authorship_agent` (if set)
        # for CRM timeline clarity — agent_id on the run stays 'main'.
        task_author_override=hb.task_authorship_agent,
    )
