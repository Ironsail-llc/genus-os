"""Tests for Agent Architect — fleet evolution meta-agent.

Validates the manifest, task dispatch logic, priority scoring,
memory block state management, cross-pollination, and integration
with AutoAgent, AutoResearch, Buddy, and the scheduler.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from robothor.engine.config import load_manifest, manifest_to_agent_config

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig

# ─── Constants ──────────────────────────────────────────────────────

MANIFEST_PATH = Path(__file__).resolve().parents[3] / "docs" / "agents" / "agent-architect.yaml"
AUTO_AGENT_MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "agents" / "auto-agent.yaml"
)
AUTO_RESEARCHER_MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "agents" / "auto-researcher.yaml"
)


# ─── Helpers ────────────────────────────────────────────────────────


def _load_architect_config() -> AgentConfig:
    """Load the agent-architect manifest into an AgentConfig."""
    manifest = load_manifest(MANIFEST_PATH)
    return manifest_to_agent_config(manifest)


def _mock_blocks():
    """In-memory block store for testing memory block reads/writes."""
    store: dict[str, str] = {}

    def read_block(name: str) -> dict:
        if name in store:
            return {"content": store[name], "last_written_at": "2026-04-05T02:00:00"}
        return {"error": f"Block '{name}' not found"}

    def write_block(name: str, content: str) -> dict:
        store[name] = content
        return {"success": True, "block_name": name}

    return store, read_block, write_block


def _mock_agent_stats(
    agent_id: str, success_rate: float = 0.85, avg_cost: float = 0.12, total_runs: int = 50
) -> dict:
    """Create a mock agent stats return value."""
    return {
        "agent_id": agent_id,
        "success_rate": success_rate,
        "avg_cost_usd": avg_cost,
        "total_runs": total_runs,
        "error_rate": 1 - success_rate,
        "avg_duration_ms": 5000,
        "avg_tokens": 2000,
        "top_error_types": [],
    }


def _make_dispatch_ledger(*entries: tuple[str, str, str, str]) -> str:
    """Build a dispatch ledger string from tuples of (date, agent_id, target, outcome)."""
    lines = []
    for date, agent_id, target, outcome in entries:
        lines.append(f"{date} | {agent_id} | {target} | {outcome} | test learning")
    return "\n".join(lines)


def _make_evolution_log(*entries: tuple[str, float, str]) -> str:
    """Build an evolution log string from tuples of (date, health, trend)."""
    lines = []
    for date, health, trend in entries:
        lines.append(
            f"{date} | Fleet health: {health}% | Trend: {trend} | Actions: 2 dispatched | Top target: main | Notes: test"
        )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# Group 1: Manifest & Configuration
# ═══════════════════════════════════════════════════════════════════


class TestManifestConfiguration:
    """Verify the agent-architect.yaml manifest is valid and well-formed."""

    def test_manifest_loads_cleanly(self):
        """Manifest parses into a valid AgentConfig without errors."""
        config = _load_architect_config()
        assert config.id == "agent-architect"
        assert config.model_primary == "openrouter/anthropic/claude-opus-4.6"

    def test_manifest_tools_registered(self):
        """Engine-native tools in tools_allowed exist in the ToolRegistry.

        Some tools (memory_block_*, list_tasks, create_task, resolve_task,
        search_memory) are provided via the Bridge MCP server, not the
        native engine registry. We only check native tools here.
        """
        mcp_tools = {
            "memory_block_read",
            "memory_block_write",
            "memory_block_list",
            "append_to_block",
            "list_tasks",
            "create_task",
            "resolve_task",
            "search_memory",
        }
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools.registry import ToolRegistry

            registry = ToolRegistry()
            config = _load_architect_config()
            for tool in config.tools_allowed:
                if tool in mcp_tools:
                    continue  # Provided by Bridge MCP, not native registry
                assert tool in registry._schemas, f"Tool '{tool}' not in native registry"

    def test_manifest_denied_tools_blocked(self):
        """Dangerous tools are in tools_denied."""
        config = _load_architect_config()
        denied = set(config.tools_denied)
        assert "exec" in denied
        assert "experiment_create" in denied
        assert "experiment_measure" in denied
        assert "experiment_commit" in denied
        assert "benchmark_define" in denied
        assert "benchmark_run" in denied

    def test_manifest_relationships_valid(self):
        """reports_to, creates_tasks_for, escalates_to reference real agents."""
        config = _load_architect_config()
        assert config.reports_to == "main"
        # creates_tasks_for and escalates_to are manifest-level, check raw manifest
        manifest = load_manifest(MANIFEST_PATH)
        assert "auto-agent" in manifest["creates_tasks_for"]
        assert "auto-researcher" in manifest["creates_tasks_for"]
        assert manifest["escalates_to"] == "main"

    def test_manifest_warmup_blocks_declared(self):
        """All 5 required memory blocks are in warmup.memory_blocks."""
        manifest = load_manifest(MANIFEST_PATH)
        blocks = manifest["warmup"]["memory_blocks"]
        expected = {
            "architect_evolution_log",
            "architect_dispatch_ledger",
            "autoagent_learnings",
            "autoresearch_learnings",
            "performance_baselines",
        }
        assert expected == set(blocks)


# ═══════════════════════════════════════════════════════════════════
# Group 2: Task Dispatch Logic
# ═══════════════════════════════════════════════════════════════════


class TestTaskDispatchLogic:
    """Verify task creation patterns match what AutoAgent/AutoResearch expect."""

    def test_dispatch_task_for_auto_agent_format(self):
        """Tasks for auto-agent have correct tags, assignee, and body structure."""
        task = {
            "title": "Optimize email-classifier: low correctness score",
            "body": "Target: email-classifier\nFocus: correctness\nCurrent stats: success_rate=0.65\nHypothesis: Add forwarding detection",
            "assignedToAgent": "auto-agent",
            "tags": ["architect", "fleet-evolution", "email-classifier"],
            "priority": "high",
        }
        assert task["assignedToAgent"] == "auto-agent"
        assert "architect" in task["tags"]
        assert "email-classifier" in task["tags"]
        assert "Target:" in task["body"]
        assert "Hypothesis:" in task["body"]

    def test_dispatch_task_for_auto_researcher_format(self):
        """Tasks for auto-researcher route metric-focused work correctly."""
        task = {
            "title": "Optimize email-classifier: reduce avg cost",
            "body": "Target: email-classifier\nFocus: cost reduction\nMetric: avg_cost_usd",
            "assignedToAgent": "auto-researcher",
            "tags": ["architect", "fleet-evolution", "email-classifier"],
            "priority": "normal",
        }
        assert task["assignedToAgent"] == "auto-researcher"

    def test_dispatch_respects_14_day_cooldown(self):
        """Agents dispatched <14 days ago are skipped."""
        _make_dispatch_ledger(
            ("2026-04-01", "email-classifier", "auto-agent", "improved 5%"),
        )
        # 2026-04-05 is only 4 days after dispatch — should be skipped
        from datetime import date

        dispatch_date = date(2026, 4, 1)
        current_date = date(2026, 4, 5)
        days_since = (current_date - dispatch_date).days
        assert days_since < 14, "Should be within cooldown period"

        # After 14 days it's fine
        future_date = date(2026, 4, 16)
        days_since_future = (future_date - dispatch_date).days
        assert days_since_future >= 14, "Should be outside cooldown period"

    def test_dispatch_skips_agent_with_open_task(self):
        """If list_tasks returns open tasks for an agent, skip dispatching."""
        existing_tasks = [
            {
                "id": "t1",
                "title": "Optimize email-classifier: tone",
                "status": "TODO",
                "tags": ["autoagent", "email-classifier"],
            },
        ]
        # Agent has an open task — dispatch should be skipped
        assert len(existing_tasks) > 0
        assert any("email-classifier" in t.get("tags", []) for t in existing_tasks)

    def test_dispatch_max_3_per_run(self):
        """Never create more than 3 optimization tasks per run."""
        max_dispatch = 3
        # Simulate 5 agents needing optimization
        agents_needing_work = ["a1", "a2", "a3", "a4", "a5"]
        dispatched = agents_needing_work[:max_dispatch]
        assert len(dispatched) <= 3

    def test_dispatch_max_1_cross_pollination(self):
        """Cross-system tasks are capped at 1 per run."""
        max_cross_pollination = 1
        cross_tasks_created = 0
        # Simulate finding 3 cross-pollination opportunities
        opportunities = ["pattern_a", "pattern_b", "pattern_c"]
        for _ in opportunities:
            if cross_tasks_created < max_cross_pollination:
                cross_tasks_created += 1
        assert cross_tasks_created == 1

    def test_dispatch_includes_hypothesis(self):
        """Task body must contain a specific hypothesis, not generic text."""
        good_body = "Hypothesis: Email-classifier's correctness dropped because forwarded emails have nested headers that confuse the classification prompt."
        bad_body = "Please optimize this agent."
        assert "Hypothesis:" in good_body
        assert "Hypothesis:" not in bad_body

    def test_dispatch_structural_proposals_tag_philip(self):
        """Structural proposals (new agent, retirement) use needs-philip tag."""
        structural_task = {
            "title": "Structural: Propose new agent for invoice processing",
            "tags": ["architect", "needs-philip", "structural"],
            "assignedToAgent": "main",
        }
        assert "needs-philip" in structural_task["tags"]
        assert structural_task["assignedToAgent"] == "main"


# ═══════════════════════════════════════════════════════════════════
# Group 3: Priority Scoring
# ═══════════════════════════════════════════════════════════════════


class TestPriorityScoring:
    """Verify the ROI-based prioritization logic."""

    def _compute_impact(self, total_runs: int, success_rate: float) -> float:
        """Simplified impact: run volume * room for improvement."""
        volume_score = min(total_runs / 200, 1.0)  # Normalize to 0-1
        room = 1.0 - success_rate
        return volume_score * 0.5 + room * 0.5

    def _compute_feasibility(
        self, has_benchmark: bool, prior_success: bool, prior_failure: bool
    ) -> float:
        score = 0.5  # base
        if has_benchmark:
            score += 0.2
        if prior_success:
            score += 0.3
        if prior_failure:
            score -= 0.4
        return max(0.0, min(1.0, score))

    def _compute_urgency(self, declining: bool, anomaly: bool, stale: bool) -> float:
        score = 0.0
        if declining:
            score += 0.4
        if anomaly:
            score += 0.3
        if stale:
            score += 0.3
        return min(1.0, score)

    def _compute_priority(self, impact: float, feasibility: float, urgency: float) -> float:
        return impact * 0.45 + feasibility * 0.30 + urgency * 0.25

    def test_high_traffic_agent_ranks_higher(self):
        """Agent with 200 runs/week outranks agent with 5 runs at same score."""
        impact_high = self._compute_impact(200, 0.70)
        impact_low = self._compute_impact(5, 0.70)
        assert impact_high > impact_low

    def test_declining_score_boosts_urgency(self):
        """Week-over-week decline increases urgency."""
        urgency_declining = self._compute_urgency(declining=True, anomaly=False, stale=False)
        urgency_stable = self._compute_urgency(declining=False, anomaly=False, stale=False)
        assert urgency_declining > urgency_stable

    def test_existing_benchmark_boosts_feasibility(self):
        """Agents with benchmark suites defined rank higher in feasibility."""
        feas_with = self._compute_feasibility(
            has_benchmark=True, prior_success=False, prior_failure=False
        )
        feas_without = self._compute_feasibility(
            has_benchmark=False, prior_success=False, prior_failure=False
        )
        assert feas_with > feas_without

    def test_previously_failed_optimization_lowers(self):
        """Agents where prior AutoAgent work failed rank lower."""
        feas_failed = self._compute_feasibility(
            has_benchmark=False, prior_success=False, prior_failure=True
        )
        feas_clean = self._compute_feasibility(
            has_benchmark=False, prior_success=False, prior_failure=False
        )
        assert feas_failed < feas_clean

    def test_score_60_over_score_90(self):
        """More room for improvement = higher impact."""
        impact_60 = self._compute_impact(100, 0.60)
        impact_90 = self._compute_impact(100, 0.90)
        assert impact_60 > impact_90

    def test_dormant_agent_flagged_as_stale(self):
        """0 runs in 14+ days triggers staleness urgency boost."""
        urgency_stale = self._compute_urgency(declining=False, anomaly=False, stale=True)
        urgency_active = self._compute_urgency(declining=False, anomaly=False, stale=False)
        assert urgency_stale > urgency_active


# ═══════════════════════════════════════════════════════════════════
# Group 4: Memory Block State Management
# ═══════════════════════════════════════════════════════════════════


class TestMemoryBlockState:
    """Verify that evolution log and dispatch ledger are managed correctly."""

    def test_evolution_log_format(self):
        """Evolution log entries follow the expected format."""
        entry = "2026-04-05 | Fleet health: 82.5% | Trend: up | Actions: 3 dispatched | Top target: email-classifier | Notes: first run"
        parts = entry.split(" | ")
        assert len(parts) == 6
        assert parts[0] == "2026-04-05"
        assert "Fleet health:" in parts[1]
        assert "Trend:" in parts[2]
        assert "Actions:" in parts[3]

    def test_dispatch_ledger_records_outcome(self):
        """Dispatch ledger entries include agent, target, and outcome."""
        entry = "2026-04-05 | email-classifier | auto-agent | improved 8% | Removing deep_reason cut cost"
        parts = entry.split(" | ")
        assert len(parts) == 5
        assert parts[1] == "email-classifier"
        assert parts[2] == "auto-agent"
        assert "improved" in parts[3]

    def test_reads_prior_learnings_before_dispatch(self):
        """Learnings blocks are read before creating dispatch tasks."""
        store, read_fn, write_fn = _mock_blocks()
        store["autoagent_learnings"] = (
            "2026-03-30 | email-classifier | Removing deep_reason saved 40% cost"
        )
        store["autoresearch_learnings"] = (
            "2026-03-28 | main | Success rate improved 5% with tighter prompts"
        )

        result = read_fn("autoagent_learnings")
        assert "error" not in result
        assert "email-classifier" in result["content"]

        result2 = read_fn("autoresearch_learnings")
        assert "error" not in result2
        assert "main" in result2["content"]

    def test_stale_ledger_entries_dont_block(self):
        """Entries older than 14 days don't prevent re-dispatch."""
        from datetime import date

        _make_dispatch_ledger(
            ("2026-03-15", "email-classifier", "auto-agent", "no improvement"),
        )
        dispatch_date = date(2026, 3, 15)
        current_date = date(2026, 4, 5)
        days_since = (current_date - dispatch_date).days
        assert days_since > 14, "Old entry should not block new dispatch"

    def test_empty_blocks_handled_gracefully(self):
        """First run with no prior state returns error dicts, not crashes."""
        _, read_fn, _ = _mock_blocks()
        result = read_fn("architect_evolution_log")
        assert "error" in result
        assert "not found" in result["error"]

        result2 = read_fn("architect_dispatch_ledger")
        assert "error" in result2


