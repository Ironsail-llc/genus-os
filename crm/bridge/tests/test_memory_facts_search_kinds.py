"""The Memory page's fact search turns ``search_facts`` rows into FACT ids.

``search_facts`` can return rows that are not facts (conversation segments,
insights, episodes), each with an ``id`` from its own table. Read as fact ids,
a segment whose id is 412 would show — and offer to forget — fact 412, which
has nothing to do with the query. Only ``kind == "fact"`` rows are facts.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def ranked(monkeypatch):
    from routers import memory_facts

    from robothor.memory import facts

    calls: list[dict] = []

    async def search_facts(query, **kwargs):
        calls.append(kwargs)
        return [
            {"id": 412, "kind": "segment", "fact_text": "User: what is the locker code?"},
            {"id": 7, "kind": "fact", "fact_text": "The locker code changes quarterly."},
            {"id": 3, "kind": "insight", "insight_text": "Locker codes rotate."},
            {"id": 9, "kind": "fact", "fact_text": "The garage has 40 lockers."},
        ]

    monkeypatch.setattr(facts, "search_facts", search_facts)
    return memory_facts, calls


def test_only_fact_rows_become_fact_ids(ranked):
    memory_facts, _calls = ranked

    ids = memory_facts._ranked_by_search("locker code", "test-tenant", "true", 10)

    assert ids == [7, 9]


def test_the_page_does_not_ask_for_conversation_segments(ranked):
    memory_facts, calls = ranked

    memory_facts._ranked_by_search("locker code", "test-tenant", "true", 10)

    assert calls and calls[0].get("include_segments") is False


def test_the_page_searches_the_admin_erasure_view(ranked):
    """The admin forgets facts from this page, private ones included, so its
    search must see every owner scope, as its browse arm does."""
    memory_facts, calls = ranked

    memory_facts._ranked_by_search("locker code", "test-tenant", "true", 10)

    assert calls and calls[0].get("admin_erasure_view") is True
