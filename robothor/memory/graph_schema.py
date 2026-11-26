"""Vocabulary of the memory graph: origins and their authority, scopes, edge
retirement reasons and entity-resolution methods.

Each list here is also a CHECK constraint in the memory graph foundation
migration (``crm/migrations/*_memory_graph_foundation.sql``); the test suite
reads the migration and fails if the two disagree.

Origin is *who asserted* a fact or an edge, ranked by how far it is trusted
when two claims disagree or a recall is filtered:

    operator_confirmed > user_stated > crm > tool > agent_generated > external > inferred

``inferred`` is for edges only (the system derived the edge itself). A row
written before origins existed has ``origin IS NULL``; for ranking it counts as
``agent_generated``, which is what almost every legacy row is. An unknown
string is treated the same way rather than trusted.
"""

from __future__ import annotations

from enum import StrEnum


class Origin(StrEnum):
    """Who asserted a fact or edge, most to least authoritative."""

    OPERATOR_CONFIRMED = "operator_confirmed"
    USER_STATED = "user_stated"
    CRM = "crm"
    TOOL = "tool"
    AGENT_GENERATED = "agent_generated"
    EXTERNAL = "external"
    INFERRED = "inferred"


#: Every origin, most authoritative first.
AUTHORITY_ORDER: tuple[str, ...] = tuple(o.value for o in Origin)

#: Origins a ``memory_facts`` row may carry (no ``inferred``).
FACT_ORIGINS: tuple[str, ...] = tuple(o for o in AUTHORITY_ORDER if o != Origin.INFERRED)

#: Origins a ``memory_relations`` row may carry.
RELATION_ORIGINS: tuple[str, ...] = AUTHORITY_ORDER

#: How a NULL (pre-origin) or unrecognised origin is ranked.
LEGACY_ORIGIN: str = Origin.AGENT_GENERATED.value

#: Origins whose rows may be recalled automatically, without a caller asking.
AUTO_RECALL_ORIGINS: frozenset[str] = frozenset(
    {Origin.OPERATOR_CONFIRMED.value, Origin.USER_STATED.value, Origin.CRM.value}
)

#: Why an edge stopped being believed (``memory_relations.retired_reason``):
#: it held and then ended, it was shown wrong, or its evidence moved to another
#: edge when two were merged.
RETIREMENT_REASONS: tuple[str, ...] = ("ended", "refuted", "evidence_merged")

#: Who a fact or entity is visible to (``scope_kind``). ``shared`` is the whole
#: tenant and has no ``scope_id``; ``user`` and ``agent`` name their owner in it.
SCOPE_KINDS: tuple[str, ...] = ("shared", "user", "agent")
DEFAULT_SCOPE_KIND: str = "shared"

#: How a surface form was resolved to an entity
#: (``memory_resolution_decisions.method``).
RESOLUTION_METHODS: tuple[str, ...] = (
    "strong_key",
    "full_name",
    "context",
    "llm",
    "exact",
    "operator",
    "backfill",
)

_RANK: dict[str, int] = {o: len(AUTHORITY_ORDER) - i for i, o in enumerate(AUTHORITY_ORDER)}


def normalize_origin(origin: str | None) -> str:
    """The origin a row is ranked as: itself if known, else ``agent_generated``."""
    if origin is not None and str(origin) in _RANK:
        return str(origin)
    return LEGACY_ORIGIN


def authority_rank(origin: str | None) -> int:
    """Higher is more authoritative. NULL or unknown ranks as ``agent_generated``."""
    return _RANK[normalize_origin(origin)]


def least_trusted(*origins: str | None) -> str:
    """The least authoritative of ``origins``, normalised (see :func:`normalize_origin`).

    A conclusion is only as trustworthy as its weakest support, so a derived
    row takes the origin this returns over everything it was derived from.
    """
    if not origins:
        raise ValueError("least_trusted() needs at least one origin")
    return min((normalize_origin(o) for o in origins), key=authority_rank)


def is_trusted_for_auto_recall(origin: str | None) -> bool:
    """Whether a row of this origin may be recalled without being asked for.

    Only what the operator confirmed, what a user stated, and what the CRM
    holds. NULL and unknown origins are not trusted.
    """
    return origin is not None and str(origin) in AUTO_RECALL_ORIGINS
