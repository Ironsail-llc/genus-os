"""Skill write-origin provenance (Rip 4).

Ported from Hermes Agent ``tools/skill_provenance.py``. A ContextVar
that distinguishes foreground user-directed skill writes from
background-review-fork autonomous writes. The Rip 5 curator only
consolidates / archives skills with ``is_agent_created=True`` —
without this signal, the curator would happily prune skills the user
hand-authored.

Usage::

    from robothor.engine.skill_provenance import (
        set_current_write_origin,
        reset_current_write_origin,
        get_current_write_origin,
        is_background_review,
        BACKGROUND_REVIEW,
    )

    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        await spawn_review_fork(...)
    finally:
        reset_current_write_origin(token)

Inside ``_create_skill`` the handler reads
``get_current_write_origin()`` and tags ``meta.is_agent_created=True``
when the context is ``BACKGROUND_REVIEW``. Foreground / cron /
sub-agent calls default to ``"foreground"`` and stay user-owned.
"""

from __future__ import annotations

import contextvars

_write_origin: contextvars.ContextVar[str] = contextvars.ContextVar(
    "skill_write_origin",
    default="foreground",
)

# Sentinel for writes coming from the background review fork.
BACKGROUND_REVIEW = "background_review"

# Sentinel for writes from the slow consolidation curator (Rip 5).
CURATOR = "curator"


def is_agent_authored_origin(origin: str | None = None) -> bool:
    """True iff the write origin is an autonomous fork (review OR curator).

    Both the per-turn review fork and the curator are the agent acting on its
    own skill library — their writes are curator-eligible. Foreground (operator-
    driven) writes are not.
    """
    o = origin if origin is not None else get_current_write_origin()
    return o in (BACKGROUND_REVIEW, CURATOR)


def set_current_write_origin(origin: str) -> contextvars.Token[str]:
    """Bind the active write origin to the current asyncio context.

    Returns a Token that the caller must pass to
    :func:`reset_current_write_origin` in a finally block. The
    ContextVar is per-Task, so a value set inside one fork does not
    leak into the parent task or sibling forks.
    """
    return _write_origin.set(origin or "foreground")


def reset_current_write_origin(token: contextvars.Token[str]) -> None:
    """Restore the prior write origin. Pair every set with a reset."""
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    """Return the active write origin (default ``"foreground"``)."""
    return _write_origin.get()


def is_background_review() -> bool:
    """True iff the current write origin is the background-review fork."""
    return get_current_write_origin() == BACKGROUND_REVIEW