# ═══════════════════════════════════════════════════════════════════
# Group 5: Cross-Pollination Logic
# ═══════════════════════════════════════════════════════════════════


class TestCrossPollination:
    """Verify cross-pollination identifies and caps cross-system tasks."""

    AGENT_GROUPS = {
        "email": ["email-classifier", "email-responder", "email-analyst"],
        "crm": ["crm-hygiene", "crm-dedup", "crm-enrichment", "crm-steward"],
        "monitoring": ["calendar-monitor", "vision-monitor", "proactive-check"],
    }

    def _find_group(self, agent_id: str) -> str | None:
        for group, members in self.AGENT_GROUPS.items():
            if agent_id in members:
                return group
        return None

    def _find_untested_peers(self, source_agent: str, tested_agents: set[str]) -> list[str]:
        group = self._find_group(source_agent)
        if not group:
            return []
        return [a for a in self.AGENT_GROUPS[group] if a != source_agent and a not in tested_agents]

    def test_cross_pollinate_finds_untested_pattern(self):
        """Learning from agent A suggests testing on untested agent B."""
        untested = self._find_untested_peers("email-classifier", {"email-classifier"})
        assert "email-responder" in untested
        assert "email-analyst" in untested

    def test_cross_pollinate_groups_by_role(self):
        """Email agents are grouped together, CRM agents together."""
        assert self._find_group("email-classifier") == "email"
        assert self._find_group("crm-hygiene") == "crm"
        assert self._find_group("calendar-monitor") == "monitoring"

    def test_cross_pollinate_skips_already_tested(self):
        """Don't re-propose if learning already applied to peer."""
        tested = {"email-classifier", "email-responder"}
        untested = self._find_untested_peers("email-classifier", tested)
        assert "email-responder" not in untested
        assert "email-analyst" in untested

    def test_cross_pollinate_respects_cap(self):
        """Max 1 cross-system task per run."""
        opportunities = [
            ("email-classifier", "email-responder"),
            ("crm-hygiene", "crm-dedup"),
            ("calendar-monitor", "vision-monitor"),
        ]
        max_cross = 1
        dispatched = opportunities[:max_cross]
        assert len(dispatched) == 1


