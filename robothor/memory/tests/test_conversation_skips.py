"""Skipped conversation windows: listed, and retried only when asked."""

from __future__ import annotations

from typing import Any

import pytest

from robothor.memory.tests import test_write_path_integration as wp
from robothor.memory.tests.test_write_path_integration import (
    ALICE,
    TENANT,
    World,
    store_chat,
    turn_fact,
)

pytestmark = pytest.mark.integration

database = wp.database
world = wp.world


def _skip(world: World, key: str, start: int, end: int) -> None:
    world.db.q(
        "INSERT INTO ingested_items (tenant_id, source_name, item_id, content_hash) "
        "VALUES (%s, 'conversation_facts_skipped', %s, 'unparseable')",
        (TENANT, f"{key}#{start}-{end}"),
    )


def _skips(world: World) -> list[Any]:
    return world.db.q(
        "SELECT item_id FROM ingested_items WHERE source_name = 'conversation_facts_skipped'"
    )


@pytest.mark.asyncio
async def test_the_status_lists_skipped_windows_and_changes_nothing(world: World) -> None:
    from robothor.memory.conversation_skips import retry_skipped

    ids = store_chat(world.db, "telegram:4242", [("user", "I prefer tea."), ("assistant", "ok")])
    _skip(world, "telegram:4242", ids[0], ids[1])
    report = await retry_skipped(TENANT)
    assert report["dry_run"] is True and report["skipped"] == 1
    assert report["windows"] == [
        {
            "session_key": "telegram:4242",
            "start_ref": ids[0],
            "end_ref": ids[1],
            "reason": "unparseable",
        }
    ]
    assert world.db.facts() == [] and len(_skips(world)) == 1


@pytest.mark.asyncio
async def test_a_retried_window_is_extracted_and_no_longer_skipped(world: World) -> None:
    from robothor.memory.conversation_skips import retry_skipped

    world.model.on_turn("tea", turn_fact("The user prefers tea.", about_user=True))
    turns = [("user", "I prefer tea.", {"sender_telegram_id": "4242"}), ("assistant", "ok")]
    ids = store_chat(world.db, "telegram:4242", turns, person=ALICE)
    _skip(world, "telegram:4242", ids[0], ids[1])
    report = await retry_skipped(TENANT, apply=True)
    assert report["retried"] == 1 and report["still_failing"] == 0
    assert [f["fact_text"] for f in world.db.facts()] == ["The user prefers tea."]
    assert _skips(world) == []


@pytest.mark.asyncio
async def test_a_window_that_still_fails_stays_listed(world: World, monkeypatch: Any) -> None:
    from robothor.memory import generation
    from robothor.memory.conversation_skips import retry_skipped

    async def broken(*_a: Any, **_k: Any) -> str:
        return "not json"

    monkeypatch.setattr(generation, "generate", broken)
    ids = store_chat(world.db, "telegram:4242", [("user", "I prefer tea."), ("assistant", "ok")])
    _skip(world, "telegram:4242", ids[0], ids[1])
    report = await retry_skipped(TENANT, apply=True)
    assert report["retried"] == 0 and report["still_failing"] == 1
    assert len(_skips(world)) == 1


@pytest.mark.asyncio
async def test_a_retry_that_loses_its_lease_writes_nothing(world: World, monkeypatch: Any) -> None:
    from robothor.memory import conversation_ingest as ci
    from robothor.memory.conversation_skips import retry_skipped

    world.model.on_turn("tea", turn_fact("The user prefers tea.", about_user=True))
    turns = [("user", "I prefer tea.", {"sender_telegram_id": "4242"}), ("assistant", "ok")]
    ids = store_chat(world.db, "telegram:4242", turns, person=ALICE)
    _skip(world, "telegram:4242", ids[0], ids[1])
    renewed: list[str] = []

    def lost(key: str, tenant: str, token: str) -> bool:
        renewed.append(token)
        return False

    monkeypatch.setattr(ci, "_renew", lost)
    report = await retry_skipped(TENANT, apply=True)
    assert renewed and report["still_failing"] == 1
    assert world.db.facts() == [] and len(_skips(world)) == 1
