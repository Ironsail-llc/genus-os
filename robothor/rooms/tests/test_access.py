"""Room access rules: who may do what in a shared thread.

Pure functions, no database. The matrix is written out by hand rather than
derived from the code's own table, so a change to the rules has to change this
file too.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from robothor.rooms import access
from robothor.rooms.access import (
    ACTIONS,
    Actor,
    can,
    parse_room_key,
    require,
    resolve_room_key,
    room_access,
    room_session_key,
)
from robothor.rooms.errors import (
    InvalidBlocks,
    RevisionConflict,
    RoomBusy,
    RoomError,
    RoomForbidden,
    RoomNotFound,
)
from robothor.rooms.models import Room, RoomMember

TENANT = "test-tenant"
OTHER_TENANT = "test-tenant-other"
ROOM_ID = str(uuid.uuid4())
ALICE = str(uuid.uuid4())


def _room(**overrides: Any) -> Room:
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "id": ROOM_ID,
        "kind": "report",
        "title": "Weekly numbers",
        "agent_id": "main",
        "session_key": room_session_key("main", ROOM_ID),
        "created_by": ALICE,
    }
    fields.update(overrides)
    return Room(**fields)


def _member(assigned: bool) -> RoomMember:
    return RoomMember(tenant_id=TENANT, room_id=ROOM_ID, user_id=ALICE, assigned=assigned)


# ─── session key ─────────────────────────────────────────────────────────


def test_the_session_key_is_agent_first_then_the_room() -> None:
    assert room_session_key("main", ROOM_ID) == f"agent:main:room:{ROOM_ID}"


def test_parse_reads_back_what_the_writer_wrote() -> None:
    assert parse_room_key(room_session_key("helper", ROOM_ID)) == ("helper", ROOM_ID)


def test_parse_normalises_the_room_id() -> None:
    assert parse_room_key(f"agent:main:room:{ROOM_ID.upper()}") == ("main", ROOM_ID)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "agent:main:primary",
        f"agent:main:user:{ALICE}",
        f"agent::room:{ROOM_ID}",
        "agent:main:room:not-a-uuid",
        f"agent:main:room:{ROOM_ID}:extra",
        f"room:main:agent:{ROOM_ID}",
        f"bot:main:room:{ROOM_ID}",
        None,
        42,
    ],
)
def test_parse_refuses_anything_that_is_not_a_room_key(key: Any) -> None:
    assert parse_room_key(key) is None


@pytest.mark.parametrize(
    ("agent_id", "room_id"),
    [("", ROOM_ID), ("ma:in", ROOM_ID), ("main", "not-a-uuid"), ("main", "")],
)
def test_the_writer_refuses_parts_that_would_not_parse_back(agent_id: str, room_id: str) -> None:
    with pytest.raises(ValueError):
        room_session_key(agent_id, room_id)


# ─── the access matrix ───────────────────────────────────────────────────

#: tenant role -> (no membership, shared, assigned)
MATRIX: dict[str, tuple[str, str, str]] = {
    "owner": ("admin", "admin", "admin"),
    "admin": ("admin", "admin", "admin"),
    "member": ("none", "shared", "assigned"),
    "user": ("none", "shared", "assigned"),
    "viewer": ("none", "viewer", "viewer"),
    "service": ("none", "none", "none"),
    "guest": ("none", "none", "none"),
    "": ("none", "none", "none"),
}
MEMBERSHIPS = ("none", "shared", "assigned")


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("membership", MEMBERSHIPS)
@pytest.mark.parametrize("role", list(MATRIX))
def test_room_access_for_every_role_and_membership(
    role: str, membership: str, archived: bool
) -> None:
    room = _room(archived_at=datetime.now(UTC) if archived else None)
    row = None if membership == "none" else _member(membership == "assigned")
    actor = Actor(user_id=ALICE, role=role, tenant_id=TENANT, is_service=role == "service")
    assert room_access(actor, room, row) == MATRIX[role][MEMBERSHIPS.index(membership)]


@pytest.mark.parametrize("role", ["owner", "admin", "member"])
def test_a_service_identity_gets_nothing_whatever_role_it_carries(role: str) -> None:
    actor = Actor(user_id=ALICE, role=role, tenant_id=TENANT, is_service=True)
    assert room_access(actor, _room(), _member(True)) == "none"


@pytest.mark.parametrize("role", list(MATRIX))
def test_another_tenants_actor_gets_nothing(role: str) -> None:
    actor = Actor(user_id=ALICE, role=role, tenant_id=OTHER_TENANT)
    assert room_access(actor, _room(), _member(True)) == "none"


def test_a_member_row_of_another_user_or_room_is_ignored() -> None:
    actor = Actor(user_id=ALICE, role="member", tenant_id=TENANT)
    someone_else = RoomMember(
        tenant_id=TENANT, room_id=ROOM_ID, user_id=str(uuid.uuid4()), assigned=True
    )
    other_room = RoomMember(
        tenant_id=TENANT, room_id=str(uuid.uuid4()), user_id=ALICE, assigned=True
    )
    assert room_access(actor, _room(), someone_else) == "none"
    assert room_access(actor, _room(), other_room) == "none"


def test_actions_are_exactly_the_contracts() -> None:
    assert set(ACTIONS) == {
        "read",
        "post",
        "run",
        "request_period",
        "accept_task",
        "edit_definition",
        "share",
        "archive",
        "restore_revision",
        "duplicate",
        "reassign",
        "list_all",
    }


_VIEWER = {"read"}
_SHARED = _VIEWER | {"post", "run", "request_period", "accept_task", "duplicate"}
_ASSIGNED = _SHARED | {"edit_definition", "share", "archive", "restore_revision"}
_ADMIN = _ASSIGNED | {"reassign", "list_all"}
GRANTS: dict[str, set[str]] = {
    "none": set(),
    "viewer": _VIEWER,
    "shared": _SHARED,
    "assigned": _ASSIGNED,
    "admin": _ADMIN,
}
#: What an archived room still allows, to whoever holds the action anyway.
ARCHIVED = {"read", "archive", "duplicate", "list_all"}


@pytest.mark.parametrize("action", sorted(GRANTS["admin"]))
@pytest.mark.parametrize("level", list(GRANTS))
def test_can_for_every_access_level_and_action(level: str, action: str) -> None:
    assert can(level, action) is (action in GRANTS[level])  # type: ignore[arg-type]


@pytest.mark.parametrize("action", sorted(GRANTS["admin"]))
@pytest.mark.parametrize("level", list(GRANTS))
def test_an_archived_room_is_read_only_but_can_be_unarchived(level: str, action: str) -> None:
    expected = action in GRANTS[level] and action in ARCHIVED
    assert can(level, action, archived=True) is expected  # type: ignore[arg-type]


def test_can_refuses_an_unknown_action() -> None:
    with pytest.raises(ValueError):
        can("admin", "delete_everything")  # type: ignore[arg-type]


def test_can_treats_an_unknown_access_level_as_none() -> None:
    assert can("superuser", "read") is False  # type: ignore[arg-type]


# ─── require / resolve_room_key, against a fake data layer ───────────────


class _FakeDal:
    def __init__(self, room: Room | None, member: RoomMember | None) -> None:
        self.room, self.member = room, member
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def get_room(self, tenant_id: str, room_id: str) -> Room | None:
        self.calls.append(("get_room", (tenant_id, room_id)))
        if self.room is None or self.room.tenant_id != tenant_id or self.room.id != room_id:
            return None
        return self.room

    def get_member(self, tenant_id: str, room_id: str, user_id: str) -> RoomMember | None:
        self.calls.append(("get_member", (tenant_id, room_id, user_id)))
        m = self.member
        if m is None or (m.tenant_id, m.room_id, m.user_id) != (tenant_id, room_id, user_id):
            return None
        return m


@pytest.fixture
def fake_dal(monkeypatch):
    def install(room: Room | None, member: RoomMember | None = None) -> _FakeDal:
        fake = _FakeDal(room, member)
        monkeypatch.setattr(access.dal, "get_room", fake.get_room)
        monkeypatch.setattr(access.dal, "get_member", fake.get_member)
        return fake

    return install


def test_require_returns_the_room_when_the_action_is_allowed(fake_dal) -> None:
    fake_dal(_room(), _member(False))
    actor = Actor(user_id=ALICE, role="member", tenant_id=TENANT)
    assert require(actor, ROOM_ID, "post").id == ROOM_ID


def test_require_reads_only_the_actors_tenant(fake_dal) -> None:
    fake = fake_dal(_room(), _member(True))
    actor = Actor(user_id=ALICE, role="owner", tenant_id=OTHER_TENANT)
    with pytest.raises(RoomNotFound):
        require(actor, ROOM_ID, "read")
    assert fake.calls[0] == ("get_room", (OTHER_TENANT, ROOM_ID))


def test_require_says_not_found_when_the_room_is_missing(fake_dal) -> None:
    fake_dal(None)
    with pytest.raises(RoomNotFound):
        require(Actor(user_id=ALICE, role="owner", tenant_id=TENANT), ROOM_ID, "read")


def test_require_says_not_found_without_access_so_ids_cannot_be_probed(fake_dal) -> None:
    fake_dal(_room(), None)
    with pytest.raises(RoomNotFound):
        require(Actor(user_id=ALICE, role="member", tenant_id=TENANT), ROOM_ID, "read")


def test_require_says_forbidden_with_access_but_not_the_action(fake_dal) -> None:
    fake_dal(_room(), _member(False))
    with pytest.raises(RoomForbidden):
        require(Actor(user_id=ALICE, role="member", tenant_id=TENANT), ROOM_ID, "share")


def test_require_refuses_a_post_into_an_archived_room(fake_dal) -> None:
    fake_dal(_room(archived_at=datetime.now(UTC)), _member(True))
    actor = Actor(user_id=ALICE, role="member", tenant_id=TENANT)
    with pytest.raises(RoomForbidden):
        require(actor, ROOM_ID, "post")
    assert require(actor, ROOM_ID, "archive").id == ROOM_ID


def test_require_refuses_a_malformed_room_id_without_a_lookup(fake_dal) -> None:
    fake = fake_dal(_room())
    with pytest.raises(RoomNotFound):
        require(Actor(user_id=ALICE, role="owner", tenant_id=TENANT), "1; DROP", "read")
    assert fake.calls == []


def _auth(role: str = "member", tenant: str = TENANT, service: bool = False) -> Any:
    return SimpleNamespace(user_id=ALICE, tenant_id=tenant, role=role, is_service=service)


def test_actor_from_auth_carries_the_acting_tenant() -> None:
    actor = Actor.from_auth(_auth(role="admin", tenant=OTHER_TENANT, service=True))
    assert actor == Actor(user_id=ALICE, role="admin", tenant_id=OTHER_TENANT, is_service=True)


def test_resolve_is_none_for_a_key_that_is_not_a_room(fake_dal) -> None:
    fake = fake_dal(_room())
    assert resolve_room_key(_auth(), "agent:main:primary") is None
    assert fake.calls == []


def test_resolve_returns_the_rooms_own_key(fake_dal) -> None:
    fake_dal(_room(), _member(False))
    assert resolve_room_key(_auth(), room_session_key("main", ROOM_ID)) == _room().session_key


def test_resolve_refuses_a_key_naming_another_agent(fake_dal) -> None:
    fake_dal(_room(), _member(True))
    with pytest.raises(RoomNotFound):
        resolve_room_key(_auth(), room_session_key("helper", ROOM_ID))


def test_resolve_refuses_a_caller_without_access(fake_dal) -> None:
    fake_dal(_room(), None)
    with pytest.raises(RoomNotFound):
        resolve_room_key(_auth(), room_session_key("main", ROOM_ID))


def test_resolve_checks_the_action_it_is_asked_for(fake_dal) -> None:
    fake_dal(_room(), _member(False))
    key = room_session_key("main", ROOM_ID)
    assert resolve_room_key(_auth(role="viewer"), key) == key
    with pytest.raises(RoomForbidden):
        resolve_room_key(_auth(role="viewer"), key, action="post")


# ─── errors ──────────────────────────────────────────────────────────────


def test_every_error_is_a_room_error() -> None:
    for error in (RoomNotFound, RoomForbidden, RevisionConflict, RoomBusy, InvalidBlocks):
        assert issubclass(error, RoomError)


def test_a_revision_conflict_names_the_current_revision() -> None:
    assert RevisionConflict(current_rev=4).current_rev == 4


def test_room_busy_names_who_is_answering() -> None:
    assert RoomBusy(answering="Alice").answering == "Alice"


def test_a_room_carries_its_link() -> None:
    assert _room().link == f"/?v=chat&room={ROOM_ID}"