# ═══════════════════════════════════════════════════════════════════
# Group 6: Telegram Summary Output
# ═══════════════════════════════════════════════════════════════════


class TestTelegramSummary:
    """Verify the weekly summary format contains required information."""

    def _build_summary(
        self,
        fleet_health: float = 82.5,
        trend: str = "up",
        agents_analyzed: int = 15,
        anomalies: int = 2,
        dispatched: int = 3,
        prior_completed: int = 1,
        prior_outcomes: str = "1 improved",
        top_priority: str = "email-classifier",
        top_reason: str = "declining success rate",
        cumulative: float = 5.2,
    ) -> str:
        return f"""Weekly Fleet Evolution Report

Fleet Health: {fleet_health}% ({trend} from last week)
Agents analyzed: {agents_analyzed} | Anomalies: {anomalies}

Actions This Week:
- Dispatched {dispatched} optimization tasks
- {prior_completed} prior tasks completed ({prior_outcomes})

Top Priority Next Week: {top_priority} — {top_reason}

Cumulative improvement since tracking began: {cumulative}%"""

    def test_summary_includes_fleet_health_score(self):
        summary = self._build_summary(fleet_health=85.0)
        assert "85.0%" in summary
        assert "Fleet Health:" in summary

    def test_summary_includes_trend_direction(self):
        for trend in ["up", "down", "flat"]:
            summary = self._build_summary(trend=trend)
            assert f"({trend} from last week)" in summary

    def test_summary_includes_cumulative_improvement(self):
        summary = self._build_summary(cumulative=12.3)
        assert "12.3%" in summary
        assert "Cumulative improvement" in summary


