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
def test_agent_warmup_records_agent_goal_section(mock_get, tmp_path):
    # v2: every agent has its own goal task, looked up by agent_id.
    def side_effect(*, tenant_id, agent_id=""):
        if agent_id == "main":
            return _row(tags=["session_goal", "agent:main", "thread"])
        return None

    mock_get.side_effect = side_effect

    cfg = _FakeConfig("main")
    preamble, timings = warmup_mod.build_warmth_preamble(cfg, tmp_path, tenant_id="default")

    assert "ACTIVE AGENT GOAL" in preamble
    assert "agent_goal" in timings, f"section timings: {timings}"


@patch("robothor.engine.session_goal.dal.get_active_session_goal")
def test_worker_with_no_goal_renders_no_section(mock_get, tmp_path):
    # No goal task for this worker → no agent_goal block in preamble.
    mock_get.return_value = None
    cfg = _FakeConfig("email-classifier")
    preamble, _timings = warmup_mod.build_warmth_preamble(cfg, tmp_path, tenant_id="default")
    assert "ACTIVE AGENT GOAL" not in preamble


@patch("robothor.engine.session_goal.dal.get_active_session_goal")
def test_each_agent_sees_only_its_own_goal(mock_get, tmp_path):
    # delphi has its own goal; email-classifier has its own; they don't bleed.
    def side_effect(*, tenant_id, agent_id=""):
        if agent_id == "delphi":
            return _row(tags=["session_goal", "agent:delphi", "thread"], objective="Delphi work")
        if agent_id == "email-classifier":
            return _row(
                tags=["session_goal", "agent:email-classifier", "thread"],
                objective="Email triage",
            )
        return None

    mock_get.side_effect = side_effect

    delphi_preamble, _ = warmup_mod.build_warmth_preamble(
        _FakeConfig("delphi"), tmp_path, tenant_id="default"
    )
    assert "Delphi work" in delphi_preamble
    assert "Email triage" not in delphi_preamble

    email_preamble, _ = warmup_mod.build_warmth_preamble(
        _FakeConfig("email-classifier"), tmp_path, tenant_id="default"
    )
    assert "Email triage" in email_preamble
    assert "Delphi work" not in email_preamble


@patch("robothor.engine.session_goal.dal.get_active_session_goal")
def test_no_active_goal_no_block(mock_get, tmp_path):
    mock_get.return_value = None
    preamble, _ = warmup_mod.build_warmth_preamble(
        _FakeConfig("main"), tmp_path, tenant_id="default"
    )
    assert "ACTIVE AGENT GOAL" not in preamble


@patch("robothor.engine.session_goal.dal.get_active_session_goal")
def test_session_goal_section_swallows_dal_exceptions(mock_get, tmp_path):
    # Defensive: a DAL crash must not break warmup.
    mock_get.side_effect = RuntimeError("db down")
    preamble, timings = warmup_mod.build_warmth_preamble(
        _FakeConfig("main"), tmp_path, tenant_id="default"
    )
    # _run_section catches; section appears in timings but not in preamble.
    assert "ACTIVE AGENT GOAL" not in preamble
    assert "agent_goal" in timings
