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


# ── auto_task is the one field an override must NOT simply inherit ─────────
#
# `dataclasses.replace` fixed a whitelist that was dropping security fields,
# and it necessarily carries every other field too — including `auto_task`,
# which files one operator-facing CRM row per run. On this instance that is
# nine scheduled agents making ~138 runs a day, so inheriting it silently
# would put roughly 4,000 rows a month into the operator's queue. The runner's
# own `should_create_auto_task` docstring records the last time that happened:
# 6,887 junk rows that sat there as TODO.
#
# So it stays off for an override run unless the override block asks for it.
# Not because inheriting is wrong in principle, but because turning it on for
# nine agents is a decision, not a side effect of a refactor.


@pytest.mark.parametrize("build", [_build_heartbeat_config, _build_worker_config])
def test_an_override_run_does_not_file_a_crm_task_just_because_the_agent_does(build):
    parent = _restrictive()
    parent.auto_task = True

    assert build(parent).auto_task is False


@pytest.mark.parametrize(
    ("build", "block"), [(_build_heartbeat_config, "heartbeat"), (_build_worker_config, "worker")]
)
def test_an_override_block_can_ask_for_its_own_crm_task(build, block):
    parent = _restrictive()
    parent.auto_task = False
    getattr(parent, block).auto_task = True

    assert build(parent).auto_task is True


@pytest.mark.parametrize("block", ["heartbeat", "worker"])
def test_the_manifest_can_actually_set_it(block):
    """An opt-in no manifest can express is an inert control."""
    import yaml

    schema = yaml.safe_load(
        (
            __import__("pathlib").Path(__file__).resolve().parents[1] / "schema/agent_manifest.yaml"
        ).read_text()
    )
    section = schema["optional"][block]

    assert section["auto_task"]["type"] == "boolean"
    assert section["auto_task"]["default"] is False
