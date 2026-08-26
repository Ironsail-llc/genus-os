"""Entity recall must be expressible as an index lookup, and stay case-blind.

`_build_entity_context` matched entities with

    EXISTS (SELECT 1 FROM unnest(entities) e WHERE lower(e) = lower(%s))

which no index can serve. Every agent turn ran it up to five times, each a
full scan of the tenant's active facts: 13,155 shared buffers per call
(105 MB) against a 128 MB shared_buffers, so one query swept 82% of the pool,
five times per warmup, ~350 runs a day. Measured median 262 ms versus 2.7 ms
for every other warmup section.

The trap is the obvious "fix": dropping lower() and using the existing GIN on
the raw array. On this instance 646 of 14,714 distinct entity names collapse
under lower(), and that rewrite was measured losing 1,191 of 3,222 matched
rows — 37% of entity recall, silently. So the lowered form has to be what is
indexed, and these tests pin both halves.
"""

from __future__ import annotations

import inspect
import re

from robothor.engine import warmup


def _entity_sql() -> str:
    return inspect.getsource(warmup._build_entity_context)


def test_the_predicate_is_an_indexable_array_overlap():
    sql = _entity_sql()
    assert "lower_entities(entities)" in sql, "entity recall is not using the indexed expression"
    assert "&&" in sql, "expected an array-overlap predicate the GIN can serve"


def test_the_unindexable_unnest_predicate_is_gone():
    sql = _entity_sql()
    collapsed = re.sub(r"\s+", " ", sql)
    assert "FROM unnest(entities)" not in collapsed, "the full-scan predicate is still present"


def test_matching_is_still_case_insensitive():
    """Dropping lower() would silently lose 37% of matches on this data."""
    sql = _entity_sql()
    assert "lower(%s)" in sql or "lower(%(entity)s)" in sql, (
        "the probe value is no longer lowercased — matching became case-sensitive"
    )


def test_the_migration_is_registered_and_immutable():
    """A migration absent from the manifest never runs; a non-IMMUTABLE
    function cannot be used in a GIN expression index at all."""
    from pathlib import Path

    root = Path(warmup.__file__).resolve().parents[2]
    manifest = (root / "robothor" / "migrations" / "manifest.txt").read_text()
    assert "crm/109_facts_entities_lower_gin.sql" in manifest

    sql = (root / "crm" / "migrations" / "109_facts_entities_lower_gin.sql").read_text()
    assert "IMMUTABLE" in sql
    assert "gin (lower_entities(entities))" in sql
    assert "idx_facts_tenant_created" in sql
