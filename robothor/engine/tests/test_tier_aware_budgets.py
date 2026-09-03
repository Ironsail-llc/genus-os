"""Time budgets must be sized for the model that will actually answer.

2026-08-27. `ttft_hint_ms` has been in ModelLimits since the registry was
written, documented as "for interactive routing", with `ttft_hint_ms=9000` on
the local 27B and a 3000ms default. Nothing ever read it. Meanwhile every
watchdog budget came from the manifest alone, so when the OpenRouter weekly cap
sent the fleet to the local tier the whole ruleset stayed calibrated for cloud
latency and started killing healthy runs.

This is the second half of the fix. 1.1 stopped counting an in-flight call as
idle; a call in flight deliberately still does NOT count as output, so
early-stall kept firing on main's heartbeat. Scaling the budget by the model's
own tempo is what closes that, and it is why 1.1 and 1.2 ship together.

Two edges are load-bearing:

  * 0 MUST stay 0. `_defaults.yaml` sets `stall_timeout_seconds: 0` fleet-wide,
    which disables the watchdog. Scaling a disabled budget into a live timeout
    would kill every agent on the fleet.
  * The wall-clock factor is CLAMPED. main's successful local runs average
    33.5 min and reach 47.3 min, so 2x the 3600s fleet ceiling covers them with
    headroom; the uncapped 3x would turn a backstop into a suggestion.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from robothor.engine.model_registry import chain_tempo_factor, tempo_factor
from robothor.engine.run_budget import effective_wallclock_ceiling, watchdog_budgets_for

LOCAL = "ollama_chat/qwen3.8:27b"
FAST_CLOUD = "openrouter/deepseek/deepseek-v4-flash"  # ttft_hint_ms=900
PRIMARY = "openrouter/xiaomi/mimo-v2.5"  # ttft_hint_ms=3000, the baseline


class TestTempo:
    def test_the_tempo_derivation_has_a_real_production_call_site(self):
        """Anti-inertness — and the first draft of this test was itself inert.

        Grepping for `ttft_hint_ms` passed on a COMMENT in llm_client that
        merely mentioned the field, which is precisely the "must_contain greps
        the name in prose" failure this repo keeps re-learning. So assert on a
        CALL to the derivation, with comment and docstring lines stripped.
        """
        root = Path(__file__).resolve().parents[2]  # robothor/
        out = subprocess.run(
            ["grep", "-rn", r"tempo_factor(", "--include=*.py", str(root)],
            capture_output=True,
            text=True,
        ).stdout
        call_sites = []
        for ln in out.splitlines():
            try:
                _path, _lineno, code = ln.split(":", 2)
            except ValueError:
                continue
            if "model_registry.py" in _path or "/tests/" in _path:
                continue  # the definition, and the tests that pin it
            stripped = code.strip()
            if stripped.startswith(("#", '"', "'", "*")):
                continue  # a mention, not a call
            if "import" in stripped and "(" not in stripped.split("import", 1)[1]:
                continue
            call_sites.append(ln)

        assert call_sites, (
            "tempo_factor has no production call site — the registry's latency "
            "metadata is inert again, which is the state this whole change exists "
            "to end"
        )

    def test_the_local_tier_is_three_times_the_calibration_baseline(self):
        assert tempo_factor(LOCAL) == pytest.approx(3.0)

    def test_a_fast_cloud_model_never_shrinks_a_budget(self):
        """Scaling only ever loosens; a manifest number is a floor."""
        assert tempo_factor(FAST_CLOUD) == pytest.approx(1.0)

    def test_the_baseline_model_is_exactly_one(self):
        assert tempo_factor(PRIMARY) == pytest.approx(1.0)

    def test_an_unknown_model_is_treated_as_baseline(self):
        assert tempo_factor("some/model-nobody-registered") == pytest.approx(1.0)

    def test_the_chain_is_sized_by_its_slowest_member(self):
        """Computed once from the whole chain, so a mid-run fallback to the
        local tier finds the budgets already correct — no retune, no mutating
        a deadline the run is already relying on."""
        assert chain_tempo_factor([PRIMARY, LOCAL]) == pytest.approx(3.0)

    def test_an_empty_chain_is_baseline(self):
        assert chain_tempo_factor([]) == pytest.approx(1.0)


class TestBudgets:
    def _cfg(self, **kw):
        from robothor.engine.models import AgentConfig

        base = {
            "id": "t",
            "name": "T",
            "model_primary": PRIMARY,
            "model_fallbacks": [LOCAL],
            "timeout_seconds": 0,
            "stall_timeout_seconds": 0,
            "early_stall_timeout_seconds": 0,
        }
        base.update(kw)
        return AgentConfig(**base)

    def test_a_disabled_budget_stays_disabled(self):
        """The single most dangerous edge in the phase."""
        b = watchdog_budgets_for(self._cfg())
        assert b.stall == 0
        assert b.early_stall == 0

    def test_a_cloud_calibrated_early_stall_is_scaled_for_the_local_tier(self):
        b = watchdog_budgets_for(self._cfg(early_stall_timeout_seconds=120))
        assert b.early_stall == 360  # 120 * 3.0

    def test_a_cloud_calibrated_stall_is_scaled_for_the_local_tier(self):
        b = watchdog_budgets_for(self._cfg(stall_timeout_seconds=120))
        assert b.stall == 360

    def test_the_wall_clock_factor_is_capped_at_two(self):
        """3600 * min(3.0, 2.0) = 7200, not 10800."""
        assert effective_wallclock_ceiling(0, models=[PRIMARY, LOCAL]) == 7200

    def test_an_agents_explicit_ceiling_is_scaled_not_replaced(self):
        assert effective_wallclock_ceiling(1200, models=[PRIMARY, LOCAL]) == 2400

    def test_a_cloud_only_chain_is_unchanged(self):
        """CLOUD behaviour must be byte-identical to today."""
        assert effective_wallclock_ceiling(1200, models=[PRIMARY]) == 1200
        assert effective_wallclock_ceiling(0, models=[PRIMARY]) == 3600

    def test_no_models_argument_preserves_the_old_signature(self):
        """Existing callers must keep working while they migrate."""
        assert effective_wallclock_ceiling(1200) == 1200

    def test_the_watchdog_and_the_loop_self_check_read_the_same_number(self):
        """The drift guard. Without it the loop kills at 3600 while the
        watchdog believes it has 7200."""
        cfg = self._cfg()
        chain = [cfg.model_primary, *cfg.model_fallbacks]
        assert watchdog_budgets_for(cfg).hard == effective_wallclock_ceiling(
            cfg.timeout_seconds, models=chain
        )


class TestStreamChunkTimeout:
    def test_stream_chunk_timeout_is_longer_for_the_local_tier(self):
        from robothor.engine.llm_client import STREAM_CHUNK_TIMEOUT, stream_chunk_timeout

        assert stream_chunk_timeout(LOCAL) == pytest.approx(STREAM_CHUNK_TIMEOUT * 3.0)
        assert stream_chunk_timeout(PRIMARY) == pytest.approx(STREAM_CHUNK_TIMEOUT)


class TestTheRunnerActuallyUsesIt:
    """A scaled number nothing reads is the failure this change exists to end."""

    def test_the_runner_derives_budgets_from_the_single_source(self):
        """Source-anchored: the runner must not re-derive the ceiling inline.

        The inline derivation is what let runner.py:760 and runner.py:2025
        disagree. If someone reintroduces `_fleet_wallclock_ceiling()` at the
        watchdog construction site, this fails.
        """
        src = (Path(__file__).resolve().parents[1] / "runner.py").read_text()
        assert "watchdog_budgets_for(agent_config)" in src, (
            "the runner no longer reads the scaled budgets — tier awareness is inert"
        )
        head = src.split("watchdog = _StallWatchdog(", 1)[0]
        tail = head.rsplit("watchdog_budgets_for(agent_config)", 1)[-1]
        assert "_fleet_wallclock_ceiling()" not in tail, (
            "the hard ceiling is being derived inline again, beside the single source"
        )

    def test_the_chunk_loop_no_longer_reads_the_flat_constant(self):
        src = (Path(__file__).resolve().parents[1] / "llm_client.py").read_text()
        assert "timeout=stream_chunk_timeout(model)" in src
        assert "timeout=STREAM_CHUNK_TIMEOUT" not in src, "the per-chunk timeout is flat again"
