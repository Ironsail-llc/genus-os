"""Tests for the seed_agent_goals one-shot script."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "seed_agent_goals.py"
_spec = importlib.util.spec_from_file_location("seed_agent_goals", str(_SCRIPT))
assert _spec is not None and _spec.loader is not None
seed_mod = importlib.util.module_from_spec(_spec)
sys.modules["seed_agent_goals"] = seed_mod
_spec.loader.exec_module(seed_mod)


SAMPLE_MANIFEST = """\
id: test-agent
goals:
  quality:
    - {id: passes-its-job, metric: benchmark_pass_rate, target: ">=0.85", weight: 5.0}
"""

EMPTY_GOALS_MANIFEST = """\
id: empty-agent
"""


def _setup_agents_dir(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "test-agent.yaml").write_text(SAMPLE_MANIFEST)
    (agents / "empty-agent.yaml").write_text(EMPTY_GOALS_MANIFEST)
    # Ensure utility files are skipped.
    (agents / "_defaults.yaml").write_text("policy: {}\n")
    (agents / "schema.yaml").write_text("# schema\n")
    return agents


def test_skips_utility_files(tmp_path):
    agents = _setup_agents_dir(tmp_path)
    manifests = seed_mod._load_manifests(agents)
    ids = {a for a, _ in manifests}
    assert ids == {"test-agent", "empty-agent"}


@patch.object(seed_mod.dal, "get_or_create_agent_goal")
def test_seed_calls_dal_for_each_agent(mock_get_or_create, tmp_path, capsys):
    agents = _setup_agents_dir(tmp_path)
    mock_get_or_create.return_value = {
        "id": "task-x",
        "session_goal_meta": {"objective": "(seeded from manifest …)"},
    }
    rc = seed_mod.seed(tenant_id="default", agents_dir=agents)
    assert rc == 0
    assert mock_get_or_create.call_count == 2
    out = capsys.readouterr().out
    assert "test-agent" in out
    assert "empty-agent" in out


@patch.object(seed_mod.dal, "get_active_session_goal")
@patch.object(seed_mod.dal, "get_or_create_agent_goal")
def test_dry_run_does_not_write(mock_get_or_create, mock_get_active, tmp_path, capsys):
    agents = _setup_agents_dir(tmp_path)
    mock_get_active.return_value = None  # all agents need seeding
    rc = seed_mod.seed(tenant_id="default", agents_dir=agents, dry_run=True)
    assert rc == 0
    mock_get_or_create.assert_not_called()
    out = capsys.readouterr().out
    assert "[dry-run create]" in out


@patch.object(seed_mod.dal, "get_or_create_agent_goal")
def test_seed_returns_nonzero_on_failure(mock_get_or_create, tmp_path):
    agents = _setup_agents_dir(tmp_path)
    mock_get_or_create.return_value = None  # simulate failure
    rc = seed_mod.seed(tenant_id="default", agents_dir=agents)
    assert rc != 0
