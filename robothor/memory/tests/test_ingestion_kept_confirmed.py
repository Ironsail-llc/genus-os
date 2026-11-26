"""The write path and an extraction the operator overruled.

When conflict resolution keeps an operator-confirmed fact, the new extraction
is stored inactive. It is not a processed fact: its mentions and relations must
not reach the graph (they are the relations the operator corrected) and it is
counted apart.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import robothor.memory.write_path as wp
from robothor.memory.graph_schema import Origin


@pytest.mark.asyncio
async def test_a_kept_confirmed_result_is_not_a_processed_fact() -> None:
    facts = [
        {"fact_text": "Bob leads the Helios project", "category": "personal"},
        {"fact_text": "Alice joined the Helios team", "category": "personal"},
    ]
    results = [
        {"action": "kept_confirmed", "new_id": 99, "existing_id": 7},
        {"action": "stored", "new_id": 100},
    ]
    graph = MagicMock(return_value={"mentions_written": 0, "relations_stored": 0})
    ctx = wp.WriteContext(
        tenant_id="test-tenant",
        source_channel="chat",
        content_type="conversation",
        origin=Origin.AGENT_GENERATED,
    )
    with (
        patch.object(wp, "resolve_and_store", new=AsyncMock(side_effect=results)),
        patch.object(wp, "_write_graph", new=graph),
        patch.object(wp, "_embed", new=AsyncMock(return_value=[[0.0], [0.0]])),
    ):
        out = await wp.write_facts(facts, source_content="some content", context=ctx)

    assert out["fact_ids"] == [100]
    assert out["facts_processed"] == 1
    assert out["facts_kept_confirmed"] == 1
    assert out["facts_skipped"] == 0
    # Only the processed fact enters the graph.
    assert [c.args[1] for c in graph.call_args_list] == [100]
