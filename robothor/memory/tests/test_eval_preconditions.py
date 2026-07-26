"""Pure preconditions for the memory eval: refuse to run rather than lie.

The eval has been unrunnable for weeks — row-level security rejects its seed
writes because the process carries the production tenant while the suite seeds
under ``memory-eval``. That failure was indistinguishable from a normal
non-zero exit, so "the memory eval is failing" and "the memory eval cannot
execute at all" looked identical to anything reading the exit code.

Two guarantees are pinned here, both pure so they need no database and no
patching of collaborators:

1. Exit 3 means "could not run", exit 2 means "cases failed", 0 means passed.
   A harness that cannot run must never be mistakable for a pass *or* for an
   ordinary failure — the first hides a broken gate, the second gets muted as
   a flaky test.
2. Seeding refuses the production tenant outright. The existing guard only
   covered cleanup, so a mistyped ``--tenant`` would write fixture facts into
   the operator's real memory and only decline to delete them afterwards.
"""

from __future__ import annotations

import pytest

from robothor.constants import DEFAULT_TENANT
from robothor.memory.eval import EvalPreconditionError, exit_code_for


class TestExitCodeSeparation:
    def test_all_passed_is_zero(self):
        assert exit_code_for({"passed": 12, "total": 12}, blocked=None) == 0

    def test_below_the_floor_is_two(self):
        # This asserted 11/12 -> 2 back when the gate demanded perfection. The
        # suite is now 267 generated cases with a measured 0.9476 baseline, so
        # "any failure fails the run" meant the nightly unit paged every single
        # night — and a gate that always pages gets muted. The contract is now a
        # floor (DEFAULT_MIN_PASS_RATE), and 11/12 = 0.917 clears it.
        assert exit_code_for({"passed": 11, "total": 12}, blocked=None) == 0
        assert exit_code_for({"passed": 8, "total": 12}, blocked=None) == 2

    def test_blocked_is_three_even_when_report_looks_perfect(self):
        """A blocker outranks the report. A stale perfect report must not mask it."""
        assert exit_code_for({"passed": 12, "total": 12}, blocked="RLS tenant mismatch") == 3

    def test_missing_report_is_three_not_zero(self):
        """No report means the run did not happen — that is 'could not run', not 'passed'."""
        assert exit_code_for(None, blocked=None) == 3

    def test_empty_suite_is_three_not_zero(self):
        """0 of 0 passing is vacuous. A gate that grades an empty suite green is the
        exact 'clean signal from an empty table' failure this codebase has a
        standing lesson about."""
        assert exit_code_for({"passed": 0, "total": 0}, blocked=None) == 3


class TestSeedTenantGuard:
    @pytest.mark.asyncio
    async def test_run_suite_refuses_production_tenant_before_touching_disk(self, tmp_path):
        """The guard must fire before suite loading, seeding, or any DB call.

        Passing a suite path that does not exist proves ordering: if the guard
        ran after load_suite we would get FileNotFoundError instead.
        """
        from robothor.memory.eval import run_suite

        with pytest.raises(EvalPreconditionError, match=DEFAULT_TENANT):
            await run_suite(tmp_path / "does-not-exist.yaml", tenant_id=DEFAULT_TENANT)

    @pytest.mark.asyncio
    async def test_run_suite_refuses_empty_tenant(self, tmp_path):
        """An empty tenant resolves to the default at the DAL layer — same blast radius."""
        from robothor.memory.eval import run_suite

        with pytest.raises(EvalPreconditionError):
            await run_suite(tmp_path / "does-not-exist.yaml", tenant_id="")
