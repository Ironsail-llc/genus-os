"""Compaction may not evict the statement of the task.

MEASURED 2026-09-16, ten benchmark runs on one model. The four runs whose
transcript begins with a compaction summary scored a mean of **0.016**; the six
that never compacted scored **0.450**. On the worst of them the required output
header appears three times in the competitor's context and **zero** times in
ours: the spec had been summarised away, and the agent then invented the
deliverable's shape from memory — its own columns, its own path, its own
section headings.

The competing scaffold compacts *harder* than we do and does not have this
problem, because of one constant: it pins the first three messages and never
evicts them, and under that harness the task statement is one of them.

Two defences here, in that order:

1. **Structural.** The first N messages are protected exactly as the system
   prompt already was. `ROBOTHOR_COMPACTION_PROTECT_FIRST_N`, default 3.
2. **Restorative.** What the task said about its OUTPUT — the exact path, the
   exact header, the exact fields, the exact headings — is re-rendered verbatim
   after every compaction, so the contract is in front of the model even on a
   run long enough to have compacted the rest of the spec twice over.
"""

from __future__ import annotations

import pytest

from robothor.engine.compaction import compact, protected_prefix_len

TASK = (
    "Compile the matching talks and save them to `/work/results/talks.tsv`.\n\n"
    "The TSV must use exactly the following header:\n\n"
    "```text\n"
    "Track\tTitle\tSpeakers\n"
    "```\n"
)


def _long_conversation(turns: int = 60) -> list[dict]:
    """A run long enough that every count-based pass has something to drop."""
    messages: list[dict] = [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": TASK},
    ]
    for i in range(turns):
        messages.append({"role": "assistant", "content": f"Working on step {i}."})
        messages.append({"role": "user", "content": "x" * 4000 + f" step {i}"})
    return messages


class TestTheProtectedPrefix:
    def test_the_default_is_three(self):
        """The same number the competing scaffold pins, for the same reason."""
        from robothor.settings import get_settings

        assert get_settings().providers.compaction_protect_first_n == 3

    def test_it_never_ends_on_an_assistant_that_called_a_tool(self):
        """Protecting a tool CALL while its RESULT is summarised away leaves a
        dangling call, which several providers reject outright. The prefix
        shrinks rather than orphaning the pair."""
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "r"},
        ]
        assert protected_prefix_len(messages, 3) == 2

    def test_it_is_bounded_by_the_conversation(self):
        assert protected_prefix_len([{"role": "system", "content": "s"}], 3) == 1
        assert protected_prefix_len([], 3) == 0

    def test_zero_keeps_the_system_prompt(self):
        """The system message was already unconditionally protected; turning
        this setting off must not take that away."""
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        assert protected_prefix_len(messages, 0) == 1


@pytest.mark.asyncio
class TestTheTaskSurvives:
    async def test_the_task_statement_survives_a_compaction(self, monkeypatch):
        monkeypatch.setattr("robothor.engine.compaction.extract_facts", _no_facts, raising=False)
        monkeypatch.setattr(
            "robothor.engine.compaction.summarize_segment", _fake_summary, raising=False
        )
        result = await compact(_long_conversation(), models=["x"], threshold=1, drain_to=1)
        assert result.passes_used >= 2, "the fixture must actually compact"
        assert any(TASK in str(m.get("content", "")) for m in result.messages), (
            "the task statement was summarised away — the failure that cost "
            "three benchmark tasks their entire score"
        )

    async def test_it_survives_compacting_twice(self, monkeypatch):
        """A long run compacts repeatedly. A defence that only holds for the
        first one is a defence that holds until the run gets interesting."""
        monkeypatch.setattr("robothor.engine.compaction.extract_facts", _no_facts, raising=False)
        monkeypatch.setattr(
            "robothor.engine.compaction.summarize_segment", _fake_summary, raising=False
        )
        once = await compact(_long_conversation(), models=["x"], threshold=1, drain_to=1)
        grown = list(once.messages)
        for i in range(40):
            grown.append({"role": "assistant", "content": f"more {i}"})
            grown.append({"role": "user", "content": "y" * 4000})
        twice = await compact(grown, models=["x"], threshold=1, drain_to=1)
        assert any(TASK in str(m.get("content", "")) for m in twice.messages)

    async def test_the_system_prompt_still_comes_first(self, monkeypatch):
        monkeypatch.setattr("robothor.engine.compaction.extract_facts", _no_facts, raising=False)
        monkeypatch.setattr(
            "robothor.engine.compaction.summarize_segment", _fake_summary, raising=False
        )
        result = await compact(_long_conversation(), models=["x"], threshold=1, drain_to=1)
        assert result.messages[0]["role"] == "system"


