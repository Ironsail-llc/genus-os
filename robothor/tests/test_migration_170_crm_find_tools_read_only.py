"""Migration 170: read-only roles may use the two CRM ``find_*`` lookups.

Viewer and member sessions are read-only by pattern (``search_*``, ``get_*``,
``list_*`` allowed, everything else denied). ``find_company_by_identifier``
and ``find_person_by_identifier`` only read, but match none of those
patterns, so they would have been refused. The migration allows those two
exact names and nothing broader: a future ``find_or_create_*`` must not slip
through a ``find_*`` pattern.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MIGRATION = "170_crm_find_tools_read_only"
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"
FIND_TOOLS = ("find_company_by_identifier", "find_person_by_identifier")


def test_the_migration_is_in_the_manifest() -> None:
    entries = [line.strip() for line in MANIFEST.read_text().splitlines()]
    assert f"crm/{MIGRATION}.sql" in entries


def _decide(rows: list[tuple[str, str]], tool: str) -> str | None:
    """The engine's rule (robothor.engine.permissions): the most specific match wins."""
    matching = [(p, a) for p, a in rows if fnmatch.fnmatch(tool, p)]
    if not matching:
        return None
    return max(
        matching,
        key=lambda r: (r[0] == tool, sum(c not in "*?[]" for c in r[0]), r[1] == "deny"),
    )[1]


@pytest.mark.integration
@pytest.mark.parametrize("role", ["viewer", "member"])
def test_read_only_roles_may_find_but_not_write(scratch_db, role: str) -> None:
    db, _ = scratch_db(through=MIGRATION)
    cur = db.cursor()
    cur.execute(
        "SELECT tool_pattern, access FROM role_permissions "
        "WHERE tenant_id = '__default__' AND role = %s",
        (role,),
    )
    rows = cur.fetchall()
    for tool in FIND_TOOLS:
        assert _decide(rows, tool) == "allow", tool
    for tool in ("set_affiliation", "find_or_create_company"):
        assert _decide(rows, tool) == "deny", tool
    assert ("find_*", "allow") not in rows
