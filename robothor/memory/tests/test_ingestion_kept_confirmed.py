"""Ingestion and an extraction the operator overruled.

When conflict resolution keeps an operator-confirmed fact, the new extraction
is stored inactive. It is not a processed fact: its entities must not reach the
graph (they are the relations the operator corrected) and it is counted apart.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import robothor.memory.ingestion as ing


@pytest.mark.asyncio
async def test_a_kept_confirmed_result_is_not_a_processed_fact(monkeypatch: Any) -> None:
    monkeypatch.delenv("MEMORY_VAULT_POPULATE", raising=False)
    facts = [
        {"fact_text": "Bob leads the Helios project", "category": "personal"},
        {"fact_text": "Alice joined the Helios team", "category": "personal"},
    ]
    results = [
        {"action": "kept_confirmed", "new_id": 99, "existing_id": 7},
        {"action": "stored", "new_id": 100},
    ]
    source_channel = MagicMock()
    entities = AsyncMock(return_value={"entities_stored": 1, "relations_stored": 0})
    with (
        patch.object(ing, "extract_facts", new=AsyncMock(return_value=facts)),
        patch.object(ing, "resolve_and_store", new=AsyncMock(side_effect=results)),
        patch.object(ing, "_set_source_channel", new=source_channel),
        patch.object(ing, "extract_entities_batch", new=entities),
        patch("robothor.memory.vault._vault_populate_enabled", return_value=False),
    ):
        out = await ing.ingest_content("some content", "chat", "conversation")

    assert out["fact_ids"] == [100]
    assert out["facts_processed"] == 1
    assert out["facts_kept_confirmed"] == 1
    assert out["facts_skipped"] == 0
    entities.assert_awaited_once()
    assert entities.call_args.args[0] == [100]
    # The overruled row still records where it came from.
    assert [c.args[0] for c in source_channel.call_args_list] == [99, 100]
