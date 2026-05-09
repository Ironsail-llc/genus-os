"""Runner integration tests for session-goal warmup injection.

The session goal should appear as a ``warmup_section:session_goal`` step in
``agent_run_steps`` whenever it would render — and must NOT inject for
worker agents (delivery: none / non-owner / no agent-scoped goal).

These tests exercise the warmup pipeline directly rather than spinning up a
full LLM run; the runner integration point is just calling
``build_warmth_preamble`` and consuming its (preamble, timings) tuple.
"""

from __future__ import annotations

from unittest.mock import patch

from robothor.engine import warmup as warmup_mod


class _FakeConfig:
    def __init__(self, agent_id: str):
        self.id = agent_id
        self.warmup_memory_blocks: list[str] = []
        self.warmup_context_files: list[str] = []
        self.warmup_peer_agents: list[str] = []


def _row(*, tags: list[str], objective: str = "Ship session goal"):
    return {
        "id": "task-x",
        "objective": objective,
        "tags": tags,
        "status": "TODO",
        "session_goal_meta": {
            "success_criteria": ["c1"],
            "evidence": [],
            "completion_note": "",
        },
    }


@patch("robothor.engine.session_goal.dal.get_active_session_goal")
def test_owner_warmup_records_session_goal_section(mock_get, tmp_path):
    # Workspace goal exists; owner=main asks warmup → section recorded.
    def side_effect(*, tenant_id, agent_id=""):
        if agent_id:
            return None
        return _row(tags=["session_goal"])

    mock_get.side_effect = side_effect

    cfg = _FakeConfig("main")
    preamble, timings = warmup_mod.build_warmth_preamble(cfg, tmp_path, tenant_id="default")

    assert "ACTIVE SHORT-TERM GOAL" in preamble
    assert "session_goal" in timings, f"section timings: {timings}"


@patch("robothor.engine.session_goal.dal.get_active_session_goal")
def test_worker_warmup_does_not_inject_session_goal(mock_get, tmp_path):
    # Workspace goal exists; worker (email-classifier) → not injected.
    def side_effect(*, tenant_id, agent_id=""):
        if agent_id:
            return None
        return _row(tags=["session_goal"])

    mock_get.side_effect = side_effect

    cfg = _FakeConfig("email-classifier")
    preamble, timings = warmup_mod.build_warmth_preamble(cfg, tmp_path, tenant_id="default")

    assert "ACTIVE SHORT-TERM GOAL" not in preamble
    # The section is run (so timing is captured even when result empty), but
    # the rendered preamble must not include the goal block.


@patch("robothor.engine.session_goal.dal.get_active_session_goal")
def test_agent_scoped_goal_injects_only_for_owner(mock_get, tmp_path):
    # Agent-scoped goal for delphi exists.
    def side_effect(*, tenant_id, agent_id=""):
        if agent_id == "delphi":
            return _row(tags=["session_goal", "agent:delphi"], objective="Delphi work")
        return None

    mock_get.side_effect = side_effect

    own_preamble, _ = warmup_mod.build_warmth_preamble(
        _FakeConfig("delphi"), tmp_path, tenant_id="default"
    )
    assert "Delphi work" in own_preamble

    other_preamble, _ = warmup_mod.build_warmth_preamble(
        _FakeConfig("email-classifier"), tmp_path, tenant_id="default"
    )
    assert "Delphi work" not in other_preamble


@patch("robothor.engine.session_goal.dal.get_active_session_goal")
def test_no_active_goal_no_block(mock_get, tmp_path):
    mock_get.return_value = None
    preamble, _ = warmup_mod.build_warmth_preamble(
        _FakeConfig("main"), tmp_path, tenant_id="default"
    )
    assert "ACTIVE SHORT-TERM GOAL" not in preamble


@patch("robothor.engine.session_goal.dal.get_active_session_goal")
def test_session_goal_section_swallows_dal_exceptions(mock_get, tmp_path):
    # Defensive: a DAL crash must not break warmup.
    mock_get.side_effect = RuntimeError("db down")
    preamble, timings = warmup_mod.build_warmth_preamble(
        _FakeConfig("main"), tmp_path, tenant_id="default"
    )
    # _run_section catches; section appears in timings but not in preamble.
    assert "ACTIVE SHORT-TERM GOAL" not in preamble
    assert "session_goal" in timings