async def _no_facts(*_a, **_k):
    return []


async def _fake_summary(*_a, **_k):
    return "summary of some turns"


class TestTheStickyContract:
    def test_it_renders_every_item_the_task_stated(self):
        from robothor.engine.deliverable_contract import contract_sticky_block

        block = contract_sticky_block(TASK)
        assert block is not None
        assert "Track" in block and "Title" in block and "Speakers" in block
        assert "/work/results/talks.tsv" in block

    def test_it_is_silent_when_the_task_stated_no_contract(self):
        from robothor.engine.deliverable_contract import contract_sticky_block

        assert contract_sticky_block("Summarise the inbox.") is None
        assert contract_sticky_block(None) is None

    def test_it_is_bounded(self):
        """It is re-sent on every compaction of every long run. A block that
        grows with the task is a second context problem."""
        from robothor.engine.deliverable_contract import (
            STICKY_BLOCK_MAX_CHARS,
            contract_sticky_block,
        )

        spec = TASK + "\n".join(f"Also save results/f{i}.csv" for i in range(400))
        block = contract_sticky_block(spec)
        assert block is not None
        assert len(block) <= STICKY_BLOCK_MAX_CHARS

    def test_it_says_it_is_the_task_s_own_words(self):
        """An agent that cannot tell an engine reminder from the task itself
        will argue with one of them."""
        from robothor.engine.deliverable_contract import contract_sticky_block

        assert "task" in (contract_sticky_block(TASK) or "").lower()


@pytest.mark.asyncio
class TestTheContractIsRestoredAfterCompaction:
    """`compress_context` is where every caller compacts, so it is where the
    contract comes back."""

    @staticmethod
    def _messages():
        return [
            {"role": "system", "content": "You are an agent."},
            {"role": "user", "content": TASK},
            {"role": "assistant", "content": "done"},
        ]

    async def test_the_contract_is_appended(self, monkeypatch):
        from robothor.engine import context
        from robothor.engine.deliverable_contract import STICKY_MARKER

        monkeypatch.setattr(
            "robothor.engine.feature_flags.deliverable_contract_mode", lambda: "enforce"
        )
        out = context._restore_output_contract(self._messages(), self._messages())
        assert STICKY_MARKER in out[-1]["content"]
        assert "Track Title Speakers" in out[-1]["content"]

    async def test_off_restores_nothing(self, monkeypatch):
        from robothor.engine import context

        monkeypatch.setattr(
            "robothor.engine.feature_flags.deliverable_contract_mode", lambda: "off"
        )
        assert context._restore_output_contract(self._messages(), self._messages()) == (
            self._messages()
        )

    async def test_it_does_not_stack_a_second_copy(self, monkeypatch):
        from robothor.engine import context
        from robothor.engine.deliverable_contract import STICKY_MARKER

        monkeypatch.setattr(
            "robothor.engine.feature_flags.deliverable_contract_mode", lambda: "enforce"
        )
        once = context._restore_output_contract(self._messages(), self._messages())
        twice = context._restore_output_contract(self._messages(), once)
        assert sum(STICKY_MARKER in str(m.get("content", "")) for m in twice) == 1

    async def test_a_task_with_no_contract_gets_nothing(self, monkeypatch):
        from robothor.engine import context

        monkeypatch.setattr(
            "robothor.engine.feature_flags.deliverable_contract_mode", lambda: "enforce"
        )
        plain = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "Summarise the inbox."},
        ]
        assert context._restore_output_contract(plain, plain) == plain
