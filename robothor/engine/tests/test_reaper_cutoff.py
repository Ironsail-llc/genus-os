"""The reaper must not call a healthy long run a crash.

2026-08-27. `_cleanup_stale_runs` reaped every `running` row older than a
hardcoded 30 minutes and labelled it with `classify_reap_reason`, i.e. as a
daemon restart or a crash. On the local tier `main`'s SUCCESSFUL runs average
33.5 minutes and reach 47.3 — so the reaper was tombstoning live, healthy work
and filing it as a crash, while the watchdog that actually owns the run's clock
believed it had up to 7200s.

Two mechanisms, deliberately, because one number cannot serve both:

* An ORPHAN is any run that predates the current daemon boot. Nothing is
  executing it by definition, so it is reaped immediately -- strictly faster
  than the old 30-minute wait.
* A LIVE run is reaped only past the ceiling its own watchdog would enforce,
  plus grace for finalization. Anything shorter contradicts the watchdog.
"""

from __future__ import annotations

from robothor.engine.daemon import REAP_GRACE_SECONDS, stale_run_cutoff_seconds
from robothor.engine.run_budget import effective_wallclock_ceiling


class TestTheCutoffAgreesWithTheWatchdog:
    def test_the_reaper_never_fires_before_the_wall_clock_ceiling(self):
        """The invariant. A shorter cutoff means the reaper overrules the
        watchdog and calls a run it is still legitimately executing a crash."""
        import os

        last_resort = os.environ.get("ROBOTHOR_LAST_RESORT_MODEL", "") or "ollama_chat/qwen3.8:27b"
        ceiling = effective_wallclock_ceiling(0, models=[last_resort])
        assert stale_run_cutoff_seconds() > ceiling

    def test_the_cutoff_covers_a_successful_local_tier_run(self):
        """47.3 min is not a hypothetical -- it is main's measured maximum
        SUCCESSFUL run on the local tier. The number lives here so a future
        tuner has to argue with the data rather than the constant."""
        assert stale_run_cutoff_seconds() >= 47.3 * 60

    def test_the_grace_covers_finalization(self):
        """A run may still be writing its summary after the loop ends."""
        from robothor.engine.finalization_budget import FINALIZATION_TOTAL_BUDGET

        assert REAP_GRACE_SECONDS >= FINALIZATION_TOTAL_BUDGET

    def test_the_old_thirty_minute_constant_is_gone(self):
        """Source-anchored: a second hardcoded interval must not reappear.

        Checks CODE, not prose. The phrase "30 minutes" now appears in the
        comments explaining what this replaced, and a naive grep would trip on
        that -- the third time in this change that a guard nearly passed (or
        failed) on a comment rather than a call.
        """
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "daemon.py").read_text()
        body = src.split("def _cleanup_stale_runs", 1)[-1].split("\ndef ", 1)[0]
        code = "\n".join(
            ln for ln in body.splitlines() if not ln.lstrip().startswith(("#", '"', "'"))
        )
        assert "INTERVAL '30 minutes'" not in code, "the flat interval is back"
        assert "stale_run_cutoff_seconds" in code, "the reaper no longer asks for a real cutoff"


class TestOrphansAreReapedImmediately:
    def test_the_query_selects_orphans_by_daemon_boot(self):
        """A run predating this daemon has no process behind it, whatever its
        age. Reaping it does not need to wait for the long live-run cutoff."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "daemon.py").read_text()
        block = src.split("def _cleanup_stale_runs", 1)[-1].split("\ndef ", 1)[0]
        assert "started_at <" in block
        # both arms present: an orphan arm keyed to boot, and a live-run arm
        assert block.count("started_at <") >= 2, "expected a two-tier predicate"
