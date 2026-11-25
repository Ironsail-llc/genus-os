"""Who may read a memory row: one rule for every reader of ``memory_facts``.

The write path stores a row's owner in ``scope_kind`` / ``scope_id``:

* ``shared`` (``scope_id`` NULL): every reader in the tenant;
* ``user``: one person, ``person:{crm person id}``, or one member,
  ``user:{user id}`` (the ids ``segments.conversation_scope`` gives);
* ``agent``: one agent, ``agent:{agent id}``.

A reader is the list of those ids it owns (:func:`reader_scope_ids`). It sees
shared rows plus rows whose ``scope_id`` is one of its ids; with no ids, shared
rows only.

The reader is the person a run serves in their own conversation, decided by the
run itself (``robothor/engine/private_recall.py``, ``ToolContext.private_reader``),
the same reader conversation segments use. A run's identity or principal alone
is not a reader: a scheduled, workspace or sub-agent run can execute as a
member. A restricted data scope only narrows it (:func:`served_reader`): the
served person is kept when it is the scope's person, and the scope never
supplies a reader of its own.

``memory_activate`` (the graph walk) takes one reader id and reads user-scoped
evidence only; :func:`graph_reader` picks it. Every SQL reader that returns
fact text to a caller uses :func:`fact_visibility_sql`; a reader that derives
shared rows from facts (consolidation aside, which keeps scopes apart) reads
shared rows only.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: The predicate a reader with no ids gets, and every derivation that writes
#: shared rows: shared facts only.
SHARED_ONLY_SQL = "scope_kind = 'shared'"

_PREFIXES = ("person:", "user:", "agent:")


def reader_scope_ids(
    *, person_id: Any = None, user_id: Any = None, agent_id: Any = None
) -> list[str]:
    """The ``scope_id`` values a reader owns, in the write path's id space."""
    out: list[str] = []
    if person_id:
        out.append(f"person:{person_id}")
    if user_id:
        out.append(f"user:{user_id}")
    if agent_id:
        out.append(f"agent:{agent_id}")
    return out


def normalize_reader(value: Any) -> str | None:
    """A reader id in the write path's id space; a bare UUID is a CRM person."""
    if not value:
        return None
    text = str(value)
    if text.startswith(_PREFIXES):
        return text
    try:
        return f"person:{uuid.UUID(text)}"
    except ValueError:
        return None


def served_reader(scope: Any, person_id: Any, user_id: Any) -> tuple[Any, Any]:
    """``(person, user)`` of the served reader after a restricted scope narrows it.

    The rule ``search_facts`` applies to segments since
    ``robothor/engine/private_recall.py`` decides the reader: unrestricted, as
    given; restricted, nothing unless the served person is the scope's person,
    and then that person and the served user, which the caller reads by only
    once ``segments.user_belongs_to_person`` holds. A scope never adds a
    reader the run does not serve.
    """
    if scope is None or not getattr(scope, "restricted", False):
        return person_id or None, user_id or None
    scoped = getattr(scope, "person_id", None)
    if not person_id or not scoped or str(person_id) != str(scoped):
        return None, None
    return person_id, user_id or None


def readers_for(
    *,
    scope: Any = None,
    person_id: Any = None,
    user_id: Any = None,
    agent_id: Any = None,
    extra: Any = None,
) -> list[str]:
    """The reader ids of one call. A restricted ``scope`` only narrows them."""
    restricted = scope is not None and getattr(scope, "restricted", False)
    if restricted:
        person_id, user_id = served_reader(scope, person_id, user_id)
        agent_id = None
    out = reader_scope_ids(person_id=person_id, user_id=user_id, agent_id=agent_id)
    normalized = normalize_reader(extra)
    if restricted and normalized != normalize_reader(getattr(scope, "person_id", None)):
        normalized = None
    if normalized and normalized not in out:
        out.append(normalized)
    return out


def graph_reader(readers: Iterable[str]) -> str | None:
    """The one reader id ``memory_activate`` takes: the person, else the user."""
    readers = list(readers)
    for prefix in ("person:", "user:"):
        for r in readers:
            if r.startswith(prefix):
                return r
    return None


def fact_visibility_sql(readers: Sequence[str], *, alias: str = "") -> tuple[str, tuple[Any, ...]]:
    """``(predicate, params)``: shared rows, and rows scoped to one of ``readers``."""
    p = f"{alias}." if alias else ""
    if not readers:
        return f"{p}{SHARED_ONLY_SQL}", ()
    return (
        f"({p}scope_kind = 'shared' OR ({p}scope_kind IN ('user', 'agent') "
        f"AND {p}scope_id = ANY(%s::text[])))",
        (list(readers),),
    )


def shared_only_sql(alias: str = "") -> str:
    """Shared facts only, for a reader that derives rows every reader will see."""
    return f"{alias}.{SHARED_ONLY_SQL}" if alias else SHARED_ONLY_SQL


__all__ = [
    "SHARED_ONLY_SQL",
    "fact_visibility_sql",
    "graph_reader",
    "normalize_reader",
    "reader_scope_ids",
    "readers_for",
    "served_reader",
    "shared_only_sql",
]
