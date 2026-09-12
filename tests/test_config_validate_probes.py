"""``genus config validate`` must judge a service by an endpoint it is allowed to reach.

The bridge's ``/health`` is behind auth in production, so probing it from the
CLI answers 401 for a service that is perfectly healthy. The validator prefers
``/ready`` (unauthenticated on every service that has one) and only then falls
back to ``/health``; a 401/403 from the fallback still proves the service is up.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import TYPE_CHECKING

from robothor.config import probe_service

if TYPE_CHECKING:
    import pytest


class _Resp(io.BytesIO):
    def __init__(self, status: int) -> None:
        super().__init__(b"{}")
        self.status = status


def _fake_urlopen(routes: dict[str, int | Exception]):
    calls: list[str] = []

    @contextmanager
    def urlopen(req, timeout=0):  # noqa: ARG001 - signature mirrors urllib
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        outcome = routes.get(url, urllib.error.URLError("connection refused"))
        if isinstance(outcome, Exception):
            raise outcome
        yield _Resp(outcome)

    return urlopen, calls


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "denied", hdrs=None, fp=None)  # type: ignore[arg-type]


def test_ready_is_preferred_over_health(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "http://127.0.0.1:9100"
    urlopen, calls = _fake_urlopen(
        {f"{base}/ready": 200, f"{base}/health": _http_error(f"{base}/health", 401)}
    )
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    ok, detail = probe_service(base)

    assert ok is True
    assert detail == f"{base}/ready → 200"
    assert calls == [f"{base}/ready"]


def test_health_fallback_when_ready_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "http://127.0.0.1:8600"
    urlopen, calls = _fake_urlopen(
        {f"{base}/ready": _http_error(f"{base}/ready", 404), f"{base}/health": 200}
    )
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    ok, detail = probe_service(base)

    assert ok is True
    assert detail == f"{base}/health → 200"
    assert calls == [f"{base}/ready", f"{base}/health"]


def test_auth_gated_health_still_counts_as_up(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "http://127.0.0.1:9100"
    urlopen, _ = _fake_urlopen(
        {
            f"{base}/ready": _http_error(f"{base}/ready", 404),
            f"{base}/health": _http_error(f"{base}/health", 401),
        }
    )
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    ok, detail = probe_service(base)

    assert ok is True
    assert "401" in detail and "authenticated" in detail


def test_unreachable_service_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "http://127.0.0.1:9"
    urlopen, _ = _fake_urlopen({})
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    ok, detail = probe_service(base)

    assert ok is False
    assert "connection refused" in detail


def test_server_error_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "http://127.0.0.1:9099"
    urlopen, _ = _fake_urlopen(
        {
            f"{base}/ready": _http_error(f"{base}/ready", 503),
            f"{base}/health": _http_error(f"{base}/health", 500),
        }
    )
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    ok, detail = probe_service(base)

    assert ok is False
    assert "503" in detail  # /ready answered: not ready is the verdict, no fallback


# ── validate() is a shim over the doctor ──────────────────────────────────────


def test_validate_delegates_to_the_doctor_and_probes_nothing_itself(monkeypatch) -> None:
    """One doctor. `robothor.config.validate()` used to be a second, live-looking
    implementation of the same probes -- three env vars, four ports, the
    database, redis, ollama, three service endpoints and two paths -- and after
    `genus config validate` became an alias, nothing called it. A parallel
    implementation nobody runs is the shape every "inert control" on this
    instance has had.
    """
    from robothor.doctor.runner import CheckResult, DoctorReport

    seen = {}

    def _fake_run_sync(ctx, **_kwargs):
        seen["offline"] = ctx.offline
        return DoctorReport(
            results=[
                CheckResult(
                    id="db.connect",
                    title="PostgreSQL is reachable",
                    category="database",
                    severity="required",
                    status="pass",
                    detail="connected to robothor_memory",
                    fixable=False,
                ),
                CheckResult(
                    id="redis.connect",
                    title="Redis is reachable",
                    category="redis",
                    severity="required",
                    status="fail",
                    detail="refused",
                    fixable=False,
                ),
                CheckResult(
                    id="slack.token",
                    title="Slack bot token",
                    category="channels",
                    severity="info",
                    status="skip",
                    detail="no Slack bot token configured",
                    fixable=False,
                ),
            ]
        )

    monkeypatch.setattr("robothor.doctor.runner.run_sync", _fake_run_sync)

    from robothor.config import validate

    results = validate()

    assert seen["offline"] is True, "the shim must not make a paid call"
    assert [(name, ok) for name, ok, _detail in results] == [
        ("db.connect", True),
        ("redis.connect", False),
        ("slack.token", True),
    ]


def test_a_skipped_check_is_not_reported_as_a_failure_by_the_shim(monkeypatch) -> None:
    """The legacy tuple has one bool for three states. A check that could not
    run has not failed, and callers of this shape treated False as broken."""
    from robothor.doctor.runner import CheckResult, DoctorReport

    row = CheckResult(
        id="provider.completion",
        title="t",
        category="models",
        severity="required",
        status="skip",
        detail="no provider credential configured",
        fixable=False,
    )
    monkeypatch.setattr(
        "robothor.doctor.runner.run_sync", lambda ctx, **kw: DoctorReport(results=[row])
    )

    from robothor.config import validate

    name, ok, detail = validate()[0]
    assert ok is True
    assert "skipped" in detail


def test_the_old_probe_helpers_are_gone(monkeypatch) -> None:
    """`required_env_checks` existed only to feed the deleted body. Left behind
    it would be the same finding at smaller scale."""
    import robothor.config as config_module

    assert not hasattr(config_module, "required_env_checks")
