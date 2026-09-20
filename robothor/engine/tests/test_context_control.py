from unittest.mock import AsyncMock, patch

from robothor.engine.compaction import CompactionResult, _find_safe_split_index, _recent_split
from robothor.engine.context import maybe_compress
from robothor.engine.context_control import ContextControl, control


def exchanges(count=30):
    messages = []
    for i in range(count):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [{"id": str(i), "function": {"name": "read", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": str(i), "content": "x" * 4000},
            ]
        )
    return messages


def test_consecutive_batches_are_separable():
    messages = exchanges()
    assert _find_safe_split_index(messages, 41) == 40
    assert _find_safe_split_index(messages, 40) == 40
    split = _recent_split(messages, 4000, 20)
    assert split >= 54
    assert messages[split]["role"] == "assistant"


async def test_no_progress_compaction_is_not_repeated_by_preflight():
    state = ContextControl()
    token = control.set(state)
    messages = [{"role": "system", "content": "x" * 4000}]
    result = CompactionResult(messages=messages, tokens_before=1000, tokens_after=1000)
    try:
        with patch("robothor.engine.compaction.compact", AsyncMock(return_value=result)) as compact:
            output = await maybe_compress(messages, ["test"], threshold=800)
            await maybe_compress(output, ["test"], threshold=800)
            assert compact.await_count == 1
            assert len(state.measurements) == 1
    finally:
        control.reset(token)


async def test_failed_summary_still_reclaims_complete_exchanges():
    state = ContextControl()
    token = control.set(state)
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "add sam"},
        {"role": "assistant", "content": "working"},
        *exchanges(),
    ]
    result = CompactionResult(messages=messages, tokens_before=42000, tokens_after=42000)
    try:
        with patch("robothor.engine.compaction.compact", AsyncMock(return_value=result)):
            output = await maybe_compress(messages, ["test"], threshold=8000)
        assert state.measurements[0]["tokens_after"] < 8000
        assert output[:3] == messages[:3]
        ids = {tc["id"] for m in output for tc in m.get("tool_calls", [])}
        assert all(m["tool_call_id"] in ids for m in output if m["role"] == "tool")
    finally:
        control.reset(token)


def test_active_request_is_retained_verbatim_once():
    from robothor.engine.compaction import _split_for_summary

    task = {"role": "developer", "content": "[ACTIVE REQUEST]\nOnly add the confirmed attendee."}
    head, retained, rest = _split_for_summary(
        [
            {"role": "system", "content": "Rules"},
            {"role": "user", "content": "Earlier task"},
            {"role": "assistant", "content": "Earlier answer"},
            task,
            *exchanges(3),
        ]
    )
    assert head.count(task) == 1
    assert task not in retained and task not in rest
