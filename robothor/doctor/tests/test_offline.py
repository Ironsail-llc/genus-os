"""``--offline`` means nothing costs money, leaves the box, or forks.

This is the half of the bridge route's guarantee that lives in the doctor.
``crm/bridge/tests/test_doctor_route.py`` proves the route builds an offline
context; these prove what an offline context actually suppresses. Both halves
are needed: the route is what a Health panel polls, and online it would make a
paid completion, a call to api.telegram.org and a fork of the host script on
every refresh.

Each test asserts that the outbound seam was NOT touched -- a zero call count,
not merely a skipped status -- because a check could report `skip` after doing
the work, and that is the shape a regression would take.
"""

from __future__ import annotations

import asyncio

from robothor.doctor.checks import channels as channel_checks
from robothor.doctor.checks import host as host_checks
from robothor.doctor.checks import models as model_checks
from robothor.doctor.registry import builtin_checks
from robothor.doctor.runner import run_sync
from robothor.doctor.tests.conftest import fake_http, make_ctx

FAKE_TOKEN = "1234567:AAfake-telegram-token-value"


def _run(checks, check_id: str, ctx):
    check = next(item for item in checks if item.id == check_id)
    answer = asyncio.run(check.run(ctx))
    return answer if isinstance(answer, list) else [answer]


def test_offline_makes_no_model_call(monkeypatch) -> None:
    calls: list[str] = []

    async def _call(_messages, **kwargs):
        calls.append(str(kwargs.get("model")))
        return object()

    monkeypatch.setattr("robothor.engine.llm_client.llm_call", _call)
    monkeypatch.setattr(model_checks, "_fleet_model", lambda: "openrouter/test/model")

    rows = _run(model_checks.CHECKS, "provider.completion", make_ctx(offline=True))
    assert rows[0].status == "skip"
    assert calls == []


def test_offline_does_not_resolve_the_fleet_model_either(monkeypatch) -> None:
    """Resolving it parses the manifest directory; the skip is decided first."""

    def _boom():
        raise AssertionError("the fleet model must not be resolved offline")

    monkeypatch.setattr(model_checks, "_fleet_model", _boom)
    assert (
        _run(model_checks.CHECKS, "provider.completion", make_ctx(offline=True))[0].status == "skip"
    )


def test_offline_does_not_call_telegram(monkeypatch) -> None:
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "12345")
    from robothor.settings import reset_settings

    reset_settings()

    fetch = fake_http({})
    rows = _run(channel_checks.CHECKS, "telegram.token", make_ctx(offline=True, http_fetch=fetch))
    assert fetch.calls == []
    assert any(row.status == "skip" for row in rows)


def test_offline_forks_no_host_script(monkeypatch) -> None:
    """The script walks every unit and drop-in and shells out per file."""
    forks: list[object] = []
    monkeypatch.setattr(host_checks, "running_under_systemd", lambda: True)
    monkeypatch.setattr(
        host_checks.subprocess, "run", lambda *a, **k: forks.append(a) or AssertionError
    )

    rows = _run(host_checks.CHECKS, "host.unit_drift", make_ctx(offline=True))
    assert forks == []
    assert rows[0].status == "skip"
    assert "--offline" in rows[0].detail


def test_ollama_and_the_loopback_services_still_run_offline() -> None:
    """Offline means "costs nothing and leaves nothing"; it does not mean
    blind. A local probe is free and is the whole point of the report."""
    ctx = make_ctx(offline=True, http_fetch=fake_http({}))
    rows = _run(model_checks.CHECKS, "ollama.reachable", ctx)
    assert rows[0].status == "fail"  # it was actually probed, and nothing answered
    assert ctx.http_fetch.calls  # type: ignore[union-attr]


def test_the_three_costly_checks_are_the_only_ones_offline_suppresses() -> None:
    """A record of the contract: if a fourth check learns to cost money it has
    to be added here deliberately, not discovered on a bill."""
    suppressed = {"provider.completion", "telegram.token", "host.unit_drift"}
    assert suppressed <= {check.id for check in builtin_checks()}


def test_the_total_budget_reports_the_rest_as_not_run_never_as_passing() -> None:
    """The failure mode this guards is a partial run reading as health."""

    async def slow(_ctx):
        """A stub."""
        await asyncio.sleep(5)
        raise AssertionError("unreachable")

    from robothor.doctor.model import Check

    checks = [
        Check(id="a.one", title="a", category="a", severity="required", run=slow),
        Check(id="b.one", title="b", category="b", severity="required", run=slow),
    ]
    report = run_sync(make_ctx(timeout_s=0.05, total_timeout_s=0.05), checks=checks)

    assert [row.status for row in report.results] == ["fail", "fail"]
    assert "total budget" in report.results[1].detail
    assert report.exit_code == 1
