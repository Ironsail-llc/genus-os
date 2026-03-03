"""Shared fixtures for template system tests."""

from __future__ import annotations

import pytest
import yaml


@pytest.fixture
def tmp_bundle(tmp_path):
    """Create a minimal template bundle in a temp directory."""
    bundle = tmp_path / "test-agent"
    bundle.mkdir()

    # setup.yaml
    setup = {
        "agent_id": "test-agent",
        "version": "1.0.0",
        "instruction_file_path": "brain/TEST_AGENT.md",
        "variables": {
            "model_primary": {
                "type": "string",
                "default": "openrouter/moonshotai/kimi-k2.5",
                "description": "Primary model",
            },
            "timezone": {
                "type": "string",
                "default": "America/New_York",
                "description": "Schedule timezone",
            },
            "cron_expr": {
                "type": "string",
                "default": "0 */2 * * *",
                "description": "Cron schedule",
            },
            "reports_to": {
                "type": "string",
                "default": "main",
                "description": "Reports to",
            },
        },
    }
    (bundle / "setup.yaml").write_text(yaml.dump(setup, default_flow_style=False))

    # manifest.template.yaml
    manifest_template = """\
id: test-agent
name: Test Agent
description: A test agent
version: "{{ version }}"
department: custom

reports_to: {{ reports_to }}
escalates_to: {{ reports_to }}

model:
  primary: {{ model_primary }}

schedule:
  cron: "{{ cron_expr }}"
  timezone: {{ timezone }}
  timeout_seconds: 300
  max_iterations: 10
  session_target: isolated

delivery:
  mode: none

tools_allowed:
  - exec
  - read_file
  - write_file

instruction_file: brain/TEST_AGENT.md
bootstrap_files: []
"""
    (bundle / "manifest.template.yaml").write_text(manifest_template)

    # instructions.template.md
    (bundle / "instructions.template.md").write_text(
        "# Test Agent\n\nYou are a test agent running in {{ timezone }}.\n"
    )

    # SKILL.md
    skill_md = """\
---
name: Test Agent
version: "1.0.0"
description: A test agent for validation
format: robothor-native/v1
---

# Test Agent
"""
    (bundle / "SKILL.md").write_text(skill_md)

    return bundle


@pytest.fixture
def tmp_defaults(tmp_path):
    """Create a _defaults.yaml file."""
    defaults = {
        "model_primary": "openrouter/moonshotai/kimi-k2.5",
        "timezone": "America/New_York",
        "reports_to": "main",
        "escalates_to": "main",
    }
    path = tmp_path / "_defaults.yaml"
    path.write_text(yaml.dump(defaults, default_flow_style=False))
    return path


@pytest.fixture
def tmp_instance_dir(tmp_path):
    """Create a temp .robothor/ directory."""
    instance_dir = tmp_path / ".robothor"
    instance_dir.mkdir()
    return instance_dir


@pytest.fixture
def tmp_repo(tmp_path, tmp_bundle):
    """Create a minimal repo structure for testing."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # docs/agents/
    agents_dir = repo / "docs" / "agents"
    agents_dir.mkdir(parents=True)

    # brain/
    brain = repo / "brain"
    brain.mkdir()
    (brain / "TEST_AGENT.md").write_text("# Test Agent\n")
    (brain / "AGENTS.md").write_text("# Agents\n")
    (brain / "TOOLS.md").write_text("# Tools\n")

    # schema.yaml
    schema = {
        "required": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "version": {"type": "string"},
            "department": {
                "type": "string",
                "enum": [
                    "email",
                    "calendar",
                    "operations",
                    "security",
                    "communications",
                    "crm",
                    "briefings",
                    "core",
                    "custom",
                ],
            },
        }
    }
    (agents_dir / "schema.yaml").write_text(yaml.dump(schema, default_flow_style=False))

    # templates/agents/
    tmpl_agents = repo / "templates" / "agents"
    tmpl_agents.mkdir(parents=True)

    # _defaults.yaml
    defaults = {
        "model_primary": "openrouter/moonshotai/kimi-k2.5",
        "timezone": "America/New_York",
        "reports_to": "main",
    }
    (tmpl_agents / "_defaults.yaml").write_text(yaml.dump(defaults, default_flow_style=False))

    return repo
