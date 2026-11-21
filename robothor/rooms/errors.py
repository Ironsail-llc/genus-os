"""Errors the rooms layer raises. The bridge maps them to HTTP statuses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.rooms.tasks import TaskPerson


class RoomError(Exception):
    """Base class for every rooms error. ``code`` is what the bridge returns."""

    code = "room_error"


class RoomNotFound(RoomError):  # noqa: N818 - names fixed by the contract
    """No such room in the caller's tenant, or the caller has no access (404).

    The two are deliberately the same error, so a room id cannot be probed.
    """

    code = "not_found"


class RoomForbidden(RoomError):  # noqa: N818 - names fixed by the contract
    """The caller can see the room but may not take this action (403)."""

    code = "forbidden"


class RevisionConflict(RoomError):  # noqa: N818 - names fixed by the contract
    """The definition changed since the caller read it (409)."""

    def __init__(self, current_rev: int, message: str | None = None) -> None:
        self.current_rev = current_rev
        super().__init__(message or f"the room is at revision {current_rev}")


class RoomBusy(RoomError):  # noqa: N818 - names fixed by the contract
    """Another agent turn is running in the room (409 ``room_busy``)."""

    def __init__(self, answering: str = "", message: str | None = None) -> None:
        self.answering = answering
        super().__init__(message or f"the room is busy answering {answering or 'someone'}")


class InvalidBlocks(RoomError):  # noqa: N818 - names fixed by the contract
    """An edition's blocks are not a list of allowed components (422)."""


class TaskNotFound(RoomError):  # noqa: N818 - named like the contract's errors
    """No such task in the caller's tenant, or not linked to that room (404)."""

    code = "not_found"


class TaskAlreadyAccepted(RoomError):  # noqa: N818 - named like the contract's errors
    """Someone else has the task (409 ``already_accepted``); ``by`` says who."""

    code = "already_accepted"

    def __init__(self, by: TaskPerson | Any, message: str | None = None) -> None:
        self.by = by
        name = getattr(by, "display_name", "") or "someone else"
        super().__init__(message or f"{name} has already accepted this task")


class TaskAlreadyDismissed(RoomError):  # noqa: N818 - named like the contract's errors
    """The suggestion was dismissed; a task is decided once (409 ``already_dismissed``)."""

    code = "already_dismissed"
