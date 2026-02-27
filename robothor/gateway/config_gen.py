"""Generate openclaw.json and jobs.json from YAML agent manifests.

The base template provides static config (env, auth, models, defaults).
Agent manifests in docs/agents/*.yaml define each agent's model, tools,
schedule, and delivery settings. This module merges them into the final
config files deployed to the gateway config directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml


def load_manifests(manifest_dir: Path) -> list[dict]:
    """Load all YAML agent manifests from a directory."""
    manifests = []
    for f in sorted(manifest_dir.glob("*.yaml")):
        with open(f) as fp:
            data = yaml.safe_load(fp)
            if data and isinstance(data, dict) and "id" in data:
                manifests.append(data)
    return manifests


def _parse_model_spec(manifest: dict) -> dict:
    """Convert manifest model spec to openclaw agent config format."""
    model = manifest.get("model", {})
    primary = model.get("primary", "")

    result: dict = {}
    if primary:
        result["primary"] = primary
    fallbacks = model.get("fallbacks", [])
    if fallbacks:
        result["fallbacks"] = fallbacks
    return result


def _parse_tools(manifest: dict) -> dict | None:
    """Convert manifest tools to openclaw format."""
    denied = manifest.get("tools_denied", [])
    if not denied:
        return None
    return {"deny": denied}


def manifest_to_agent_entry(manifest: dict) -> dict:
    """Convert a single YAML manifest to an openclaw agents.list entry."""
    entry: dict = {
        "id": manifest["id"],
        "name": manifest.get("name", manifest["id"]),
        "workspace": "/home/philip/clawd",
    }

    model = _parse_model_spec(manifest)
    if model:
        entry["model"] = model

    tools = _parse_tools(manifest)
    if tools:
        entry["tools"] = tools

    # Heartbeat / delivery
    delivery = manifest.get("delivery", {})
    if delivery.get("channel") == "telegram":
        entry["heartbeat"] = {
            "every": "0",
            "target": "telegram",
            "to": delivery.get("to", ""),
        }
    else:
        entry["heartbeat"] = {"every": "0"}

    return entry


def manifest_to_job_entry(manifest: dict) -> dict | None:
    """Convert a single YAML manifest to a cron jobs.json entry.

    Returns None if the manifest has no schedule (not a cron agent).
    """
    schedule = manifest.get("schedule", {})
    cron = schedule.get("cron")
    if not cron:
        return None

    agent_id = manifest["id"]
    delivery = manifest.get("delivery", {})

    job: dict = {
        "id": f"{agent_id}-0001",
        "enabled": True,
        "name": manifest.get("name", agent_id),
        "schedule": cron,
        "timezone": schedule.get("timezone", "America/Grenada"),
        "agentId": agent_id,
        "timeoutSeconds": schedule.get("timeout_seconds", 300),
        "maxConcurrent": 1,
        "delivery": delivery.get("mode", "none"),
    }

    # Session target
    session_target = schedule.get("session_target", "isolated")
    if session_target == "isolated":
        job["sessionTarget"] = "isolated"

    return job


def generate_agents_list(
    manifests: list[dict],
    *,
    main_agent: dict | None = None,
) -> list[dict]:
    """Generate the agents.list array from manifests.

    Args:
        manifests: Loaded YAML manifests
        main_agent: Optional main agent entry (inserted first with default=True)
    """
    agents = []

    if main_agent:
        agents.append(main_agent)

    for m in manifests:
        agents.append(manifest_to_agent_entry(m))

    return agents


def generate_jobs_list(manifests: list[dict]) -> list[dict]:
    """Generate the cron jobs list from manifests."""
    jobs = []
    for m in manifests:
        job = manifest_to_job_entry(m)
        if job:
            jobs.append(job)
    return jobs


def generate_openclaw_config(
    manifest_dir: Path,
    base_template: dict,
) -> dict:
    """Generate complete openclaw.json from base template + manifests.

    The base_template should contain everything except agents.list,
    which is generated from the YAML manifests.
    """
    manifests = load_manifests(manifest_dir)

    config = json.loads(json.dumps(base_template))  # deep copy

    # Preserve main agent from base if present
    main_agent = None
    existing_agents = config.get("agents", {}).get("list", [])
    for a in existing_agents:
        if a.get("default") or a.get("id") == "main":
            main_agent = a
            break

    config.setdefault("agents", {})["list"] = generate_agents_list(
        manifests, main_agent=main_agent
    )

    return config


def generate_jobs_config(manifest_dir: Path) -> list[dict]:
    """Generate jobs.json content from YAML manifests."""
    manifests = load_manifests(manifest_dir)
    return generate_jobs_list(manifests)


def generate_and_deploy(
    manifest_dir: Path,
    config_dir: Path,
    *,
    dry_run: bool = False,
) -> int:
    """Generate and deploy config files.

    Args:
        manifest_dir: Path to docs/agents/ with YAML manifests
        config_dir: Path to gateway config directory (~/.openclaw/)
        dry_run: If True, print without writing

    Returns:
        0 on success, 1 on failure
    """
    if not manifest_dir.exists():
        print(f"Error: Manifest directory not found: {manifest_dir}")
        return 1

    manifests = load_manifests(manifest_dir)
    if not manifests:
        print(f"Warning: No agent manifests found in {manifest_dir}")

    # Load existing openclaw.json as base
    existing_config = config_dir / "openclaw.json"
    if existing_config.exists():
        base = json.loads(existing_config.read_text())
    else:
        base = {}

    # Generate
    config = generate_openclaw_config(manifest_dir, base)
    jobs = generate_jobs_config(manifest_dir)

    if dry_run:
        print("--- openclaw.json (agents.list) ---")
        print(json.dumps(config.get("agents", {}).get("list", []), indent=2))
        print()
        print("--- jobs.json ---")
        print(json.dumps(jobs, indent=2))
        return 0

    # Deploy
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "openclaw.json").write_text(json.dumps(config, indent=2) + "\n")

    cron_dir = config_dir / "cron"
    cron_dir.mkdir(exist_ok=True)
    (cron_dir / "jobs.json").write_text(json.dumps(jobs, indent=2) + "\n")

    print(f"  Generated openclaw.json ({len(manifests)} agents)")
    print(f"  Generated jobs.json ({len(jobs)} cron jobs)")
    return 0
