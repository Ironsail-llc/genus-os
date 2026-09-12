"""The runner is the part that must never make things worse.

It is the only caller of third-party and I/O-bound code in the doctor, so the
properties pinned here are the ones an operator depends on when the instance is
already broken: a check that hangs is reported as a failure and the RUN still
finishes; a plugin cannot take a built-in check's id; ``--fix`` touches only
what actually failed and never fires during a dry run; and the exit code says
one of exactly three things.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from robothor.doctor.context import DoctorContext
from robothor.doctor.model import Check, FixResult, Result
from robothor.doctor.runner import CheckSelectionError, run_sync


def _ctx(**kwargs) -> DoctorContext:
    kwargs.setdefault("timeout_s", 0.2)
    return DoctorContext(**kwargs)


def _check(check_id: str, status: str = "pass", **kwargs) -> Check:
    async def run(_ctx: DoctorContext) -> Result:
        return Result(status=status, detail=f"{check_id} says {status}")

    return Check(
        id=check_id,
        title=kwargs.pop("title", check_id),
        category=kwargs.pop("category", check_id.split(".")[0]),
        severity=kwargs.pop("severity", "required"),
        run=kwargs.pop("run", run),
        **kwargs,
    )


# ── timeouts ─────────────────────────────────────────────────────────────────


def test_a_check_that_hangs_is_a_failure_and_the_run_finishes() -> None:
    async def forever(_ctx: DoctorContext) -> Result:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    checks = [_check("slow.one", run=forever), _check("fast.one")]
    report = run_sync(_ctx(timeout_s=0.05), checks=checks)

    by_id = {row.id: row for row in report.results}
    assert by_id["slow.one"].status == "fail"
    assert "timed out after 0.05s" in by_id["slow.one"].detail
    assert by_id["fast.one"].status == "pass"


def test_a_check_that_blocks_synchronously_is_still_timed_out() -> None:
    """asyncio.timeout can only cancel at an await. A coroutine that does its
    work synchronously -- a wedged stat(), a DNS lookup, an import -- outran
    both budgets and reported success, which made the 30s bound on the bridge's
    worker thread a suggestion rather than a bound."""
    import time

    async def blocking(_ctx: DoctorContext) -> Result:
        """A stub."""
        time.sleep(5)
        return Result(status="pass", detail="I ignored the budget")

    started = time.monotonic()
    report = run_sync(_ctx(timeout_s=0.2), checks=[_check("slow.sync", run=blocking)])
    elapsed = time.monotonic() - started

    assert report.results[0].status == "fail"
    assert "timed out" in report.results[0].detail
    assert elapsed < 2.0, f"the run took {elapsed:.2f}s despite a 0.2s budget"


def test_the_total_budget_is_hard_even_for_synchronous_checks() -> None:
    import time

    async def blocking(_ctx: DoctorContext) -> Result:
        """A stub."""
        time.sleep(5)
        raise AssertionError("unreachable")

    checks = [_check(f"slow.{n}", run=blocking) for n in range(4)]
    started = time.monotonic()
    report = run_sync(_ctx(timeout_s=0.2, total_timeout_s=0.5), checks=checks)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"the run took {elapsed:.2f}s despite a 0.5s total budget"
    assert all(row.status == "fail" for row in report.results)


def test_a_sync_callable_check_is_accepted(monkeypatch) -> None:
    """A plugin may contribute a plain function. It must be run, and time-boxed
    like everything else, not awaited into a TypeError."""

    def plain(_ctx: DoctorContext) -> Result:
        """A stub."""
        return Result(status="pass", detail="synchronous and fine")

    report = run_sync(_ctx(), checks=[_check("plain.one", run=plain)])
    assert report.results[0].status == "pass"
    assert report.results[0].detail == "synchronous and fine"


def test_a_repair_cannot_overshoot_the_total_budget() -> None:
    """`_repair` used the per-check budget and its re-run got a fresh one, so
    --fix could overshoot the deadline by roughly twice timeout_s."""
    import time

    async def run(_ctx: DoctorContext) -> Result:
        """A stub."""
        return Result(status="fail", detail="broken", fixable=True)

    async def fix(_ctx: DoctorContext) -> FixResult:
        time.sleep(5)
        raise AssertionError("unreachable")

    check = Check(
        id="db.thing", title="t", category="database", severity="required", run=run, fix=fix
    )
    started = time.monotonic()
    report = run_sync(_ctx(timeout_s=0.2, total_timeout_s=0.4, fix=True), checks=[check])
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"the repair took {elapsed:.2f}s past a 0.4s total budget"
    assert report.results[0].status == "fail"


def test_a_check_that_raises_is_a_failure_not_a_traceback() -> None:
    async def explode(_ctx: DoctorContext) -> Result:
        raise RuntimeError("psycopg2 is unhappy")

    report = run_sync(_ctx(), checks=[_check("boom.one", run=explode)])

    assert report.results[0].status == "fail"
    assert "RuntimeError" in report.results[0].detail


# ── filtering ────────────────────────────────────────────────────────────────


def test_only_runs_one_check() -> None:
    checks = [_check("a.one"), _check("b.one")]
    report = run_sync(_ctx(), checks=checks, only="a.one")
    assert [row.id for row in report.results] == ["a.one"]


def test_category_runs_one_category() -> None:
    checks = [_check("a.one", category="a"), _check("a.two", category="a"), _check("b.one")]
    report = run_sync(_ctx(), checks=checks, category="a")
    assert [row.id for row in report.results] == ["a.one", "a.two"]


def test_an_unknown_only_id_is_a_doctor_error_not_an_empty_pass() -> None:
    """Exit 0 on a typo would report a healthy instance nobody checked."""
    report = run_sync(_ctx(), checks=[_check("a.one")], only="a.typo")
    assert report.exit_code == 2
    assert "a.typo" in report.error_detail


def test_an_unknown_category_is_a_doctor_error() -> None:
    report = run_sync(_ctx(), checks=[_check("a.one", category="a")], category="zz")
    assert report.exit_code == 2


# ── exit codes ───────────────────────────────────────────────────────────────


def test_exit_zero_when_nothing_required_failed() -> None:
    checks = [_check("a.one"), _check("b.one", status="fail", severity="recommended")]
    report = run_sync(_ctx(), checks=checks)
    assert report.exit_code == 0
    assert report.status == "degraded"


def test_exit_one_when_a_required_check_failed() -> None:
    report = run_sync(_ctx(), checks=[_check("a.one", status="fail")])
    assert report.exit_code == 1
    assert report.status == "degraded"


def test_exit_two_when_the_doctor_itself_errored() -> None:
    def blow_up() -> list[Check]:
        raise RuntimeError("the registry is broken")

    report = run_sync(_ctx(), checks_factory=blow_up)
    assert report.exit_code == 2
    assert report.status == "degraded"
    assert "the registry is broken" in report.error_detail


def test_an_info_failure_does_not_paint_the_instance_degraded() -> None:
    """`status` is what the Helm's banner renders. An informational finding --
    a Slack token that is not shaped like one, on an instance that does not use
    Slack -- would otherwise show the appliance as degraded while the CLI
    exits 0, which is two surfaces disagreeing about the same run."""
    checks = [_check("a.one"), _check("slack.token", status="fail", severity="info")]
    report = run_sync(_ctx(), checks=checks)

    assert report.status == "ok"
    assert report.exit_code == 0
    assert report.summary["required_failed"] == 0
    assert report.summary["recommended_failed"] == 0


def test_a_recommended_failure_still_reads_degraded() -> None:
    checks = [_check("a.one"), _check("b.one", status="fail", severity="recommended")]
    assert run_sync(_ctx(), checks=checks).status == "degraded"


def test_status_is_ok_when_everything_passed_or_skipped() -> None:
    checks = [_check("a.one"), _check("b.one", status="skip", severity="recommended")]
    report = run_sync(_ctx(), checks=checks)
    assert report.status == "ok"
    assert report.exit_code == 0
    assert report.summary == {
        "required_failed": 0,
        "recommended_failed": 0,
        "passed": 1,
        "skipped": 1,
    }


# ── fixing ───────────────────────────────────────────────────────────────────


def _fixable(state: dict) -> Check:
    async def run(_ctx: DoctorContext) -> Result:
        if state["repaired"]:
            return Result(status="pass", detail="seeded")
        return Result(status="fail", detail="not seeded", fixable=True)

    async def fix(_ctx: DoctorContext) -> FixResult:
        state["fix_calls"] += 1
        state["repaired"] = True
        return FixResult(changed=True, detail="seeded the row")

    return Check(
        id="db.thing", title="thing", category="database", severity="required", run=run, fix=fix
    )


def test_fix_repairs_a_failed_fixable_check_and_the_recheck_passes() -> None:
    state = {"repaired": False, "fix_calls": 0}
    report = run_sync(_ctx(fix=True), checks=[_fixable(state)])

    assert state["fix_calls"] == 1
    assert report.results[0].status == "pass"
    assert report.exit_code == 0
    assert "seeded the row" in report.results[0].detail


def test_fix_does_nothing_without_the_flag() -> None:
    state = {"repaired": False, "fix_calls": 0}
    report = run_sync(_ctx(), checks=[_fixable(state)])

    assert state["fix_calls"] == 0
    assert report.results[0].status == "fail"
    assert report.results[0].fixable is True


def test_fix_never_runs_in_a_dry_run() -> None:
    state = {"repaired": False, "fix_calls": 0}
    report = run_sync(_ctx(fix=True, dry_run=True), checks=[_fixable(state)])

    assert state["fix_calls"] == 0
    assert report.results[0].status == "fail"
    assert "--fix would" in report.results[0].detail


def test_fix_is_not_run_for_a_check_that_passed() -> None:
    state = {"repaired": True, "fix_calls": 0}
    run_sync(_ctx(fix=True), checks=[_fixable(state)])
    assert state["fix_calls"] == 0


def test_a_failing_fix_leaves_the_failure_and_names_the_reason() -> None:
    async def run(_ctx: DoctorContext) -> Result:
        return Result(status="fail", detail="not seeded", fixable=True)

    async def fix(_ctx: DoctorContext) -> FixResult:
        raise PermissionError("must own the ledger")

    check = Check(
        id="db.thing", title="thing", category="database", severity="required", run=run, fix=fix
    )
    report = run_sync(_ctx(fix=True), checks=[check])

    assert report.results[0].status == "fail"
    assert "PermissionError" in report.results[0].detail


# ── a check that reports several independent findings ────────────────────────


def test_a_check_may_expand_into_one_row_per_finding() -> None:
    async def run(_ctx: DoctorContext) -> list[Result]:
        return [
            Result(status="fail", detail="unit a drifted", sub_id="DRIFT"),
            Result(status="fail", detail="unit b has no template", sub_id="NO-TEMPLATE"),
        ]

    check = Check(
        id="host.unit_drift",
        title="host units",
        category="host",
        severity="recommended",
        run=run,
    )
    report = run_sync(_ctx(), checks=[check])

    assert [row.id for row in report.results] == [
        "host.unit_drift:DRIFT",
        "host.unit_drift:NO-TEMPLATE",
    ]
    assert report.summary["recommended_failed"] == 2
    assert report.exit_code == 0


def test_an_empty_finding_list_is_a_single_pass() -> None:
    async def run(_ctx: DoctorContext) -> list[Result]:
        return []

    check = Check(id="host.unit_drift", title="t", category="host", severity="recommended", run=run)
    report = run_sync(_ctx(), checks=[check])
    assert [(row.id, row.status) for row in report.results] == [("host.unit_drift", "pass")]


# ── plugin checks ────────────────────────────────────────────────────────────


class _FakeEntryPoint:
    group = "genus.doctor"

    def __init__(self, dist_name: str, payload: dict) -> None:
        self.name = "checks"
        self.dist = SimpleNamespace(name=dist_name)
        self._payload = payload

    def load(self) -> dict:
        return self._payload


def _plugin_payload(*checks: Check) -> dict:
    return {"genus_contract_version": "1.0", "checks": {check.id: check for check in checks}}


def test_a_well_named_plugin_check_is_registered() -> None:
    from robothor.doctor.registry import plugin_checks

    ep = _FakeEntryPoint("genus-widget", _plugin_payload(_check("genus_widget.ping")))
    assert [check.id for check in plugin_checks([ep])] == ["genus_widget.ping"]


def test_a_plugin_check_that_shadows_a_builtin_is_refused(caplog) -> None:
    from robothor.doctor.registry import builtin_ids, plugin_checks

    victim = sorted(builtin_ids())[0]
    ep = _FakeEntryPoint(
        "genus-widget", _plugin_payload(_check(victim), _check("genus_widget.ping"))
    )
    with caplog.at_level("WARNING"):
        assert plugin_checks([ep]) == ()
    assert victim in caplog.text


def test_a_plugin_check_without_its_distribution_prefix_is_refused(caplog) -> None:
    from robothor.doctor.registry import plugin_checks

    ep = _FakeEntryPoint("genus-widget", _plugin_payload(_check("ping.pong")))
    with caplog.at_level("WARNING"):
        assert plugin_checks([ep]) == ()
    assert "ping.pong" in caplog.text


def test_one_bad_id_skips_the_whole_plugin(caplog) -> None:
    """All-or-nothing: a half-registered plugin is a state nobody tested."""
    from robothor.doctor.registry import plugin_checks

    ep = _FakeEntryPoint(
        "genus-widget", _plugin_payload(_check("genus_widget.ok"), _check("elsewhere.bad"))
    )
    with caplog.at_level("WARNING"):
        assert plugin_checks([ep]) == ()


def test_a_broken_plugin_does_not_stop_a_good_one(caplog) -> None:
    from robothor.doctor.registry import plugin_checks

    good = _FakeEntryPoint("genus-widget", _plugin_payload(_check("genus_widget.ping")))
    bad = _FakeEntryPoint("genus-broken", _plugin_payload(_check("nope.ping")))
    with caplog.at_level("WARNING"):
        ids = [check.id for check in plugin_checks([bad, good])]
    assert ids == ["genus_widget.ping"]


# ── the built-in registry ────────────────────────────────────────────────────


def test_every_builtin_check_id_is_unique_and_prefixed_by_nothing_odd() -> None:
    from robothor.doctor.registry import builtin_checks

    checks = builtin_checks()
    ids = [check.id for check in checks]
    assert len(ids) == len(set(ids))
    assert all(check.severity in {"required", "recommended", "info"} for check in checks)


def test_every_builtin_check_explains_itself_to_an_operator() -> None:
    """The docstring is what the operator reads when a check fails."""
    from robothor.doctor.registry import builtin_checks

    undocumented = [check.id for check in builtin_checks() if not (check.run.__doc__ or "").strip()]
    assert undocumented == []


def test_the_registry_covers_every_documented_category() -> None:
    from robothor.doctor.registry import categories

    assert set(categories()) >= {
        "config",
        "database",
        "redis",
        "models",
        "channels",
        "services",
        "manifests",
        "identity",
        "secrets",
        "host",
    }


def test_importing_the_doctor_needs_no_database_and_no_engine() -> None:
    """A doctor that cannot start on a broken box is not a doctor."""
    import subprocess
    import sys

    code = "import robothor.doctor.registry as r; print(len(r.builtin_checks()))"
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    assert int(done.stdout.strip()) > 0


def test_selection_error_is_raised_by_the_selector_itself() -> None:
    from robothor.doctor.runner import select

    with pytest.raises(CheckSelectionError):
        select([_check("a.one")], only="nope")