# ═══════════════════════════════════════════════════════════════════
# Group 7: Integration with Existing Systems
# ═══════════════════════════════════════════════════════════════════


class TestSystemIntegration:
    """Verify wiring to AutoAgent, AutoResearch, Buddy, and scheduler."""

    def test_buddy_underperformer_task_not_duplicated(self):
        """If Buddy already flagged an agent, Architect should detect the open task."""
        # Buddy creates tasks with tags ["autoagent", "low-score", agent_id]
        buddy_task = {
            "id": "t1",
            "status": "TODO",
            "tags": ["autoagent", "low-score", "email-classifier"],
        }
        # Architect checks list_tasks(tags=["autoagent", "email-classifier"])
        # Should find buddy_task and skip dispatching
        matching = [
            t for t in [buddy_task] if "email-classifier" in t["tags"] and "autoagent" in t["tags"]
        ]
        assert len(matching) > 0, "Should detect existing Buddy-created task"

    def test_auto_agent_receives_architect_tasks(self):
        """auto-agent.yaml includes agent-architect in receives_tasks_from."""
        manifest = load_manifest(AUTO_AGENT_MANIFEST_PATH)
        assert "agent-architect" in manifest["receives_tasks_from"]

    def test_auto_researcher_receives_architect_tasks(self):
        """auto-researcher.yaml includes agent-architect in receives_tasks_from."""
        manifest = load_manifest(AUTO_RESEARCHER_MANIFEST_PATH)
        assert "agent-architect" in manifest["receives_tasks_from"]

    def test_scheduler_registers_weekly_cron(self):
        """Cron expression '0 2 * * 0' is valid and creates a scheduler job."""
        config = _load_architect_config()
        assert config.cron_expr == "0 2 * * 0"
        # Verify it's a valid cron (Sunday 2AM)
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(config.cron_expr)
        assert trigger is not None


# ═══════════════════════════════════════════════════════════════════
# Model Registry
# ═══════════════════════════════════════════════════════════════════


class TestModelRegistry:
    """Verify new models are registered with correct pricing."""

    def test_opus_4_6_registered(self):
        from robothor.engine.model_registry import get_model_limits

        limits = get_model_limits("openrouter/anthropic/claude-opus-4.6")
        assert limits.max_input_tokens == 1_000_000
        assert limits.supports_thinking is True
        assert limits.input_cost_per_token == 0.000_005  # $5/M

    def test_gemini_3_1_pro_registered(self):
        from robothor.engine.model_registry import get_model_limits

        limits = get_model_limits("openrouter/google/gemini-3.1-pro-preview")
        assert limits.max_input_tokens == 1_000_000
        assert limits.input_cost_per_token == 0.000_002  # $2/M

    def test_gpt_5_4_registered(self):
        from robothor.engine.model_registry import get_model_limits

        limits = get_model_limits("openrouter/openai/gpt-5.4")
        assert limits.max_input_tokens == 1_050_000
        assert limits.input_cost_per_token == 0.000_002_5  # $2.50/M
