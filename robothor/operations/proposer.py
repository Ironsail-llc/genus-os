"""Which run an action is proposed in, for whom, and from which workspace.

``Operations.propose`` records ``proposed_by_run_id``, ``proposed_for_user_id``
and ``room_id`` on the action from :func:`current_proposer`. The runner sets
it once per run (``robothor.engine.runner``), so every proposal reached from
a tool, a plugin or a library call inside that run is attributed without a
parameter threaded through each caller (the shape of ``run_context.py``'s
benchmark marker, for the same reason).

The rules, all applied here:

- ``run_id`` is always the run's id.
- ``user_id`` is the person whose interactive turn it was: set only for an
  interactive trigger (webchat, Telegram, Slack, IDE, a plugin channel) and
  only when the caller is a user account (a UUID). Scheduled, cron, workflow,
  hook, event, sub-agent and manual runs record nobody.
- ``room_id`` is the workspace the run is in: the room run carrier
  (``robothor.rooms.run_context``) when one is bound, else the room named by
  the session key of an interactive turn.

These columns are attribution. Member approval (``robothor.rooms.approvals``)
re-checks every one against the run's own ``agent_runs`` row before a
member's verdict counts.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING

from robothor.rooms.keys import canonical_uuid, parse_room_key

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "INTERACTIVE_TRIGGERS",
    "Proposer",
    "current_proposer",
    "mark_proposer_run",
    "proposer_for_run",
    "proposer_scope",
    "reset_proposer",
    "set_proposer",
]

#: Triggers where a person is on the other end of the turn.
INTERACTIVE_TRIGGERS = frozenset({"webchat", "telegram", "slack", "ide", "channel"})


@dataclass(frozen=True)
class Proposer:
    """Identifiers only."""

    run_id: str | None
    user_id: str | None
    room_id: str | None


_CURRENT: ContextVar[Proposer | None] = ContextVar("robothor_action_proposer", default=None)


def current_proposer() -> Proposer | None:
    return _CURRENT.get()


def set_proposer(proposer: Proposer | None) -> Token[Proposer | None]:
    """Bind ``proposer`` for the rest of this task; pass the token to ``reset_proposer``."""
    return _CURRENT.set(proposer)


def reset_proposer(token: Token[Proposer | None]) -> None:
    """Restore what was bound before ``token``'s ``set``. Never raises: a run's
    teardown must not fail on it (a token from another context is ignored)."""
    with suppress(ValueError, RuntimeError):
        _CURRENT.reset(token)


def mark_proposer_run(
    run_id: str, trigger_type: object, trigger_detail: str | None, user_id: str | None
) -> Token[Proposer | None]:
    """Bind this run's attribution for the rest of the run (the runner calls it
    once per run, for every run, so a nested run never inherits its parent's
    person) and return the token its teardown passes to ``reset_proposer``."""
    return set_proposer(
        proposer_for_run(
            run_id=run_id,
            trigger_type=str(getattr(trigger_type, "value", trigger_type) or ""),
            trigger_detail=trigger_detail,
            user_id=user_id,
        )
    )


@contextmanager
def proposer_scope(proposer: Proposer | None) -> Iterator[Proposer | None]:
    token = _CURRENT.set(proposer)
    try:
        yield proposer
    finally:
        _CURRENT.reset(token)


def proposer_for_run(
    *, run_id: str, trigger_type: str, trigger_detail: str | None, user_id: str | None
) -> Proposer:
    """The attribution a run's proposals carry (the rules are in the module docstring)."""
    from robothor.rooms.run_context import current_room_run

    trigger = str(getattr(trigger_type, "value", trigger_type) or "")
    interactive = trigger in INTERACTIVE_TRIGGERS
    person = canonical_uuid(user_id) if interactive else None
    room = None
    carrier = current_room_run()
    if carrier is not None:
        room = canonical_uuid(carrier.room_id)
    elif interactive and trigger_detail:
        parsed = parse_room_key(trigger_detail.partition(":")[2])
        room = parsed[1] if parsed else None
    return Proposer(run_id=run_id or None, user_id=person, room_id=room)
