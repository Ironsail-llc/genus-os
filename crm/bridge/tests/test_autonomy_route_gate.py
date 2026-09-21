"""`/api/autonomy/*` exists only on an instance that offers the feature.

`ROBOTHOR_AUTONOMY_ENABLED` turned off the dashboard page, the system-prompt
paragraph and the browser tool wording, and its own description says "off
means absent". The bridge did not read it. The only gate on these routes was
`require_personal_owner`, which checks role and identity — so with the feature
off, any authenticated `owner|admin|member|user` could still POST an
enrollment or a grant. Those are the endpoints that store payment cards and
hand an agent spending authority: the two things an operator who never
switched the feature on would be most surprised to learn were reachable.

404 rather than 403, and for the same reason `/setup` answers 404 once an
owner exists: a capability this appliance does not offer is not a permission
the caller lacks.

The gate is applied where the router is mounted
(`bridge_service.include_router(..., dependencies=[...])`) rather than inside
the router, so the routes stay in the assembled app and
`test_mutations_are_gated.py` still sees them. A router that vanished would
make that file's per-route assertions vacuously green, which is the failure
mode it was written to catch.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from crm.bridge.autonomy_gate import require_feature_offered


@pytest.fixture
def offered(monkeypatch):
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_AUTONOMY_ENABLED", "true")
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def not_offered(monkeypatch):
    from robothor.settings import reset_settings

    monkeypatch.delenv("ROBOTHOR_AUTONOMY_ENABLED", raising=False)
    reset_settings()
    yield
    reset_settings()


class TestTheDependency:
    def test_it_refuses_when_the_instance_does_not_offer_the_feature(self, not_offered):
        with pytest.raises(HTTPException) as raised:
            require_feature_offered()
        assert raised.value.status_code == 404

    def test_it_passes_once_the_instance_opts_in(self, offered):
        assert require_feature_offered() is None

    def test_the_refusal_says_nothing_about_who_is_asking(self, not_offered):
        """A 404 that explained the caller's role would confirm the feature
        exists and is merely switched off, which is the thing "absent" means
        not to say."""
        with pytest.raises(HTTPException) as raised:
            require_feature_offered()
        assert "role" not in str(raised.value.detail).lower()
        assert "permission" not in str(raised.value.detail).lower()


class TestOverTheWire:
    """The dependency and the wiring are each pinned above; this is the two
    of them together, answering an actual request."""

    def _client(self):
        from types import SimpleNamespace

        from fastapi import Depends, FastAPI, Request
        from fastapi.testclient import TestClient

        from crm.bridge.routers import autonomy

        app = FastAPI()

        @app.middleware("http")
        async def auth(request: Request, call_next):
            request.state.auth = SimpleNamespace(
                tenant_id="t", actor_id="alice", role="owner", is_service=False
            )
            return await call_next(request)

        app.include_router(autonomy.router, dependencies=[Depends(require_feature_offered)])
        # `raise_server_exceptions=False` because this class asks one question:
        # does the gate answer 404, or does it let the request through. What
        # the handler then does with a database is a different test's business,
        # and the `test-bridge` lane has no database — so raising the handler's
        # psycopg2 error here made a passing gate look like a failing one.
        return TestClient(app, raise_server_exceptions=False)

    def test_an_authenticated_owner_still_gets_404_with_the_feature_off(self, not_offered):
        """Role and identity were the ONLY checks in front of this route, so
        with the feature off a member could enroll a payment card on an
        appliance whose operator had never switched personal automation on."""
        response = self._client().post(
            "/api/autonomy/resources", json={"kind": "credential", "label": "x"}
        )
        assert response.status_code == 404

    def test_the_same_request_gets_past_the_gate_once_offered(self, offered):
        response = self._client().post(
            "/api/autonomy/resources", json={"kind": "credential", "label": "x"}
        )
        assert response.status_code != 404, "the gate refused an instance that opted in"


class TestItIsActuallyWiredToTheRouter:
    """A dependency nobody applied is the defect this replaces — the previous
    round shipped `feature_offered()` whose only caller was eight lines below
    its own definition."""

    def _routes(self):
        """Flattened, the way ``test_mutations_are_gated`` flattens.

        FastAPI >= 0.139 leaves an included router as one lazy
        ``_IncludedRouter`` entry in ``app.routes``, so a naive loop sees four
        docs routes and concludes there is nothing to check — a guard that
        passes by inspecting nothing, which is the shape of defect this whole
        review round is about.
        """
        from bridge_service import app

        try:
            from fastapi.routing import iter_route_contexts

            routes = list(iter_route_contexts(app.routes))
        except ImportError:  # pragma: no cover — FastAPI < 0.139
            routes = list(app.routes)
        return [r for r in routes if str(getattr(r, "path", "")).startswith("/api/autonomy")]

    def test_the_autonomy_routes_are_still_mounted(self):
        assert self._routes(), "the router stopped contributing routes"

    def _gate_names(self, route):
        names = {
            getattr(getattr(d, "call", None), "__name__", "")
            for d in getattr(route, "dependencies", []) or []
        }
        dependant = getattr(route, "dependant", None)
        names |= {
            getattr(getattr(d, "call", None), "__name__", "")
            for d in getattr(dependant, "dependencies", []) or []
        }
        return names

    def test_every_autonomy_route_carries_the_gate(self):
        missing = [
            f"{sorted(r.methods or [])} {r.path}"
            for r in self._routes()
            if "require_feature_offered" not in self._gate_names(r)
        ]
        assert not missing, "ungated autonomy routes: " + ", ".join(missing)
