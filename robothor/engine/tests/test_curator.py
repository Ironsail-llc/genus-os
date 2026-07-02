"""Tests for Rip 5 curator: candidate selection + cadence gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from robothor.engine.curator import (
    CURATOR_DEFAULT_INTERVAL_DAYS,
    CURATOR_REVIEW_PROMPT,
    CuratorResult,
    list_curator_candidates,
    should_run_curator,
)
from robothor.engine.skills import SkillDefinition


def _skill(name: str) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description="x",
        parameters=[],
        trigger_phrases=[],
        tools_required=[],
        composable=False,
        tags=[],
        output_format="text",
        content="body",
        path=Path("/tmp/x"),
    )


class TestCuratorPromptContract:
    def test_mentions_do_not_capture(self) -> None:
        """The Hermes guardrail is the load-bearing piece — make
        sure consolidation prompt also forbids capturing
        environment failures."""
        assert "Do NOT capture" in CURATOR_REVIEW_PROMPT
        assert "environment-dependent" in CURATOR_REVIEW_PROMPT.lower()

    def test_mentions_is_agent_created_filter(self) -> None:
        assert "is_agent_created" in CURATOR_REVIEW_PROMPT

    def test_pinned_is_protected_from_archive(self) -> None:
        assert "Pinned" in CURATOR_REVIEW_PROMPT
        assert "NEVER archive" in CURATOR_REVIEW_PROMPT


class TestListCuratorCandidates:
    def test_only_agent_created_are_candidates(self) -> None:
        skills = {
            "a": _skill("a"),
            "b": _skill("b"),
            "c": _skill("c"),
        }
        metas = {
            "a": {"is_agent_created": True},
            "b": {"is_agent_created": False},
            "c": {"is_agent_created": True, "pinned": True},
        }
        candidates, pinned, human = list_curator_candidates(
            skills, meta_loader=lambda n: metas.get(n)
        )
        assert [s.name for s in candidates] == ["a"]
        assert pinned == ["c"]
        assert human == ["b"]

    def test_missing_meta_treated_as_human_authored(self) -> None:
        skills = {"a": _skill("a")}
        candidates, pinned, human = list_curator_candidates(skills, meta_loader=lambda n: None)
        assert candidates == []
        assert human == ["a"]


class TestShouldRunCurator:
    def test_no_prior_pass_runs(self) -> None:
        assert should_run_curator(None) is True

    def test_recent_pass_skips(self) -> None:
        now = datetime.now(UTC)
        recent = now - timedelta(days=1)
        assert should_run_curator(recent, now=now) is False

    def test_stale_pass_runs(self) -> None:
        now = datetime.now(UTC)
        stale = now - timedelta(days=8)
        assert should_run_curator(stale, now=now) is True

    def test_at_threshold_runs(self) -> None:
        now = datetime.now(UTC)
        at_threshold = now - timedelta(days=CURATOR_DEFAULT_INTERVAL_DAYS)
        assert should_run_curator(at_threshold, now=now) is True

    def test_interval_overridable(self) -> None:
        now = datetime.now(UTC)
        last = now - timedelta(days=2)
        assert should_run_curator(last, now=now, interval_days=1) is True
        assert should_run_curator(last, now=now, interval_days=3) is False


class TestCuratorResult:
    def test_total_actions(self) -> None:
        result = CuratorResult(
            tenant_id="t",
            dry_run=True,
            candidates_inspected=10,
            proposed_archive=["a", "b"],
            proposed_merge=[("c", "d")],
            proposed_demote=["e", "f", "g"],
        )
        assert result.total_actions() == 6


# ─── spawn_curator + persistence (Phase 3 wiring) ───────────────────


class TestCuratorOrchestration:
    def test_spawn_curator_skips_when_no_candidates(self):
        import asyncio
        from unittest.mock import patch

        from robothor.engine.curator import spawn_curator

        with patch("robothor.engine.curator.list_curator_candidates", return_value=([], [], [])):
            result = asyncio.run(spawn_curator(scheduler=object()))
        assert result == {"status": "skipped", "reason": "no_candidates"}

    def test_last_pass_round_trip(self):
        from unittest.mock import patch

        from robothor.engine.curator import load_curator_last_pass, store_curator_last_pass

        store: dict[str, str] = {}

        def _read(name, tenant_id="t"):
            return {"content": store.get(name, "")} if name in store else {"error": "missing"}

        def _write(name, content, tenant_id="t"):
            store[name] = content
            return {"success": True}

        when = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
        with (
            patch("robothor.memory.blocks.read_block", side_effect=_read),
            patch("robothor.memory.blocks.write_block", side_effect=_write),
        ):
            assert load_curator_last_pass() is None  # nothing stored
            store_curator_last_pass(when)
            got = load_curator_last_pass()
        assert got == when


class TestCuratorDryRun:
    def test_whitelist_excludes_archive_in_dry_run(self):
        from robothor.engine.curator import _curator_tool_whitelist

        assert "skill_archive" not in _curator_tool_whitelist(dry_run=True)
        assert "skill_archive" in _curator_tool_whitelist(dry_run=False)

    def test_curator_dry_run_default_true(self, monkeypatch):
        from robothor.engine.curator import curator_dry_run

        monkeypatch.delenv("ROBOTHOR_CURATOR_APPLY", raising=False)
        assert curator_dry_run() is True
        monkeypatch.setenv("ROBOTHOR_CURATOR_APPLY", "1")
        assert curator_dry_run() is False


# ─── PR-3b: accretion gate live caller ───────────────────────────────


class TestEvaluateAccretionGate:
    """evaluate_accretion_gate() computes the two-key verdict for the
    curator's own apply pass. Scoping decision (see curator.py module
    docstring): it gates on the curator agent itself, not per-skill —
    there is no per-skill benchmark/judge attribution yet."""

    def test_promotes_when_no_regression_and_judge_at_baseline(self):
        from unittest.mock import patch

        from robothor.engine.curator import evaluate_accretion_gate

        with (
            patch(
                "robothor.engine.curator._curator_benchmark_regression",
                return_value=(False, "no benchmark suite for 'curator'"),
            ),
            patch(
                "robothor.engine.curator._curator_judge_scores",
                return_value=(4.0, 3.5),
            ),
        ):
            ok, reason = evaluate_accretion_gate()
        assert ok is True
        assert "promoted" in reason

    def test_blocks_on_benchmark_regression(self):
        from unittest.mock import patch

        from robothor.engine.curator import evaluate_accretion_gate

        with (
            patch(
                "robothor.engine.curator._curator_benchmark_regression",
                return_value=(True, "benchmark pass rate regressed 0.90 -> 0.60"),
            ),
            patch(
                "robothor.engine.curator._curator_judge_scores",
                return_value=(4.0, 3.5),
            ),
        ):
            ok, reason = evaluate_accretion_gate()
        assert ok is False
        assert "safety regression" in reason
        assert "regressed" in reason

    def test_blocks_below_baseline_judge_score(self):
        from unittest.mock import patch

        from robothor.engine.curator import evaluate_accretion_gate

        with (
            patch(
                "robothor.engine.curator._curator_benchmark_regression",
                return_value=(False, "no benchmark suite for 'curator'"),
            ),
            patch(
                "robothor.engine.curator._curator_judge_scores",
                return_value=(2.0, 4.0),
            ),
        ):
            ok, reason = evaluate_accretion_gate()
        assert ok is False
        assert "below baseline" in reason

    def test_fails_closed_on_missing_judge_history(self):
        """No judge rows yet for the curator agent — cannot confirm quality is
        at least the baseline, so the gate blocks rather than assuming pass."""
        from unittest.mock import patch

        from robothor.engine.curator import evaluate_accretion_gate

        with (
            patch(
                "robothor.engine.curator._curator_benchmark_regression",
                return_value=(False, "no benchmark suite for 'curator'"),
            ),
            patch(
                "robothor.engine.curator._curator_judge_scores",
                return_value=(None, None),
            ),
        ):
            ok, reason = evaluate_accretion_gate()
        assert ok is False
        assert "judge" in reason.lower()


class _FakeScheduler:
    async def _run_agent(self, agent_id: str) -> None:
        return None


class TestSpawnCuratorAccretionGateWiring:
    def test_dry_run_mode_never_consults_gate(self):
        import asyncio
        from unittest.mock import patch

        import robothor.engine.curator as curator_mod

        with (
            patch.object(curator_mod, "list_curator_candidates", return_value=(["a"], [], [])),
            patch.object(curator_mod, "accretion_enabled", return_value=True),
            patch.object(curator_mod, "evaluate_accretion_gate") as gate_mock,
            patch.object(curator_mod, "store_curator_pass_summary"),
        ):
            result = asyncio.run(curator_mod.spawn_curator(_FakeScheduler(), dry_run=True))
        gate_mock.assert_not_called()
        assert result is not None
        assert result["dry_run"] is True

    def test_accretion_disabled_leaves_apply_mode_unchanged(self):
        import asyncio
        from unittest.mock import patch

        import robothor.engine.curator as curator_mod

        with (
            patch.object(curator_mod, "list_curator_candidates", return_value=(["a"], [], [])),
            patch.object(curator_mod, "accretion_enabled", return_value=False),
            patch.object(curator_mod, "evaluate_accretion_gate") as gate_mock,
            patch.object(
                curator_mod,
                "_curator_tool_whitelist",
                wraps=curator_mod._curator_tool_whitelist,
            ) as whitelist_spy,
            patch.object(curator_mod, "store_curator_pass_summary"),
        ):
            result = asyncio.run(curator_mod.spawn_curator(_FakeScheduler(), dry_run=False))
        gate_mock.assert_not_called()
        whitelist_spy.assert_called_once_with(dry_run=False)
        assert result is not None
        assert result["dry_run"] is False

    def test_gate_blocks_downgrades_apply_to_dry_run(self):
        import asyncio
        from unittest.mock import patch

        import robothor.engine.curator as curator_mod

        with (
            patch.object(curator_mod, "list_curator_candidates", return_value=(["a"], [], [])),
            patch.object(curator_mod, "accretion_enabled", return_value=True),
            patch.object(
                curator_mod,
                "evaluate_accretion_gate",
                return_value=(False, "blocked: judge score 2.00 below baseline 4.00"),
            ),
            patch.object(
                curator_mod,
                "_curator_tool_whitelist",
                wraps=curator_mod._curator_tool_whitelist,
            ) as whitelist_spy,
            patch.object(curator_mod, "store_curator_pass_summary") as store_mock,
        ):
            result = asyncio.run(curator_mod.spawn_curator(_FakeScheduler(), dry_run=False))
        whitelist_spy.assert_called_once_with(dry_run=True)
        assert result is not None
        assert result["dry_run"] is True
        assert result["gate_verdict"] is False
        assert result["gate_reason"] is not None
        assert "below baseline" in result["gate_reason"]
        stored_payload = store_mock.call_args.args[0]
        assert stored_payload["gate_verdict"] is False
        assert stored_payload["mode"] == "dry_run"

    def test_gate_pass_keeps_apply_whitelist(self):
        import asyncio
        from unittest.mock import patch

        import robothor.engine.curator as curator_mod

        with (
            patch.object(curator_mod, "list_curator_candidates", return_value=(["a"], [], [])),
            patch.object(curator_mod, "accretion_enabled", return_value=True),
            patch.object(
                curator_mod,
                "evaluate_accretion_gate",
                return_value=(True, "promoted: no regression, judge 4.00 >= baseline 3.50"),
            ),
            patch.object(
                curator_mod,
                "_curator_tool_whitelist",
                wraps=curator_mod._curator_tool_whitelist,
            ) as whitelist_spy,
            patch.object(curator_mod, "store_curator_pass_summary"),
        ):
            result = asyncio.run(curator_mod.spawn_curator(_FakeScheduler(), dry_run=False))
        whitelist_spy.assert_called_once_with(dry_run=False)
        assert result is not None
        assert result["dry_run"] is False
        assert result["gate_verdict"] is True


class TestCuratorStatePersistence:
    def test_store_and_load_summary_round_trip(self):
        from unittest.mock import patch

        from robothor.engine.curator import (
            load_curator_last_pass,
            load_curator_last_summary,
            store_curator_last_pass,
            store_curator_pass_summary,
        )

        store: dict[str, str] = {}

        def _read(name, tenant_id="t"):
            return {"content": store.get(name, "")} if name in store else {"error": "missing"}

        def _write(name, content, tenant_id="t"):
            store[name] = content
            return {"success": True}

        when = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
        with (
            patch("robothor.memory.blocks.read_block", side_effect=_read),
            patch("robothor.memory.blocks.write_block", side_effect=_write),
        ):
            store_curator_last_pass(when)
            store_curator_pass_summary(
                {
                    "mode": "dry_run",
                    "gate_verdict": None,
                    "gate_reason": None,
                    "candidates_inspected": 3,
                    "skipped_pinned": ["p1"],
                    "skipped_human_authored": [],
                }
            )
            got_ts = load_curator_last_pass()
            got_summary = load_curator_last_summary()

        assert got_ts == when  # timestamp preserved alongside the new summary
        assert got_summary is not None
        assert got_summary["mode"] == "dry_run"
        assert got_summary["candidates_inspected"] == 3

    def test_summary_list_fields_are_capped(self):
        from unittest.mock import patch

        from robothor.engine.curator import load_curator_last_summary, store_curator_pass_summary

        store: dict[str, str] = {}

        def _read(name, tenant_id="t"):
            return {"content": store.get(name, "")} if name in store else {"error": "missing"}

        def _write(name, content, tenant_id="t"):
            store[name] = content
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=_read),
            patch("robothor.memory.blocks.write_block", side_effect=_write),
        ):
            store_curator_pass_summary({"skipped_pinned": [f"s{i}" for i in range(50)]})
            got = load_curator_last_summary()
        assert got is not None
        assert len(got["skipped_pinned"]) <= 10

    def test_legacy_bare_iso_content_still_parses(self):
        from unittest.mock import patch

        from robothor.engine.curator import load_curator_last_pass

        when = datetime(2026, 5, 1, tzinfo=UTC)
        store = {"curator_state": when.isoformat()}

        def _read(name, tenant_id="t"):
            return {"content": store.get(name, "")} if name in store else {"error": "missing"}

        with patch("robothor.memory.blocks.read_block", side_effect=_read):
            got = load_curator_last_pass()
        assert got == when
