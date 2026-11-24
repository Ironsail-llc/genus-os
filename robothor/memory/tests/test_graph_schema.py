"""The graph schema's vocabulary: origins and their authority, scopes, retirement
reasons and resolution methods.

The same lists live twice — as Python constants the write and read paths use,
and as CHECK constraints in the migration that the database enforces. A value
added to one and not the other is either refused by Postgres at write time or
silently unrankable at read time, so the two are compared here by reading the
migration file itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from robothor.memory import graph_schema as gs

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"
_SUFFIX = "_memory_graph_foundation.sql"


def _migration_entry() -> str:
    entries = [
        line.strip() for line in MANIFEST.read_text().splitlines() if line.strip().endswith(_SUFFIX)
    ]
    assert len(entries) == 1, f"expected one *{_SUFFIX} in the manifest, found {entries}"
    return entries[0]


def _sql() -> str:
    source, filename = _migration_entry().split("/", 1)
    return (REPO / source / "migrations" / filename).read_text()


def _statements() -> str:
    """The SQL without its comments, so a note in the header cannot pass a check."""
    return "\n".join(
        line.split("--", 1)[0] for line in _sql().splitlines() if not line.lstrip().startswith("--")
    )


def _check_values(constraint: str) -> list[str]:
    match = re.search(
        rf"CONSTRAINT\s+{re.escape(constraint)}\s+CHECK\s*\(\s*\w+\s+IN\s*\(([^)]*)\)",
        _statements(),
        re.IGNORECASE,
    )
    assert match, f"no CHECK ... IN (...) constraint named {constraint} in the migration"
    return re.findall(r"'([^']*)'", match.group(1))


# ─── the migration ships and is shaped like every other ──────────────────


def test_the_migration_is_a_crm_migration_listed_in_the_manifest() -> None:
    source, filename = _migration_entry().split("/", 1)
    assert source == "crm"
    assert (REPO / "crm" / "migrations" / filename).is_file()


def test_the_migration_leaves_the_transaction_to_the_runner() -> None:
    body = _statements().upper()
    assert "CONCURRENTLY" not in body
    assert not re.search(r"^\s*(BEGIN|COMMIT)\s*;", body, re.MULTILINE)


def test_no_new_tenant_column_names_a_tenant() -> None:
    """A DEFAULT on tenant_id writes rows into somebody's tenant (migration 098)."""
    assert not re.search(r"tenant_id[^,\n]*DEFAULT", _statements(), re.IGNORECASE)


def test_every_statement_is_idempotent() -> None:
    body = " ".join(_statements().split()).upper()
    for verb in ("CREATE TABLE", "CREATE INDEX", "ADD COLUMN"):
        total = body.count(verb)
        guarded = body.count(f"{verb} IF NOT EXISTS")
        assert total == guarded, f"{verb}: {total - guarded} without IF NOT EXISTS"


# ─── Python constants == SQL CHECK lists ─────────────────────────────────


@pytest.mark.parametrize(
    ("constraint", "constant"),
    [
        ("memory_facts_origin_check", gs.FACT_ORIGINS),
        ("memory_relations_origin_check", gs.RELATION_ORIGINS),
        ("memory_relations_retired_reason_check", gs.RETIREMENT_REASONS),
        ("memory_facts_scope_kind_check", gs.SCOPE_KINDS),
        ("memory_entities_scope_kind_check", gs.SCOPE_KINDS),
        ("memory_resolution_decisions_method_check", gs.RESOLUTION_METHODS),
    ],
)
def test_python_constants_match_the_sql_check_lists(
    constraint: str, constant: tuple[str, ...]
) -> None:
    assert sorted(_check_values(constraint)) == sorted(constant)


def test_the_scope_default_in_sql_is_the_python_default() -> None:
    defaults = re.findall(
        r"scope_kind\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'([^']+)'", _statements(), re.IGNORECASE
    )
    assert defaults and set(defaults) == {gs.DEFAULT_SCOPE_KIND}


# ─── the vocabulary itself ───────────────────────────────────────────────


def test_the_authority_order_is_the_agreed_one() -> None:
    assert gs.AUTHORITY_ORDER == (
        "operator_confirmed",
        "user_stated",
        "crm",
        "tool",
        "agent_generated",
        "external",
        "inferred",
    )
    assert tuple(o.value for o in gs.Origin) == gs.AUTHORITY_ORDER


def test_inferred_is_an_edge_origin_only() -> None:
    assert "inferred" in gs.RELATION_ORIGINS
    assert "inferred" not in gs.FACT_ORIGINS
    assert (*gs.FACT_ORIGINS, "inferred") == gs.RELATION_ORIGINS


def test_vocabularies_are_the_agreed_ones() -> None:
    assert gs.RETIREMENT_REASONS == ("ended", "refuted", "evidence_merged")
    assert gs.SCOPE_KINDS == ("shared", "user", "agent")
    assert gs.DEFAULT_SCOPE_KIND == "shared"
    assert set(gs.RESOLUTION_METHODS) == {
        "strong_key",
        "full_name",
        "context",
        "llm",
        "exact",
        "operator",
        "backfill",
    }


# ─── ranking helpers ─────────────────────────────────────────────────────


def test_authority_rank_is_strictly_decreasing_along_the_order() -> None:
    ranks = [gs.authority_rank(o) for o in gs.AUTHORITY_ORDER]
    assert ranks == sorted(ranks, reverse=True)
    assert len(set(ranks)) == len(ranks)


@pytest.mark.parametrize("legacy", [None, "", "something_unknown", "OPERATOR_CONFIRMED"])
def test_a_null_or_unknown_origin_ranks_as_agent_generated(legacy: str | None) -> None:
    assert gs.authority_rank(legacy) == gs.authority_rank("agent_generated")
    assert gs.normalize_origin(legacy) == "agent_generated"


def test_enum_members_rank_like_their_values() -> None:
    for member in gs.Origin:
        assert gs.authority_rank(member) == gs.authority_rank(member.value)


def test_least_trusted_picks_the_lowest_authority() -> None:
    assert gs.least_trusted("operator_confirmed", "tool", "crm") == "tool"
    assert gs.least_trusted("user_stated", "inferred") == "inferred"
    assert gs.least_trusted("crm") == "crm"


def test_least_trusted_counts_a_legacy_origin_as_agent_generated() -> None:
    assert gs.least_trusted("operator_confirmed", None) == "agent_generated"
    assert gs.least_trusted(None, "external") == "external"
    assert gs.least_trusted("bogus") == "agent_generated"


def test_least_trusted_needs_at_least_one_origin() -> None:
    with pytest.raises(ValueError):
        gs.least_trusted()


@pytest.mark.parametrize(
    ("origin", "trusted"),
    [
        ("operator_confirmed", True),
        ("user_stated", True),
        ("crm", True),
        ("tool", False),
        ("agent_generated", False),
        ("external", False),
        ("inferred", False),
        (None, False),
        ("bogus", False),
    ],
)
def test_only_operator_user_and_crm_origins_are_trusted_for_auto_recall(
    origin: str | None, trusted: bool
) -> None:
    assert gs.is_trusted_for_auto_recall(origin) is trusted
