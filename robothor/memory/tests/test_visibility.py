"""The one read-side scope rule for memory rows (robothor/memory/visibility.py)."""

from __future__ import annotations

import uuid

from robothor.memory import visibility as v

P = str(uuid.UUID(int=1))
U = "0c9d8e7f-6a5b-4c3d-9e2f-1a0b9c8d7e6f"


def test_reader_ids_are_the_ids_the_write_path_stores() -> None:
    """The write path stores ``person:{id}``, ``user:{uid}`` and ``agent:{id}``."""
    assert v.reader_scope_ids(person_id=P, user_id=U, agent_id="main") == [
        f"person:{P}",
        f"user:{U}",
        "agent:main",
    ]
    assert v.reader_scope_ids() == []
    assert v.reader_scope_ids(person_id="", user_id="") == []


def test_a_bare_person_id_reads_as_that_person() -> None:
    assert v.normalize_reader(P) == f"person:{P}"
    assert v.normalize_reader(f"person:{P}") == f"person:{P}"
    assert v.normalize_reader(f"user:{U}") == f"user:{U}"
    assert v.normalize_reader(None) is None
    assert v.normalize_reader("") is None


def test_the_graph_reader_is_the_person_first_then_the_user() -> None:
    assert v.graph_reader([f"user:{U}", f"person:{P}"]) == f"person:{P}"
    assert v.graph_reader([f"user:{U}", "agent:main"]) == f"user:{U}"
    assert v.graph_reader(["agent:main"]) is None
    assert v.graph_reader([]) is None


def test_with_no_reader_only_shared_rows_are_visible() -> None:
    sql, params = v.fact_visibility_sql([])
    assert sql == "scope_kind = 'shared'"
    assert params == ()


def test_a_reader_sees_shared_rows_and_rows_scoped_to_one_of_their_ids() -> None:
    sql, params = v.fact_visibility_sql([f"person:{P}"], alias="f")
    assert sql == (
        "(f.scope_kind = 'shared' OR (f.scope_kind IN ('user', 'agent') "
        "AND f.scope_id = ANY(%s::text[])))"
    )
    assert params == ([f"person:{P}"],)


def test_a_restricted_scope_reads_as_its_person_only() -> None:
    from robothor.identity.scope import DataScope

    scope = DataScope(tenant_id="test-tenant", person_id=P, restricted=True)
    assert v.readers_for(scope=scope, person_id="someone-else", user_id=U) == [f"person:{P}"]
    assert (
        v.readers_for(
            scope=DataScope(tenant_id="test-tenant", person_id=None, restricted=True), person_id=P
        )
        == []
    )
    assert v.readers_for(scope=None, person_id=P, user_id=U) == [f"person:{P}", f"user:{U}"]
    assert v.readers_for(scope=None, extra=P) == [f"person:{P}"]
