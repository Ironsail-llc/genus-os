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


class TestSuiteRunsOnlyInEvalTenants:
    """The suite deletes every fact (and relation) in the tenant it is given.
    It may only ever be given an evaluation tenant: ``memory-eval`` or
    ``memory-eval-<suffix>``. One predicate decides that for both modes."""

    @pytest.mark.parametrize(
        "tenant", ["robothor-primary", "acme", "memory-evaluation", "prod-memory-eval"]
    )
    @pytest.mark.asyncio
    async def test_run_suite_refuses_a_real_tenant_before_touching_disk(self, tmp_path, tenant):
        from robothor.memory.eval import run_suite

        with pytest.raises(EvalPreconditionError, match="evaluation tenant"):
            await run_suite(tmp_path / "does-not-exist.yaml", tenant_id=tenant)

    def test_cleanup_tenant_refuses_a_real_tenant_and_touches_nothing(self, monkeypatch):
        from robothor.memory.eval import _cleanup_tenant

        def _boom(*_a, **_k):
            raise AssertionError("no database call may happen for a non-eval tenant")

        monkeypatch.setattr("robothor.db.connection.get_connection", _boom)
        for tenant in ("acme", DEFAULT_TENANT, ""):
            with pytest.raises(EvalPreconditionError):
                _cleanup_tenant(tenant)

    def test_one_predicate_serves_both_modes(self):
        from robothor.memory import eval as suite
        from robothor.memory import eval_world_seed as world

        assert world.is_eval_tenant is suite.is_eval_tenant
        assert suite.is_eval_tenant("memory-eval")
        assert suite.is_eval_tenant("memory-eval-world")
        assert not suite.is_eval_tenant("acme")

    def test_cli_suite_mode_refuses_a_real_tenant_with_exit_three(self, monkeypatch, capsys):
        from robothor.cli import _build_parser
        from robothor.cli import memory_eval as cli

        def no_preflight(_t):
            raise AssertionError("must refuse before preflight touches the database")

        async def no_suite(*_a, **_k):
            raise AssertionError("must refuse before the suite runs")

        monkeypatch.setattr(cli, "preflight", no_preflight)
        monkeypatch.setattr(cli, "run_suite", no_suite)
        args = _build_parser().parse_args(["memory-eval", "--tenant", "acme"])
        assert cli.cmd_memory_eval(args) == 3
        assert "evaluation tenant" in capsys.readouterr().out

    def test_the_nightly_tenant_passes_the_guard(self):
        from robothor.memory.eval import EVAL_TENANT, require_eval_tenant

        assert require_eval_tenant(EVAL_TENANT) == EVAL_TENANT


def _db_reachable() -> bool:
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            conn.cursor().execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 — any failure means "no database here"
        return False


@pytest.mark.integration
class TestSuiteLeavesARealTenantAlone:
    @pytest.fixture
    def real_tenant(self):
        import uuid

        if not _db_reachable():
            pytest.skip("no PostgreSQL reachable for the integration test")
        from robothor.db.connection import get_connection

        tenant = f"test-{uuid.uuid4().hex}"
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s)", (tenant, "t"))
            cur.execute(
                "INSERT INTO memory_facts (fact_text, category, tenant_id) "
                "VALUES ('Alice runs Helios.', 'project', %s) RETURNING id",
                (tenant,),
            )
            fact_id = cur.fetchone()[0]
            ids = []
            for name, kind in (("Alice", "person"), ("Helios", "project")):
                cur.execute(
                    "INSERT INTO memory_entities (name, entity_type, tenant_id) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (name, kind, tenant),
                )
                ids.append(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO memory_relations "
                "(source_entity_id, target_entity_id, relation_type, fact_id, tenant_id) "
                "VALUES (%s, %s, 'runs', %s, %s)",
                (ids[0], ids[1], fact_id, tenant),
            )
        yield tenant
        with get_connection() as conn:
            cur = conn.cursor()
            for table in ("memory_relations", "memory_facts", "memory_entities"):
                cur.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant,))  # noqa: S608
            cur.execute("DELETE FROM crm_tenants WHERE id = %s", (tenant,))

    @staticmethod
    def _counts(tenant: str) -> tuple[int, int]:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM memory_facts WHERE tenant_id = %s", (tenant,))
            facts = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM memory_relations WHERE tenant_id = %s", (tenant,))
            return facts, cur.fetchone()[0]

    @pytest.mark.asyncio
    async def test_suite_refuses_and_every_row_survives(self, real_tenant):
        from pathlib import Path

        from robothor.memory.eval import _cleanup_tenant, run_suite

        suite = (
            Path(__file__).resolve().parents[3] / "docs" / "benchmarks" / "memory" / "suite.yaml"
        )
        assert self._counts(real_tenant) == (1, 1)
        with pytest.raises(EvalPreconditionError):
            await run_suite(suite, tenant_id=real_tenant)
        with pytest.raises(EvalPreconditionError):
            _cleanup_tenant(real_tenant)
        assert self._counts(real_tenant) == (1, 1)
