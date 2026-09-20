"""A heartbeat or drain run must not be a softer agent than the manifest.

``_build_heartbeat_config`` and ``_build_worker_config`` rebuilt ``AgentConfig``
field by field, so every field they forgot to name fell back to the dataclass
default. That has already caused one incident: the worker override omitted
``guardrails`` and ``sandbox`` and every drain run executed with no guardrails
in a local sandbox (2026-07-13). The comment in the file says so.

It happened again with the new spawn admission controls. ``spawn_allowed_agents``
and ``max_spawn_total`` are shipped as security controls, and neither builder
mentions them — so on every heartbeat, worker and drain run (the unattended
ones) the allowlist dropped back to ``[]`` (unrestricted) and the total
allowance to ``0`` (unlimited). Same for ``response_format``, ``provider_order``
and ``fleet_release_id``.

The fix is ``dataclasses.replace``: name what the override CHANGES, and a field
nobody thought about is inherited rather than silently reset.
"""

from __future__ import annotations

import dataclasses

import pytest

from robothor.engine.models import AgentConfig, HeartbeatConfig, WorkerConfig
from robothor.engine.schedule_overrides import _build_heartbeat_config, _build_worker_config

#: Fields an override run must never relax on its own. Not an exhaustive
#: security list — a named set is what drifted last time.
ADMISSION_FIELDS = (
    "spawn_allowed_agents",
    "max_spawn_total",
    "response_format",
    "provider_order",
    "fleet_release_id",
    "guardrails",
    "sandbox",
    "exec_allowlist",
    "write_path_allowlist",
    "human_approval_tools",
    "tools_denied",
    "credential_tier",
    "secret_grants",
)


def _restrictive():
    config = AgentConfig(
        id="main",
        name="Main",
        can_spawn_agents=True,
        spawn_allowed_agents=["ticket-router"],
        max_spawn_total=3,
        response_format={"type": "json_object"},
        provider_order=["deepseek"],
        guardrails=["no_secret_exfiltration"],
        sandbox="podman",
        exec_allowlist=["git"],
        write_path_allowlist=["/workspace"],
        human_approval_tools=["send_email"],
        tools_denied=["exec"],
        credential_tier="operator",
        secret_grants=["providers/example/api_key"],
        heartbeat=HeartbeatConfig(cron_expr="*/30 * * * *", instruction_file="brain/BEAT.md"),
        worker=WorkerConfig(cron_expr="0 */4 * * *", instruction_file="brain/WORK.md"),
    )
    config.fleet_release_id = "a" * 64
    return config


@pytest.mark.parametrize("build", [_build_heartbeat_config, _build_worker_config])
@pytest.mark.parametrize("name", ADMISSION_FIELDS)
def test_an_unattended_run_inherits_every_admission_control(build, name):
    parent = _restrictive()

    assert getattr(build(parent), name) == getattr(parent, name)


@pytest.mark.parametrize("build", [_build_heartbeat_config, _build_worker_config])
def test_an_override_changes_only_what_the_override_block_names(build):
    """Whitelisting fields is how they drift; this pins the complement."""
    parent = _restrictive()
    changed = {
        f.name
        for f in dataclasses.fields(AgentConfig)
        if getattr(build(parent), f.name) != getattr(parent, f.name)
    }

    assert changed <= {
        "cron_expr",
        "timezone",
        "timeout_seconds",
        "max_iterations",
        "safety_cap",
        "session_target",
        "delivery_mode",
        "delivery_channel",
        "delivery_to",
        "instruction_file",
        "bootstrap_files",
        "stall_timeout_seconds",
        "early_stall_timeout_seconds",
        "persistent_history_limit",
        "max_cost_usd",
        "hard_budget",
        "task_author_override",
        "model_primary",
        "model_fallbacks",
        "tools_allowed",
        "warmup_memory_blocks",
        "warmup_context_files",
        "warmup_peer_agents",
    }
