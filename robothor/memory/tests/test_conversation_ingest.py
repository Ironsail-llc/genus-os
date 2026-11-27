"""Conversation ingest: the stored conversation, extracted window by window, once.

No database and no model. The stored conversation is what the loader returns
(``_load_conversation`` is patched), the model answers with JSON, and the write
path is a mock that records what it was asked to store.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import robothor.memory.conversation_ingest as ci
from robothor.memory.conversation_facts import ConversationTurn
from robothor.memory.graph_schema import Origin

TENANT = "test-tenant"
KEY = "agent:main:telegram:4242"
T0 = datetime(2023, 5, 20, 9, 0, tzinfo=UTC)


def _turn(ref: int, role: str, text: str) -> ConversationTurn:
    return ConversationTurn(ref=ref, role=role, text=text, at=T0 + timedelta(minutes=ref))


def _stored(n: int, *, first_ref: int = 1) -> list[ConversationTurn]:
    return [
        _turn(first_ref + i, "user" if i % 2 == 0 else "assistant", f"turn number {first_ref + i}")
        for i in range(n)
    ]


class Store:
    """The chat store and the watermark, in memory."""

    def __init__(self, turns: list[ConversationTurn], *, scope: tuple[str, str | None]) -> None:
        self.turns = turns
        self.scope = scope
        self.watermark = 0
        self.recorded: list[tuple[str, list[int]]] = []
        self.loads = 0

    def load(
        self, session_key: str, tenant_id: str, after_ref: int, limit: int
    ) -> ci.StoredConversation | None:
        self.loads += 1
        new = [t for t in self.turns if t.ref > after_ref][:limit]
        before = [t for t in self.turns if t.ref <= after_ref][-ci.CONTEXT_TURNS :]
        return ci.StoredConversation(
            turns=new,
            context=before,
            scope_kind=self.scope[0],
            scope_id=self.scope[1],
            is_room=":room:" in session_key,
            tail_text=self.turns[-1].text if self.turns else None,
        )

    def count(self, source: str, item_id: str, tenant_id: str | None = None) -> int:
        return self.watermark

    def record(
        self,
        source: str,
        item_id: str,
        hash_val: str,
        fact_ids: list[int] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        assert tenant_id == TENANT
        self.watermark = int(hash_val)
        self.recorded.append((hash_val, list(fact_ids or [])))


class Model:
    """Answers each window with one fact per turn, citing it, by its speaker."""

    def __init__(self, fail: bool = False) -> None:
        self.prompts: list[str] = []
        self.fail = fail

    async def generate(self, prompt: str, **_kw: Any) -> str:
        self.prompts.append(prompt)
        if self.fail:
            return "{ not json"
        new = prompt[prompt.index("New turns:") :]
        facts = []
        for number, label, text in re.findall(
            r"^\[(\d+)\] .*? (User|Assistant)[^:]*: (.*)$", new, re.MULTILINE
        ):
            facts.append(
                {
                    "fact_text": f"The user mentioned {text} in this chat.",
                    "category": "personal",
                    "kind": "other",
                    "confidence": 0.9,
                    "event_time": None,
                    "about_user": label == "User",
                    "speaker": label.lower(),
                    "turns": [int(number)],
                    "mentions": [],
                    "relations": [],
                }
            )
        return json.dumps({"facts": facts})


def _history(store: Store) -> list[dict[str, Any]]:
    return [{"role": t.role, "content": t.text} for t in store.turns]


async def _run(
    store: Store,
    model: Model,
    *,
    key: str = KEY,
    write: AsyncMock | None = None,
    history: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, AsyncMock]:
    counter = iter(range(1000, 100000))
    write = write or AsyncMock(
        side_effect=lambda facts, **kw: {
            "fact_ids": [next(counter) for _ in facts],
            "facts_processed": len(facts),
            "facts_failed": 0,
        }
    )
    with (
        patch.object(ci, "_load_conversation", side_effect=store.load),
        patch.object(ci, "get_ingested_count", side_effect=store.count),
        patch.object(ci, "record_ingested", side_effect=store.record),
        patch("robothor.memory.generation.generate", new=model.generate),
        patch("robothor.memory.write_path.write_facts", new=write),
        patch.object(ci, "STORE_WAIT_SECONDS", 0.0),
    ):
        out = await ci.ingest_conversation_session(
            key,
            _history(store) if history is None else history,
            "main",
            "telegram",
            "run-1",
            TENANT,
        )
    return out, write


def _written(write: AsyncMock) -> list[tuple[Any, dict[str, Any]]]:
    return [(c.kwargs["context"], f) for c in write.call_args_list for f in c.args[0]]


# ═══ the exchange, one call per window ═══════════════════════════════════


@pytest.mark.asyncio
async def test_a_window_is_one_call_that_sees_both_speakers_in_order() -> None:
    store = Store(
        [
            _turn(1, "user", "I prefer dark roast."),
            _turn(2, "assistant", "Then try Sumatra or Kona."),
            _turn(3, "user", "I bought a grinder yesterday."),
            _turn(4, "assistant", "Burr grinders are best."),
        ],
        scope=("user", "person:6f1c1b7e-0000-4000-8000-000000000001"),
    )
    model = Model()
    out, _ = await _run(store, model)
    assert len(model.prompts) == 1
    p = model.prompts[0]
    assert (
        p.index("dark roast")
        < p.index("Sumatra")
        < p.index("grinder yesterday")
        < p.index("Burr grinders")
    )
    assert out is not None and out["llm_calls"] == 1


@pytest.mark.asyncio
async def test_each_speakers_facts_get_that_speakers_origin() -> None:
    store = Store(_stored(4), scope=("user", "person:6f1c1b7e-0000-4000-8000-000000000001"))
    _, write = await _run(store, Model())
    origins = {f["fact_text"]: ctx.origin for ctx, f in _written(write)}
    assert origins["The user mentioned turn number 1 in this chat."] == Origin.USER_STATED
    assert origins["The user mentioned turn number 2 in this chat."] == Origin.AGENT_GENERATED
    for ctx, _f in _written(write):
        assert ctx.tenant_id == TENANT
        assert ctx.conversation_ref == KEY


@pytest.mark.asyncio
async def test_a_one_exchange_session_is_extracted() -> None:
    store = Store(_stored(2), scope=("user", "person:6f1c1b7e-0000-4000-8000-000000000001"))
    model = Model()
    out, write = await _run(store, model)
    assert len(model.prompts) == 1
    assert out is not None and len(out["fact_ids"]) == 2


# ═══ each window once, the whole conversation ════════════════════════════


@pytest.mark.asyncio
async def test_turns_already_extracted_are_not_extracted_again() -> None:
    store = Store(_stored(4), scope=("user", "person:6f1c1b7e-0000-4000-8000-000000000001"))
    model = Model()
    await _run(store, model)
    assert store.watermark == 4
    await _run(store, model)  # nothing new
    assert len(model.prompts) == 1


@pytest.mark.asyncio
async def test_new_turns_are_extracted_with_the_earlier_ones_as_context_only() -> None:
    store = Store(_stored(4), scope=("user", "person:6f1c1b7e-0000-4000-8000-000000000001"))
    model = Model()
    await _run(store, model)
    store.turns += _stored(2, first_ref=5)
    _, write = await _run(store, model)
    second = model.prompts[1]
    new_part = second[second.index("New turns:") :]
    assert "turn number 5" in new_part and "turn number 6" in new_part
    assert "turn number 4" not in new_part
    assert "turn number 4" in second  # shown, read-only
    texts = [f["fact_text"] for _c, f in _written(write)]
    assert all("number 5" in t or "number 6" in t for t in texts)
    assert store.watermark == 6


@pytest.mark.asyncio
async def test_the_whole_conversation_is_covered_not_just_the_tail() -> None:
    n = ci.WINDOW_MAX_TURNS * 2 + 5
    store = Store(_stored(n), scope=("user", "person:6f1c1b7e-0000-4000-8000-000000000001"))
    model = Model()
    out, write = await _run(store, model)
    assert len(model.prompts) == 3  # 20 + 20 + 5
    refs = sorted(r for _c, f in _written(write) for r in f["metadata"]["source"]["turn_refs"])
    assert refs == list(range(1, n + 1))
    assert [int(h) for h, _ids in store.recorded] == [20, 40, 45]
    assert out is not None and out["turns_covered"] == n


@pytest.mark.asyncio
async def test_a_long_backlog_is_extracted_over_several_runs() -> None:
    n = ci.WINDOW_MAX_TURNS * (ci.MAX_WINDOWS_PER_RUN + 1)
    store = Store(_stored(n), scope=("user", "person:6f1c1b7e-0000-4000-8000-000000000001"))
    model = Model()
    await _run(store, model)
    assert len(model.prompts) == ci.MAX_WINDOWS_PER_RUN
    await _run(store, model)
    assert len(model.prompts) == ci.MAX_WINDOWS_PER_RUN + 1
    assert store.watermark == n


@pytest.mark.asyncio
async def test_a_failed_extraction_leaves_the_watermark_for_a_retry() -> None:
    store = Store(_stored(4), scope=("user", "person:6f1c1b7e-0000-4000-8000-000000000001"))
    out, write = await _run(store, Model(fail=True))
    assert out is None
    assert store.recorded == []
    write.assert_not_awaited()  # no raw transcript is stored either


@pytest.mark.asyncio
async def test_a_fact_that_could_not_be_stored_leaves_the_watermark() -> None:
    store = Store(_stored(4), scope=("user", "person:6f1c1b7e-0000-4000-8000-000000000001"))
    broken = AsyncMock(return_value={"fact_ids": [], "facts_failed": 1, "errors": ["db"]})
    out, _ = await _run(store, Model(), write=broken)
    assert out is None
    assert store.recorded == []


@pytest.mark.asyncio
async def test_a_session_nobody_stored_is_not_extracted() -> None:
    model = Model()
    with (
        patch.object(ci, "_load_conversation", return_value=None),
        patch.object(ci, "get_ingested_count", return_value=0),
        patch("robothor.memory.generation.generate", new=model.generate),
        patch.object(ci, "STORE_WAIT_SECONDS", 0.0),
    ):
        out = await ci.ingest_conversation_session(
            KEY, [{"role": "user", "content": "hi"}], "main", "telegram", "run-1", TENANT
        )
    assert out is None and model.prompts == []


@pytest.mark.asyncio
async def test_it_waits_for_the_turn_that_triggered_it_to_be_stored() -> None:
    store = Store(_stored(2), scope=("user", "person:6f1c1b7e-0000-4000-8000-000000000001"))
    history = _history(store) + [
        {"role": "user", "content": "turn number 3"},
        {"role": "assistant", "content": "turn number 4"},
    ]
    calls = {"n": 0}
    real = store.load

    def late(*a: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # the save commits while ingest waits
            store.turns += _stored(2, first_ref=3)
        return real(*a)

    store.load = late  # type: ignore[method-assign]
    model = Model()
    await _run(store, model, history=history)
    assert len(model.prompts) == 1
    assert "turn number 4" in model.prompts[0]
    assert store.watermark == 4


@pytest.mark.asyncio
async def test_a_failure_anywhere_is_logged_never_raised() -> None:
    with patch.object(ci, "get_ingested_count", side_effect=RuntimeError("db down")):
        out = await ci.ingest_conversation_session(
            KEY, [{"role": "user", "content": "hi"}], "main", "telegram", "run-1", TENANT
        )
    assert out is None


# ═══ the callers no longer skip short sessions ═══════════════════════════


@pytest.mark.parametrize("path", ["robothor/engine/chat.py", "robothor/engine/telegram.py"])
def test_the_callers_do_not_gate_ingest_on_history_length(path: str) -> None:
    root = Path(__file__).resolve().parents[3]
    src = (root / path).read_text()
    at = src.index("ingest_conversation_session(\n")
    guard = src[max(0, at - 700) : at]
    assert "len(session.history) >=" not in guard


# ═══ every fact is linked to its source turns ════════════════════════════


@pytest.mark.asyncio
async def test_each_fact_names_its_conversation_and_turn_range() -> None:
    store = Store(_stored(4), scope=("user", "person:6f1c1b7e-0000-4000-8000-000000000001"))
    _, write = await _run(store, Model())
    for ctx, fact in _written(write):
        source = fact["metadata"]["source"]
        assert source["kind"] == "conversation" and source["ref"] == KEY
        assert source["start_ref"] == min(source["turn_refs"])
        assert source["end_ref"] == max(source["turn_refs"])
        assert source["mentioned_at"].startswith("2023-05-20")
        assert ctx.metadata["session_key"] == KEY
    # The stored source content is the window's own turns, verbatim.
    call = write.call_args_list[0]
    assert all(f"turn number {i}" in call.kwargs["source_content"] for i in range(1, 5))


# ═══ the agent's own briefings (MEMORY_INGEST_SKIP_GENERATED) ════════════


def _briefing_store() -> Store:
    return Store(
        [
            _turn(1, "user", "morning"),
            _turn(2, "assistant", "Morning Briefing: a new login was detected"),
            _turn(3, "user", "thanks"),
        ],
        scope=("user", "person:6f1c1b7e-0000-4000-8000-000000000001"),
    )


@pytest.mark.asyncio
async def test_a_briefing_is_left_out_when_the_flag_is_on(monkeypatch: Any) -> None:
    monkeypatch.setenv("MEMORY_INGEST_SKIP_GENERATED", "1")
    store, model = _briefing_store(), Model()
    await _run(store, model)
    assert "Morning Briefing" not in model.prompts[0]
    assert "morning" in model.prompts[0] and "thanks" in model.prompts[0]
    assert store.watermark == 3  # the briefing is passed over, not retried


@pytest.mark.asyncio
async def test_a_briefing_is_extracted_by_default(monkeypatch: Any) -> None:
    monkeypatch.delenv("MEMORY_INGEST_SKIP_GENERATED", raising=False)
    store, model = _briefing_store(), Model()
    await _run(store, model)
    assert "Morning Briefing" in model.prompts[0]
