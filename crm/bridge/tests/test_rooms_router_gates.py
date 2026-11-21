"""The workspaces router's gates, without a database.

Every ``/api/rooms`` mutation is on the mutation-gate allowlist because its
gate is per room, not per tenant: each handler resolves the human caller and
asks ``robothor.rooms.access`` before touching anything. This pins that the
handlers do so, maps the rooms errors to their statuses, and checks that a
request with no verified human is refused before any data access.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _all_routes() -> list:
    """Every route of the assembled app, as ``test_mutations_are_gated`` walks it."""
    from bridge_service import app
    from fastapi.routing import iter_route_contexts

    return list(iter_route_contexts(app.routes))


#: The two gates a rooms handler may open with: a room action, or (for routes
#: that name no room yet) a human caller of the acting tenant.
ROOM_GATES = ("_guard(request", "_guard, request", "_human(request")


def _rooms_routes() -> list:
    return [r for r in _all_routes() if r.path == "/api/rooms" or r.path.startswith("/api/rooms/")]


def test_the_rooms_router_is_mounted() -> None:
    paths = {r.path for r in _rooms_routes()}
    assert "/api/rooms" in paths
    assert "/api/rooms/{room_id}/members/{user_id}" in paths
    assert "/api/rooms/from-task/{task_id}" in paths
    assert len(_rooms_routes()) >= 22


def test_every_rooms_route_opens_with_a_room_gate() -> None:
    ungated = [
        f"{sorted(r.methods)} {r.path}"
        for r in _rooms_routes()
        if not any(gate in inspect.getsource(r.endpoint) for gate in ROOM_GATES)
    ]
    assert not ungated, ungated


def test_every_rooms_mutation_is_audited() -> None:
    unaudited = [
        f"{sorted(r.methods)} {r.path}"
        for r in _rooms_routes()
        if r.methods & MUTATION_METHODS
        and not r.path.endswith("/read")
        and not any(a in inspect.getsource(r.endpoint) for a in ("_audit(", "_audit, request"))
    ]
    assert not unaudited, unaudited


@pytest.mark.parametrize(
    ("error", "status"),
    [
        ("RoomNotFound", 404),
        ("RoomForbidden", 403),
        ("RevisionConflict", 409),
        ("RoomBusy", 409),
        ("InvalidBlocks", 422),
    ],
)
def test_rooms_errors_map_to_statuses(error, status) -> None:
    import json

    from routers import rooms

    from robothor.rooms import errors

    exc = (
        errors.RevisionConflict(4)
        if error == "RevisionConflict"
        else errors.RoomBusy("Alice")
        if error == "RoomBusy"
        else getattr(errors, error)("nope")
    )
    response = rooms.room_error_response(None, exc)

    assert response.status_code == status
    detail = json.loads(response.body)["detail"]
    assert set(detail) >= {"error", "message", "field"}
    assert (
        detail["error"]
        == {
            "RoomNotFound": "not_found",
            "RoomForbidden": "forbidden",
            "RevisionConflict": "revision_conflict",
            "RoomBusy": "room_busy",
            "InvalidBlocks": "invalid_blocks",
        }[error]
    )
    if error == "RevisionConflict":
        assert detail["currentRev"] == 4
    if error == "RoomBusy":
        assert detail["answering"] == "Alice"


def test_a_request_without_a_verified_human_is_refused(controls_client_as_service, monkeypatch):
    """No room data access happens for an agent token."""
    from robothor.rooms import dal

    def boom(*a, **k):
        raise AssertionError("data access before the gate")

    monkeypatch.setattr(dal, "list_rooms_for_user", boom)
    monkeypatch.setattr(dal, "list_all_rooms", boom)
    response = controls_client_as_service.get("/api/rooms")
    assert response.status_code == 403
