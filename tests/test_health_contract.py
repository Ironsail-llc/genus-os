"""What `/ready` tells ArgoCD, and it had no test of its own.

`health_contract.readiness_response` decides whether this instance reports
itself ready. A 200 lets a rollout proceed and keeps a load balancer sending
traffic; a 503 stops both. It is 37 statements, it was at 59% coverage, and
no test referenced it.

That matters here specifically. Earlier in this same session the fleet guard
`check_fleet` — which rides on this endpoint — was found configured at
permissive defaults, so `/ready` answered 200 through an outage that had
deleted the primary agent's schedule. The endpoint's own logic was never the
thing under test, so nothing would have caught it returning the wrong code.

The properties worth pinning are the ones a rollout depends on: every check
runs, one failure is enough to degrade, a check that hangs does not hang the
probe, and a raising check is reported rather than propagated — a readiness
endpoint that 500s tells an orchestrator nothing it can act on.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.health_contract import liveness_response, readiness_response


async def _ok() -> str:
    return "ok"


async def _bad() -> str:
    return "error:down"


async def _raises() -> str:
    raise RuntimeError("connection refused")


async def _hangs() -> str:
    await asyncio.sleep(30)
    return "ok"


class TestLiveness:
    def test_it_never_checks_dependencies(self):
        body = liveness_response("genus", "1.0.0")
        assert body["status"] == "ok"
        assert "checks" not in body, "liveness must not depend on anything"

    def test_it_names_the_service_and_version(self):
        body = liveness_response("genus", "1.2.3")
        assert body["service"] == "genus" and body["version"] == "1.2.3"


class TestReadinessStatusCode:
    @pytest.mark.asyncio
    async def test_all_ok_is_200(self):
        body, code = await readiness_response("genus", "1.0", {"db": _ok, "redis": _ok})
        assert code == 200 and body["status"] == "ok"

    @pytest.mark.asyncio
    async def test_one_failure_degrades_the_whole_probe(self):
        """A rollout must stop on a single unreachable dependency."""
        body, code = await readiness_response("genus", "1.0", {"db": _ok, "redis": _bad})
        assert code == 503
        assert body["checks"]["db"] == "ok"
        assert body["checks"]["redis"].startswith("error")

    @pytest.mark.asyncio
    async def test_no_checks_is_ready(self):
        _, code = await readiness_response("genus", "1.0", {})
        assert code == 200


class TestAFailingCheckIsReportedNotRaised:
    @pytest.mark.asyncio
    async def test_a_raising_check_becomes_an_error_string(self):
        """A 500 from /ready tells an orchestrator nothing it can act on."""
        body, code = await readiness_response("genus", "1.0", {"db": _raises})
        assert code == 503
        assert "connection refused" in body["checks"]["db"]

    @pytest.mark.asyncio
    async def test_a_hanging_check_does_not_hang_the_probe(self):
        """A probe that never answers reads as a dead process."""
        body, code = await asyncio.wait_for(
            readiness_response("genus", "1.0", {"slow": _hangs}), timeout=10
        )
        assert code == 503
        assert body["checks"]["slow"] == "error:timeout"

    @pytest.mark.asyncio
    async def test_one_hanging_check_does_not_hide_the_others(self):
        body, _ = await asyncio.wait_for(
            readiness_response("genus", "1.0", {"slow": _hangs, "db": _ok}), timeout=10
        )
        assert body["checks"]["db"] == "ok"
        assert body["checks"]["slow"] == "error:timeout"


class TestTheShapeIsStable:
    @pytest.mark.asyncio
    async def test_every_documented_field_is_present(self):
        """Other services parse this; the shape is a contract, not a detail."""
        body, _ = await readiness_response("genus", "9.9.9", {"db": _ok})
        for field in ("status", "service", "version", "timestamp", "checks"):
            assert field in body, f"missing {field}"
        assert body["service"] == "genus" and body["version"] == "9.9.9"

    @pytest.mark.asyncio
    async def test_every_check_appears_in_the_result(self):
        names = {"db", "redis", "schedules", "disk"}
        body, _ = await readiness_response("genus", "1.0", dict.fromkeys(names, _ok))
        assert set(body["checks"]) == names


class TestWaitForReady:
    """Startup gating: the daemon waits on this before declaring itself up."""

    @pytest.mark.asyncio
    async def test_it_returns_true_as_soon_as_ready(self, monkeypatch):
        import robothor.health_contract as hc

        calls = {"n": 0}

        class _Resp:
            status_code = 200

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                calls["n"] += 1
                return _Resp()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        assert await hc.wait_for_ready("http://x", timeout=5) is True
        assert calls["n"] == 1, "it polled again after a 200"

    @pytest.mark.asyncio
    async def test_it_gives_up_at_the_deadline_rather_than_hanging(self, monkeypatch):
        """A startup gate that never returns is worse than one that fails."""
        import robothor.health_contract as hc

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                raise ConnectionError("refused")

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        result = await asyncio.wait_for(
            hc.wait_for_ready("http://x", timeout=0.3, backoff=0.05, max_backoff=0.1),
            timeout=5,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_a_connection_error_is_retried_not_raised(self, monkeypatch):
        """The service being down is the normal case during startup."""
        import robothor.health_contract as hc

        state = {"n": 0}

        class _Resp:
            def __init__(self, code):
                self.status_code = code

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                state["n"] += 1
                if state["n"] < 3:
                    raise ConnectionError("not up yet")
                return _Resp(200)

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        assert await hc.wait_for_ready("http://x", timeout=5, backoff=0.01) is True
        assert state["n"] == 3
