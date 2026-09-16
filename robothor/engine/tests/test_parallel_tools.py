"""Which of a turn's tool calls may run at the same time.

The policy is deliberately small and deliberately conservative: a call is
eligible for concurrency only when the platform has CLASSIFIED it read-only,
and the first call that is not ends grouping for the rest of the turn. These
tests pin both halves — that independent reads do group, and that nothing else
ever does.
"""

from __future__ import annotations

import pytest

from robothor.engine.parallel_tools import (
    MAX_PARALLEL_TOOL_CALLS,
    clamp_parallel_limit,
    is_parallel_safe,
    plan_batches,
)

READS = frozenset({"read_file", "web_fetch", "web_search", "list_directory"})


class TestClassification:
    def test_a_declared_read_only_tool_is_safe(self):
        assert is_parallel_safe("read_file", read_only=READS) is True

    def test_an_unclassified_tool_is_not(self):
        assert is_parallel_safe("write_file", read_only=READS) is False

    def test_exec_is_never_safe_even_if_something_classified_it_read_only(self):
        """The hard list is below the classification, not beside it."""
        assert is_parallel_safe("exec", read_only=READS | {"exec"}) is False

    @pytest.mark.parametrize("name", ["send_email", "send_notification", "send_file"])
    def test_send_prefixed_tools_are_never_safe(self, name):
        assert is_parallel_safe(name, read_only=READS | {name}) is False

    @pytest.mark.parametrize("name", ["spawn_agent", "spawn_agents"])
    def test_spawn_prefixed_tools_are_never_safe(self, name):
        assert is_parallel_safe(name, read_only=READS | {name}) is False

    def test_execute_code_is_never_safe(self):
        """It runs arbitrary code, which is every side effect at once."""
        assert is_parallel_safe("execute_code", read_only=READS | {"execute_code"}) is False

    def test_a_tool_under_a_human_approval_pattern_is_not_safe(self):
        assert is_parallel_safe("read_file", read_only=READS, human_approval=("read_*",)) is False

    def test_the_pattern_only_matches_what_it_names(self):
        assert is_parallel_safe("web_fetch", read_only=READS, human_approval=("read_*",)) is True


class TestBatchPlanning:
    def test_independent_reads_become_one_batch(self):
        assert plan_batches(["read_file", "web_fetch", "web_search"], read_only=READS, limit=4) == [
            [0, 1, 2]
        ]

    def test_the_limit_splits_a_long_run_of_reads(self):
        plan = plan_batches(["read_file"] * 5, read_only=READS, limit=2)
        assert plan == [[0, 1], [2, 3], [4]]

    def test_a_write_in_the_middle_serialises_everything_after_it(self):
        """The brief's rule, literally: a write forces the REMAINING calls
        sequential, in the model's order, and never runs beside anything."""
        plan = plan_batches(
            ["read_file", "web_fetch", "write_file", "read_file", "web_search"],
            read_only=READS,
            limit=4,
        )
        assert plan == [[0, 1], [2], [3], [4]]

    def test_a_write_first_means_nothing_is_parallel(self):
        plan = plan_batches(["write_file", "read_file", "read_file"], read_only=READS, limit=4)
        assert plan == [[0], [1], [2]]

    def test_every_call_appears_exactly_once_and_in_order(self):
        names = ["read_file", "exec", "web_fetch", "write_file", "read_file"]
        plan = plan_batches(names, read_only=READS, limit=4)
        flat = [i for batch in plan for i in batch]
        assert flat == list(range(len(names)))

    def test_a_limit_of_one_disables_grouping(self):
        plan = plan_batches(["read_file"] * 3, read_only=READS, limit=1)
        assert plan == [[0], [1], [2]]

    def test_an_empty_turn_plans_nothing(self):
        assert plan_batches([], read_only=READS, limit=4) == []


class TestLimit:
    def test_a_non_positive_setting_means_sequential(self):
        assert clamp_parallel_limit(0) == 1
        assert clamp_parallel_limit(-3) == 1

    def test_the_platform_ceiling_wins_over_an_operator_asking_for_more(self):
        assert clamp_parallel_limit(1000) == MAX_PARALLEL_TOOL_CALLS

    def test_a_sane_value_is_kept(self):
        assert clamp_parallel_limit(4) == 4
