"""The fleet must fail when a native-runner suite goes stale.

`docs/benchmarks/memory/` has a suite.yaml but deliberately has no
`docs/agents/memory.yaml` — grading an agent's prose would not measure the
retrieval path. Today that combination lands the suite in
``skipped_no_manifest`` and the fleet reports success, so the nightly memory
eval has zero consumers: if the timer dies, nothing anywhere turns red.

A gate nobody reads is not a gate. These tests pin the two pieces that make it
one: suites can declare ``runner: native`` (run by a scheduled unit, not by
spawning an agent), and the fleet asserts a fresh row exists for each of them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from robothor.engine.tools.handlers.benchmark import (
    NATIVE_SUITE_MAX_AGE_HOURS,
    native_freshness_verdict,
    suite_runner,
)

NOW = datetime(2026, 7, 25, 4, 0, tzinfo=UTC)


def _write(tmp_path, body: str):
    p = tmp_path / "suite.yaml"
    p.write_text(body)
    return p


class TestSuiteRunner:
    def test_absent_key_defaults_to_agent(self, tmp_path):
        # Every existing suite omits the key; none may change behaviour.
        assert suite_runner(_write(tmp_path, "id: x\ncases: []\n")) == "agent"

    def test_native_is_read(self, tmp_path):
        assert suite_runner(_write(tmp_path, "id: x\nrunner: native\n")) == "native"

    def test_unknown_runner_falls_back_to_agent(self, tmp_path):
        # A typo must not silently exempt a suite from being run.
        assert suite_runner(_write(tmp_path, "id: x\nrunner: nativ\n")) == "agent"

    def test_missing_file_is_agent(self, tmp_path):
        assert suite_runner(tmp_path / "nope.yaml") == "agent"

    def test_unparseable_file_is_agent(self, tmp_path):
        assert suite_runner(_write(tmp_path, "id: [unclosed\n")) == "agent"


class TestFreshnessVerdict:
    def test_fresh_row_passes(self):
        v = native_freshness_verdict("memory", NOW - timedelta(hours=2), now=NOW)
        assert v["stale"] is False
        assert v["error"] is None
        assert v["age_hours"] == pytest.approx(2.0)

    def test_never_run_is_stale(self):
        # The failure mode that matters most: the timer was never enabled.
        v = native_freshness_verdict("memory", None, now=NOW)
        assert v["stale"] is True
        assert "never" in v["error"]

    def test_stale_row_is_stale(self):
        v = native_freshness_verdict("memory", NOW - timedelta(hours=30), now=NOW)
        assert v["stale"] is True
        assert "30.0h" in v["error"]

    def test_boundary_is_generous_by_one_run(self):
        # 26h, not 24h: a nightly unit that slips an hour must not page.
        assert NATIVE_SUITE_MAX_AGE_HOURS == 26
        assert native_freshness_verdict("m", NOW - timedelta(hours=25.9), now=NOW)["stale"] is False
        assert native_freshness_verdict("m", NOW - timedelta(hours=26.1), now=NOW)["stale"] is True

    def test_naive_timestamp_is_not_a_crash(self):
        # psycopg can hand back a naive datetime depending on the column type;
        # a TypeError here would be swallowed by the fleet's except and read as
        # "suite fine".
        v = native_freshness_verdict("m", datetime(2026, 7, 25, 2, 0), now=NOW)
        assert v["stale"] is False

    def test_future_timestamp_is_not_stale(self):
        # Clock skew must not manufacture a page.
        v = native_freshness_verdict("m", NOW + timedelta(hours=1), now=NOW)
        assert v["stale"] is False


@pytest.mark.integration
class TestFleetActuallyGoesRed:
    """The wiring probe. A verdict function that returns stale=True proves
    nothing on its own — six controls in this repo were built, wired, tested
    and completely inert. This fires a real violation through the real fleet
    entry point and asserts the fleet reports failure.
    """

    async def _run_fleet(self, workspace):
        from robothor.engine.tools.dispatch import ToolContext
        from robothor.engine.tools.handlers.benchmark import _benchmark_run_fleet

        return await _benchmark_run_fleet({}, ToolContext(workspace=str(workspace)))

    @pytest.mark.asyncio
    async def test_stale_native_suite_fails_the_fleet(self, tmp_path):
        bench = tmp_path / "docs" / "benchmarks" / "ghost-never-run"
        bench.mkdir(parents=True)
        (bench / "suite.yaml").write_text("id: ghost\nrunner: native\ncases: []\n")
        (tmp_path / "docs" / "agents").mkdir(parents=True)

        result = await self._run_fleet(tmp_path)

        assert result["success"] is False, "a dead native runner must fail the fleet"
        assert result["stale_suites"] == ["ghost-never-run"]
        assert "never run" in result["native_suites"][0]["error"]
        # And it must not have been quietly filed as a missing manifest.
        assert result["skipped_no_manifest"] == []

    @pytest.mark.asyncio
    async def test_fresh_native_suite_passes_the_fleet(self, tmp_path):
        # Negative control: without it the test above only proves the check is
        # stuck on. It seeds its own fresh row rather than leaning on the live
        # `memory` row — a test that goes red because production's timer
        # slipped is a test that gets muted.
        from robothor.constants import DEFAULT_TENANT
        from robothor.db.connection import get_connection

        agent_id = "native-suite-freshness-probe"
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO benchmark_results "
                "(agent_id, suite_id, suite_path, run_at, total_cases, passed, "
                " failed, pass_rate, triggered_by, tenant_id) "
                "VALUES (%s, 'probe', 'probe', NOW(), 0, 0, 0, 1.0, 'test', %s)",
                (agent_id, DEFAULT_TENANT),
            )
            conn.commit()
        try:
            bench = tmp_path / "docs" / "benchmarks" / agent_id
            bench.mkdir(parents=True)
            (bench / "suite.yaml").write_text("id: p\nrunner: native\ncases: []\n")
            (tmp_path / "docs" / "agents").mkdir(parents=True)

            result = await self._run_fleet(tmp_path)

            assert result["success"] is True
            assert result["stale_suites"] == []
            assert result["native_suites"][0]["age_hours"] < 1
        finally:
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM benchmark_results WHERE agent_id = %s", (agent_id,))
                conn.commit()
