"""Tests for memory block seeds and seed_blocks_for_tenant."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from robothor.memory.blocks import DEFAULT_BLOCK_SEEDS, seed_blocks_for_tenant

EXPECTED_BLOCK_NAMES = {
    # Core system blocks
    "persona",
    "user_profile",
    "user_model",
    "working_context",
    "operational_findings",
    "contacts_summary",
    # Nightwatch
    "nightwatch_log",
    "performance_baselines",
    # Self-improvement agents
    "autoagent_learnings",
    "autoresearch_learnings",
    "architect_evolution_log",
    "architect_dispatch_ledger",
    # Curiosity Engine + Self Model
    "curiosity_engine_findings",
    "self_model",
}


def test_default_block_seeds_contains_all_expected():
    """All expected blocks are present in DEFAULT_BLOCK_SEEDS."""
    actual_names = {name for name, _, _ in DEFAULT_BLOCK_SEEDS}
    assert actual_names == EXPECTED_BLOCK_NAMES


def test_default_block_seeds_no_duplicates():
    """No duplicate block names in seeds."""
    names = [name for name, _, _ in DEFAULT_BLOCK_SEEDS]
    assert len(names) == len(set(names))


def test_default_block_seeds_valid_types():
    """All block types are valid."""
    valid_types = {"system", "persistent", "ephemeral"}
    for name, block_type, _ in DEFAULT_BLOCK_SEEDS:
        assert block_type in valid_types, f"Block '{name}' has invalid type '{block_type}'"


def test_default_block_seeds_positive_max_chars():
    """All max_chars values are positive integers."""
    for name, _, max_chars in DEFAULT_BLOCK_SEEDS:
        assert isinstance(max_chars, int), f"Block '{name}' max_chars is not int"
        assert max_chars > 0, f"Block '{name}' max_chars must be positive"


def test_nightwatch_blocks_are_persistent():
    """Nightwatch blocks should be persistent to survive maintenance."""
    nightwatch_blocks = {
        name: btype
        for name, btype, _ in DEFAULT_BLOCK_SEEDS
        if name in ("nightwatch_log", "performance_baselines")
    }
    for name, btype in nightwatch_blocks.items():
        assert btype == "persistent", f"'{name}' should be persistent, got '{btype}'"


def test_self_improvement_blocks_are_persistent():
    """Self-improvement agent blocks should be persistent."""
    si_blocks = {
        name: btype
        for name, btype, _ in DEFAULT_BLOCK_SEEDS
        if name.startswith(("autoagent_", "autoresearch_", "architect_"))
    }
    assert len(si_blocks) == 4
    for name, btype in si_blocks.items():
        assert btype == "persistent", f"'{name}' should be persistent, got '{btype}'"


@patch("robothor.memory.blocks.get_connection")
def test_seed_blocks_for_tenant_inserts_all_seeds(mock_get_conn):
    """seed_blocks_for_tenant executes INSERT for each seed."""
    mock_cur = MagicMock()
    mock_cur.rowcount = 1  # Simulate all inserts succeeding
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_get_conn.return_value.__enter__ = lambda s: mock_conn
    mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

    count = seed_blocks_for_tenant("test-tenant")

    assert count == len(DEFAULT_BLOCK_SEEDS)
    assert mock_cur.execute.call_count == len(DEFAULT_BLOCK_SEEDS)

    # Verify each seed was inserted with the correct tenant
    for call_args in mock_cur.execute.call_args_list:
        sql, params = call_args[0]
        assert "INSERT INTO agent_memory_blocks" in sql
        assert "ON CONFLICT" in sql
        assert params[0] == "test-tenant"
