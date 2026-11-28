"""The organisation watch's snapshot is engine state, not an agent's memory block.

``org_watch`` keeps what it last saw in the memory block
``org_watch_snapshot`` of the parent tenant (``robothor.tenancy.watch``). It
names child companies, agents and overdue tasks, and what is stored there
decides what the owner is told next. So no agent-facing surface may read,
write, append to or list it: not the ``memory_block_*`` tools, not
``append_to_block``, not deep reasoning's block reader, and not the warm-up's
memory-block section, whatever a manifest names. Only ``watch.py`` touches it.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.tenancy.watch import SNAPSHOT_BLOCK

PARENT = "acme-parent"
SPELLINGS = [SNAPSHOT_BLOCK, f" {SNAPSHOT_BLOCK} ", SNAPSHOT_BLOCK.upper()]


def _ctx() -> ToolContext:
    return ToolContext(agent_id="main", run_id="run-1", tenant_id=PARENT, user_role="member")


def _handler(name: str) -> Any:
    from robothor.engine.tools.handlers.memory import HANDLERS

    return HANDLERS[name]


@pytest.mark.parametrize("block", SPELLINGS)
def test_memory_block_read_refuses_the_snapshot(block):
    with patch("robothor.memory.blocks.read_block") as read:
        result = asyncio.run(_handler("memory_block_read")({"block_name": block}, _ctx()))

    read.assert_not_called()
    assert "error" in result
    assert "content" not in result


@pytest.mark.parametrize("block", SPELLINGS)
def test_memory_block_write_refuses_the_snapshot(block):
    with patch("robothor.memory.blocks.write_block") as write:
        result = asyncio.run(
            _handler("memory_block_write")({"block_name": block, "content": "{}"}, _ctx())
        )

    write.assert_not_called()
    assert "error" in result


@pytest.mark.parametrize("block", SPELLINGS)
def test_append_to_block_refuses_the_snapshot(block):
    with patch("robothor.crm.dal.append_to_block") as append:
        result = asyncio.run(
            _handler("append_to_block")({"block_name": block, "entry": "x"}, _ctx())
        )

    append.assert_not_called()
    assert "error" in result
    assert result.get("success") is not True


def test_memory_block_list_does_not_name_the_snapshot():
    listed = {
        "blocks": [
            {"name": "persona", "size": 10, "last_written_at": None},
            {"name": SNAPSHOT_BLOCK, "size": 900, "last_written_at": None},
        ]
    }
    with patch("robothor.memory.blocks.list_blocks", return_value=listed):
        result = asyncio.run(_handler("memory_block_list")({}, _ctx()))

    assert [b["name"] for b in result["blocks"]] == ["persona"]


def test_other_blocks_are_untouched():
    with patch("robothor.memory.blocks.read_block", return_value={"content": "hi"}) as read:
        result = asyncio.run(_handler("memory_block_read")({"block_name": "persona"}, _ctx()))

    read.assert_called_once()
    assert result == {"content": "hi"}


def test_warmup_never_loads_the_snapshot_even_when_a_manifest_names_it():
    from robothor.engine import warmup

    seen: list[list[str]] = []

    def read_blocks(names: list[str], tenant_id: str = "") -> dict[str, Any]:
        seen.append(list(names))
        return {n: {"content": f"content of {n}"} for n in names}

    with (
        patch("robothor.memory.blocks.read_blocks", side_effect=read_blocks),
        patch.object(warmup, "_defer_write", MagicMock()),
    ):
        section = warmup._build_memory_blocks_section([SNAPSHOT_BLOCK, "persona"], PARENT)

    assert all(SNAPSHOT_BLOCK not in names for names in seen)
    assert f"content of {SNAPSHOT_BLOCK}" not in section
    assert "content of persona" in section


def test_deep_reasoning_cannot_read_the_snapshot():
    from robothor.engine import rlm_tool

    with patch("robothor.memory.blocks.read_block", return_value={"content": "{}"}) as read:
        out = rlm_tool._make_memory_block_read_fn(tenant_id="test-tenant")(SNAPSHOT_BLOCK)
        preload = rlm_tool._load_context_source(
            {"type": "block", "block_name": SNAPSHOT_BLOCK}, "", tenant_id="test-tenant"
        )

    read.assert_not_called()
    assert "error" in out
    assert preload is None
