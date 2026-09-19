from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from robothor.goals.tests.test_store import db, private_database  # noqa: F401


@pytest.fixture
def client(db, monkeypatch):  # noqa: F811
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "crm/bridge"))
    from routers.pursuit_goals import router

    app = FastAPI()

    @app.middleware("http")
    async def auth(request: Request, call_next):
        request.state.auth = SimpleNamespace(
            tenant_id=db,
            role=request.headers.get("x-test-role", "owner"),
            is_service=False,
            actor_id="operator:test",
        )
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


def test_create_read_pause_resume_and_version_conflict(client):
    response = client.post(
        "/api/goals", json={"objective": "Deliver report", "success_criteria": ["Delivered"]}
    )
    assert response.status_code == 200
    g = response.json()["goal"]
    assert client.get("/api/goals").json()["goals"][0]["id"] == g["id"]
    path = f"/api/goals/{g['id']}"
    paused = client.patch(path, json={"action": "pause", "version": g["version"]})
    assert paused.json()["goal"]["status"] == "paused"
    assert client.patch(path, json={"action": "resume", "version": g["version"]}).status_code == 409
    resumed = client.patch(
        path, json={"action": "resume", "version": paused.json()["goal"]["version"]}
    )
    assert resumed.json()["goal"]["status"] == "queued"
    assert client.patch("/api/goals/settings", json={"enabled": False}).status_code == 200
    assert client.get("/api/goals").json()["enabled"] is False


def test_member_and_invalid_contract_refused(client):
    assert client.get("/api/goals", headers={"x-test-role": "member"}).status_code == 403
    assert (
        client.post(
            "/api/goals", json={"objective": " ", "success_criteria": ["Delivered"]}
        ).status_code
        == 422
    )
    assert client.get("/api/goals/not-a-uuid").status_code == 422
