"""Tests for gateway config generator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from robothor.gateway.config_gen import (
    generate_agents_list,
    generate_and_deploy,
    generate_jobs_config,
    generate_jobs_list,
    generate_openclaw_config,
    load_manifests,
    manifest_to_agent_entry,
    manifest_to_job_entry,
)


@pytest.fixture
def sample_manifest() -> dict:
    return {
        "id": "email-classifier",
        "name": "Email Classifier",
        "model": {
            "primary": "openrouter/moonshotai/kimi-k2.5",
            "fallbacks": ["openrouter/anthropic/claude-sonnet-4.6"],
        },
        "schedule": {
            "cron": "0 6-22/2 * * *",
            "timezone": "America/Grenada",
            "timeout_seconds": 300,
            "session_target": "isolated",
        },
        "delivery": {
            "mode": "announce",
            "channel": "telegram",
            "to": "7636850023",
        },
        "tools_denied": ["message"],
    }


@pytest.fixture
def manifest_dir(tmp_path: Path, sample_manifest: dict) -> Path:
    """Create a temp directory with YAML manifests."""
    d = tmp_path / "agents"
    d.mkdir()

    # Write sample manifest
    (d / "email-classifier.yaml").write_text(yaml.dump(sample_manifest))

    # Write a second manifest without schedule (non-cron)
    no_schedule = {
        "id": "manual-agent",
        "name": "Manual Agent",
        "model": {"primary": "openrouter/moonshotai/kimi-k2.5"},
    }
    (d / "manual-agent.yaml").write_text(yaml.dump(no_schedule))

    return d


class TestLoadManifests:
    def test_loads_yaml_files(self, manifest_dir: Path):
        manifests = load_manifests(manifest_dir)
        assert len(manifests) == 2
        ids = [m["id"] for m in manifests]
        assert "email-classifier" in ids
        assert "manual-agent" in ids

    def test_empty_dir(self, tmp_path: Path):
        d = tmp_path / "empty"
        d.mkdir()
        assert load_manifests(d) == []

    def test_skips_invalid_yaml(self, tmp_path: Path):
        d = tmp_path / "agents"
        d.mkdir()
        (d / "bad.yaml").write_text("just a string")
        (d / "no_id.yaml").write_text(yaml.dump({"name": "no id"}))
        assert load_manifests(d) == []


class TestManifestToAgentEntry:
    def test_basic_conversion(self, sample_manifest: dict):
        entry = manifest_to_agent_entry(sample_manifest)
        assert entry["id"] == "email-classifier"
        assert entry["name"] == "Email Classifier"
        assert entry["model"]["primary"] == "openrouter/moonshotai/kimi-k2.5"
        assert entry["model"]["fallbacks"] == ["openrouter/anthropic/claude-sonnet-4.6"]
        assert entry["tools"] == {"deny": ["message"]}

    def test_telegram_heartbeat(self, sample_manifest: dict):
        entry = manifest_to_agent_entry(sample_manifest)
        assert entry["heartbeat"]["target"] == "telegram"
        assert entry["heartbeat"]["to"] == "7636850023"

    def test_no_tools_denied(self):
        m = {"id": "test", "name": "Test"}
        entry = manifest_to_agent_entry(m)
        assert "tools" not in entry

    def test_no_delivery_channel(self):
        m = {"id": "test", "name": "Test", "delivery": {"mode": "none"}}
        entry = manifest_to_agent_entry(m)
        assert entry["heartbeat"] == {"every": "0"}


class TestManifestToJobEntry:
    def test_cron_agent(self, sample_manifest: dict):
        job = manifest_to_job_entry(sample_manifest)
        assert job is not None
        assert job["id"] == "email-classifier-0001"
        assert job["schedule"] == "0 6-22/2 * * *"
        assert job["agentId"] == "email-classifier"
        assert job["timeoutSeconds"] == 300
        assert job["delivery"] == "announce"

    def test_no_schedule(self):
        m = {"id": "manual", "name": "Manual"}
        assert manifest_to_job_entry(m) is None


class TestGenerateAgentsList:
    def test_with_main_agent(self, sample_manifest: dict):
        main = {"id": "main", "default": True}
        agents = generate_agents_list([sample_manifest], main_agent=main)
        assert agents[0]["id"] == "main"
        assert agents[1]["id"] == "email-classifier"

    def test_without_main(self, sample_manifest: dict):
        agents = generate_agents_list([sample_manifest])
        assert len(agents) == 1
        assert agents[0]["id"] == "email-classifier"


class TestGenerateJobsList:
    def test_filters_non_cron(self, sample_manifest: dict):
        no_sched = {"id": "manual", "name": "Manual"}
        jobs = generate_jobs_list([sample_manifest, no_sched])
        assert len(jobs) == 1
        assert jobs[0]["agentId"] == "email-classifier"


class TestGenerateOpenclawConfig:
    def test_merges_with_base(self, manifest_dir: Path):
        base = {
            "meta": {"version": "test"},
            "agents": {
                "defaults": {"model": {"primary": "test"}},
                "list": [{"id": "main", "default": True}],
            },
        }
        config = generate_openclaw_config(manifest_dir, base)
        assert config["meta"]["version"] == "test"
        agent_ids = [a["id"] for a in config["agents"]["list"]]
        assert "main" in agent_ids
        assert "email-classifier" in agent_ids


class TestGenerateAndDeploy:
    def test_dry_run_prints(self, manifest_dir: Path, tmp_path: Path, capsys):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        result = generate_and_deploy(manifest_dir, config_dir, dry_run=True)
        assert result == 0
        captured = capsys.readouterr()
        assert "email-classifier" in captured.out

    def test_writes_files(self, manifest_dir: Path, tmp_path: Path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        # Create a minimal base config
        (config_dir / "openclaw.json").write_text(json.dumps({"agents": {"list": []}}))

        result = generate_and_deploy(manifest_dir, config_dir)
        assert result == 0
        assert (config_dir / "openclaw.json").exists()
        assert (config_dir / "cron" / "jobs.json").exists()

        # Verify content
        config = json.loads((config_dir / "openclaw.json").read_text())
        agent_ids = [a["id"] for a in config["agents"]["list"]]
        assert "email-classifier" in agent_ids

    def test_missing_manifest_dir(self, tmp_path: Path):
        result = generate_and_deploy(tmp_path / "nonexistent", tmp_path / "config")
        assert result == 1
