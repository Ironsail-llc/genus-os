"""Conversation ingest: the stored conversation, extracted window by window, once.

No database and no model. The stored conversation is what the loader returns
(``_load_conversation`` is patched), the model answers with JSON, and the write
path is a mock that records what it was asked to store.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import robothor.memory.conversation_ingest as ci
from robothor.memory.conversation_facts import ConversationTurn
from robothor.memory.graph_schema import Origin

TENANT = "test-tenant"
ALICE = "6f1c1b7e-0000-4000-8000-000000000001"
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
    """The chat store and the conversation's watermark row, in memory.

    Every function the ingest calls on the database is one of these methods;
    each notes the thread it ran on, so a test can check none runs on the loop.
    """

    def __init__(self, turns: list[ConversationTurn], *, scope: tuple[str, str | None]) -> None:
        self.turns = turns
        self.scope = scope
        self.watermark = 0
        self.failures = 0
        self.failed_end: int | None = None
        self.recorded: list[tuple[str, list[int]]] = []
        self.skipped: list[tuple[int, int]] = []
        self.loads = 0
        self.claimed_by: str | None = None
        self.claims_refused = False
        self.lease_lost = False
        self.renewals = 0
        self.released = 0
        self.reowned: list[Any] = []
        self.fresh_from: int | None = None  # refs from here on are "just written"
        self.threads: set[str] = set()

    def _seen(self) -> None:
        self.threads.add(threading.current_thread().name)

    def claim(self, session_key: str, tenant_id: str) -> ci.Watermark | None:
        self._seen()
        assert tenant_id == TENANT
        if self.claims_refused or self.claimed_by is not None:
            return None
        self.claimed_by = "token-1"
        return ci.Watermark(
            through=self.watermark,
            failures=self.failures,
            token="token-1",
            failed_end=self.failed_end,
        )

    def advance(
        self, session_key: str, tenant_id: str, token: str, through: int, fact_ids: list[int]
    ) -> None:
        self._seen()
        assert token == self.claimed_by and tenant_id == TENANT
        self.watermark = through
        self.failures = 0
        self.failed_end = None
        self.recorded.append((str(through), list(fact_ids)))

    def fail(self, session_key: str, tenant_id: str, token: str, end: int) -> int:
        if self.failed_end is None:
            self.failed_end = end
        self._seen()
        assert token == self.claimed_by
        self.failures += 1
        return self.failures

    def renew(self, session_key: str, tenant_id: str, token: str) -> bool:
        self._seen()
        self.renewals += 1
        return self.claimed_by == token and not self.lease_lost

    def newest(self, session_key: str, tenant_id: str) -> int:
        self._seen()
        return max((t.ref for t in self.turns), default=0)

    def release(self, session_key: str, tenant_id: str, token: str) -> None:
        self._seen()
        assert token == self.claimed_by
        self.claimed_by = None
        self.released += 1

    def skip(self, session_key: str, tenant_id: str, start: int, end: int, reason: str) -> int:
        self._seen()
        self.skipped.append((start, end))
        return len(self.skipped)

    def reown(self, session_key: str, tenant_id: str, owner: Any) -> int:
        self._seen()
        self.reowned.append(owner)
        return 0

    def load(
        self,
        session_key: str,
        tenant_id: str,
        after_ref: int,
        limit: int,
        until: int | None = None,
        recent: Any = (),
    ) -> ci.StoredConversation | None:
        self._seen()
        self.loads += 1
        rows = [t for t in self.turns if t.ref > after_ref]
        new = rows[:limit]
        before = [t for t in self.turns if t.ref <= after_ref][-ci.CONTEXT_TURNS :]
        held = (
            min((t.ref for t in new if t.ref >= self.fresh_from), default=None)
            if self.fresh_from is not None
            else None
        )
        return ci.StoredConversation(
            turns=new,
            context=before,
            scope_kind=self.scope[0],
            scope_id=self.scope[1],
            is_room=":room:" in session_key,
            tail_text=self.turns[-1].text if self.turns else None,
            held_from=held,
            more=len(rows) > limit,
            refs=[t.ref for t in new],
        )


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
    scheduled: list[tuple[float, Any]] | None = None,
) -> tuple[dict[str, Any] | None, AsyncMock]:
    counter = iter(range(1000, 100000))
    write = write or AsyncMock(
        side_effect=lambda facts, **kw: {
            "fact_ids": [next(counter) for _ in facts],
            "facts_processed": len(facts),
            "facts_failed": 0,
        }
    )
    later = scheduled if scheduled is not None else []
    with (
        patch.object(ci, "_load_conversation", side_effect=store.load),
        patch.object(ci, "_claim", side_effect=store.claim),
        patch.object(ci, "_advance", side_effect=store.advance),
        patch.object(ci, "_fail", side_effect=store.fail),
        patch.object(ci, "_release", side_effect=store.release),
        patch.object(ci, "_renew", side_effect=store.renew, create=True),
        patch.object(ci, "_newest_ref", side_effect=store.newest, create=True),
        patch.object(ci, "_record_skip", side_effect=store.skip, create=True),
        patch.object(ci, "_reown", side_effect=store.reown, create=True),
        patch.object(ci, "_schedule", side_effect=lambda delay, fn: later.append((delay, fn))),
        patch("robothor.memory.generation.generate", new=model.generate),
        patch("robothor.memory.write_path.write_facts", new=write),
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


async def _run_scheduled(store: Store, model: Model, follow_up: Any) -> None:
    """Run a scheduled follow-up against the same fake store and model."""
    await _run_with(store, model, follow_up)


async def _run_with(store: Store, model: Model, fn: Any) -> None:
    counter = iter(range(200000, 300000))
    write = AsyncMock(
        side_effect=lambda facts, **kw: {
            "fact_ids": [next(counter) for _ in facts],
            "facts_processed": len(facts),
            "facts_failed": 0,
        }
    )
    with (
        patch.object(ci, "_load_conversation", side_effect=store.load),
        patch.object(ci, "_claim", side_effect=store.claim),
        patch.object(ci, "_advance", side_effect=store.advance),
        patch.object(ci, "_fail", side_effect=store.fail),
        patch.object(ci, "_release", side_effect=store.release),
        patch.object(ci, "_renew", side_effect=store.renew, create=True),
        patch.object(ci, "_newest_ref", side_effect=store.newest, create=True),
        patch.object(ci, "_record_skip", side_effect=store.skip),
        patch.object(ci, "_reown", side_effect=store.reown, create=True),
        patch.object(ci, "_schedule", side_effect=lambda delay, f: None),
        patch("robothor.memory.generation.generate", new=model.generate),
        patch("robothor.memory.write_path.write_facts", new=write),
    ):
        await fn()


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
async def test_a_long_backlog_drains_without_waiting_for_the_next_message() -> None:
    n = ci.WINDOW_MAX_TURNS * (ci.MAX_WINDOWS_PER_RUN + 1)
    store = Store(_stored(n), scope=("user", f"person:{ALICE}"))
    model, later = Model(), []
    await _run(store, model, scheduled=later)
    assert len(model.prompts) == ci.MAX_WINDOWS_PER_RUN
    assert len(later) == 1
    _delay, follow_up = later.pop()
    await _run_scheduled(store, model, follow_up)
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
    store = Store([], scope=("user", f"person:{ALICE}"))
    store.load = lambda *a, **kw: None  # type: ignore[method-assign]
    model = Model()
    out, _ = await _run(store, model, history=[{"role": "user", "content": "hi"}])
    assert out is None and model.prompts == []
    assert store.released == 1


@pytest.mark.asyncio
async def test_a_turn_not_stored_yet_is_picked_up_by_a_follow_up_not_a_wait() -> None:
    store = Store(_stored(2), scope=("user", f"person:{ALICE}"))
    history = _history(store) + [
        {"role": "user", "content": "turn number 3"},
        {"role": "assistant", "content": "turn number 4"},
    ]
    model, later = Model(), []
    await _run(store, model, history=history, scheduled=later)
    assert store.loads == 1  # no polling while holding the claim
    assert store.claimed_by is None
    assert len(later) == 1
    store.turns += _stored(2, first_ref=3)  # the save commits
    _delay, follow_up = later.pop()
    await _run_scheduled(store, model, follow_up)
    assert store.watermark == 4
    assert any("turn number 4" in p for p in model.prompts)


@pytest.mark.asyncio
async def test_a_failure_anywhere_is_logged_never_raised() -> None:
    with patch.object(ci, "_claim", side_effect=RuntimeError("db down")):
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
    # The stored source content is a reference to the turns, never their text.
    for call in write.call_args_list:
        assert call.kwargs["source_content"] == f"conversation:{KEY}#1-4"


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


# ═══ a conversation's facts are as private as the conversation ══════════


@pytest.mark.asyncio
async def test_a_persons_conversation_gives_facts_private_to_that_person() -> None:
    store = Store(_stored(4), scope=("user", f"person:{ALICE}"))
    _, write = await _run(store, Model())
    assert _written(write)
    for ctx, _f in _written(write):
        assert (ctx.scope_kind, ctx.scope_id) == ("user", f"person:{ALICE}")


@pytest.mark.asyncio
async def test_facts_about_the_speaker_are_anchored_to_their_person() -> None:
    store = Store(_stored(4), scope=("user", f"person:{ALICE}"))
    _, write = await _run(store, Model())
    for _ctx, f in _written(write):
        anchors = [m for m in f["mentions"] if m.get("crm_person_id")]
        if f["metadata"]["source"]["turn_refs"][0] % 2 == 1:  # a user turn: about the user
            assert anchors == [
                {"surface_form": "the user", "entity_type": "person", "crm_person_id": ALICE}
            ]
            assert f["metadata"]["subject"] == {"kind": "person", "person_id": ALICE}
        else:  # the assistant's turn is not about the user
            assert anchors == []
            assert "subject" not in f["metadata"]


@pytest.mark.asyncio
async def test_a_members_conversation_is_private_to_the_member_with_a_placeholder() -> None:
    store = Store(_stored(2), scope=("user", "user:member-1"))
    _, write = await _run(store, Model(), key="agent:main:user:member-1")
    (ctx, about), _other = _written(write)
    assert (ctx.scope_kind, ctx.scope_id) == ("user", "user:member-1")
    assert not any(m.get("crm_person_id") for m in about["mentions"])
    subject = about["metadata"]["subject"]
    assert subject["kind"] == "conversation_participant"
    assert subject["owner"] == "user:member-1"
    assert subject["conversation_ref"] == "agent:main:user:member-1"
    assert "participant" in subject["label"].lower()


@pytest.mark.asyncio
async def test_a_conversation_with_no_known_owner_is_kept_but_readable_by_no_one() -> None:
    from robothor.memory.visibility import reader_scope_ids

    store = Store(_stored(2), scope=("user", None))
    _, write = await _run(store, Model())
    written = _written(write)
    assert len(written) == 2  # not dropped
    for ctx, _f in written:
        assert ctx.scope_kind == "user"
        assert ctx.scope_id == f"conversation:{KEY}"
        # No reader's ids can ever equal it.
        assert ctx.scope_id not in reader_scope_ids(person_id=ALICE, user_id="member-1")
        assert not ctx.scope_id.startswith(("person:", "user:", "agent:"))
    about = next(f for _c, f in written if "number 1 " in f["fact_text"])
    assert about["metadata"]["subject"]["kind"] == "conversation_participant"


@pytest.mark.asyncio
async def test_a_workspace_room_is_not_extracted_into_tenant_wide_facts() -> None:
    store = Store(_stored(4), scope=("shared", None))
    model = Model()
    out, write = await _run(
        store, model, key="agent:main:room:3f2a0000-0000-4000-8000-000000000001"
    )
    assert model.prompts == []
    write.assert_not_awaited()
    assert out is None


@pytest.mark.asyncio
async def test_rows_with_no_turn_to_extract_still_move_the_watermark() -> None:
    store = Store([], scope=("user", f"person:{ALICE}"))
    real = store.load

    def only_system_rows(*a: Any, **kw: Any) -> Any:
        conv = real(*a, **kw)
        conv.scanned_through = 30  # thirty system rows, nothing to extract
        return conv

    store.load = only_system_rows  # type: ignore[method-assign]
    model = Model()
    out, _ = await _run(store, model, history=[])
    assert out is None and model.prompts == []
    assert store.watermark == 30  # the next run starts past them


@pytest.mark.asyncio
async def test_external_facts_are_written_external_and_never_anchored() -> None:
    store = Store(
        [_turn(1, "user", "Here's an email I got:\nFrom: Mallory\nturn number 1")],
        scope=("user", f"person:{ALICE}"),
    )
    _, write = await _run(store, Model())
    ((ctx, fact),) = _written(write)
    assert ctx.origin == Origin.EXTERNAL
    assert not any(m.get("crm_person_id") for m in fact["mentions"])
    assert "subject" not in fact["metadata"]


# ═══ the watermark row is the lock; nothing blocks the loop ══════════════


@pytest.mark.asyncio
async def test_every_database_call_runs_off_the_event_loop() -> None:
    store = Store(_stored(4), scope=("user", f"person:{ALICE}"))
    loop_thread = threading.current_thread().name
    await _run(store, Model())
    assert store.threads and loop_thread not in store.threads


@pytest.mark.asyncio
async def test_a_conversation_another_run_holds_is_left_to_it_then_retried() -> None:
    store = Store(_stored(4), scope=("user", f"person:{ALICE}"))
    store.claims_refused = True
    model, later = Model(), []
    out, _ = await _run(store, model, scheduled=later)
    assert out is None and model.prompts == [] and store.loads == 0
    assert len(later) == 1  # tried again shortly, so this exchange is not missed


@pytest.mark.asyncio
async def test_the_claim_is_released_even_when_extraction_fails() -> None:
    store = Store(_stored(4), scope=("user", f"person:{ALICE}"))
    await _run(store, Model(fail=True))
    assert store.released == 1 and store.claimed_by is None


@pytest.mark.asyncio
async def test_a_room_never_reads_the_store_or_the_watermark() -> None:
    store = Store(_stored(4), scope=("shared", None))
    await _run(store, Model(), key="agent:main:room:3f2a0000-0000-4000-8000-000000000001")
    assert store.threads == set()  # nothing on the database was called


# ═══ a window that cannot be extracted never blocks the conversation ═════


class PoisonModel(Model):
    """Cannot answer any window that holds the word POISON."""

    async def generate(self, prompt: str, **kw: Any) -> str:
        if "POISON" in prompt.split("New turns:", 1)[1]:
            self.prompts.append(prompt)
            return "not json"
        return await super().generate(prompt, **kw)


@pytest.mark.asyncio
async def test_a_window_that_keeps_failing_is_skipped_counted_and_logged(caplog: Any) -> None:
    store = Store(
        [_turn(1, "user", "POISON"), _turn(2, "assistant", "ok")],
        scope=("user", f"person:{ALICE}"),
    )
    model = PoisonModel()
    for _ in range(ci.MAX_WINDOW_FAILURES - 1):
        out, _ = await _run(store, model)
        assert out is None and store.watermark == 0
    assert store.failures == ci.MAX_WINDOW_FAILURES - 1  # one per failed run
    store.turns += [_turn(3, "user", "turn number 3"), _turn(4, "assistant", "turn number 4")]
    with caplog.at_level("WARNING"):
        out, write = await _run(store, model)
    assert store.skipped == [(1, 2)]
    assert out is not None and out["windows_skipped"] == 1
    assert any("skipp" in r.getMessage().lower() for r in caplog.records)
    assert store.watermark == 4  # the next turns were extracted after it
    assert _written(write)


@pytest.mark.asyncio
async def test_an_oversized_turn_is_clipped_for_extraction_with_a_marker() -> None:
    big = "word " * (ci.WINDOW_MAX_CHARS // 2)
    store = Store([_turn(1, "user", big), _turn(2, "assistant", "ok")], scope=("user", None))
    model = Model()
    await _run(store, model)
    assert model.prompts
    prompt = model.prompts[0]
    assert len(prompt) < ci.WINDOW_MAX_CHARS + 6000
    assert "characters not shown" in prompt


# ═══ turns committed out of order are not passed over ════════════════════


@pytest.mark.asyncio
async def test_turns_written_moments_ago_are_held_back_for_a_follow_up() -> None:
    store = Store(_stored(4), scope=("user", f"person:{ALICE}"))
    store.fresh_from = 3
    model, later = Model(), []
    await _run(store, model, scheduled=later)
    new = model.prompts[0].split("New turns:", 1)[1]
    assert "turn number 2" in new and "turn number 3" not in new
    assert store.watermark == 2
    assert len(later) == 1 and later[0][0] >= ci.HOLD_SECONDS


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([(1, "user", False), (2, "assistant", False)], None),
        ([(1, "user", False), (2, "assistant", True)], 1),  # an exchange stays together
        ([(1, "user", False), (2, "assistant", False), (3, "user", True)], 3),
        ([(1, "assistant", False), (2, "user", True), (3, "assistant", True)], 2),
    ],
)
def test_what_is_held_back(rows: list[tuple[int, str, bool]], expected: int | None) -> None:
    assert ci.held_from(rows) == expected


# ═══ an outage of the model server never skips real turns ═══════════════


class DownModel(Model):
    """The model server is unreachable, times out, or errors."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc
        self.down = True

    async def generate(self, prompt: str, **kw: Any) -> str:
        if self.down:
            self.prompts.append(prompt)
            raise self.exc
        return await super().generate(prompt, **kw)


@pytest.mark.parametrize(
    "exc", [ConnectionError("unreachable"), TimeoutError(), RuntimeError("503 from server")]
)
@pytest.mark.asyncio
async def test_a_model_outage_never_counts_toward_skipping(exc: Exception) -> None:
    store = Store(_stored(2), scope=("user", f"person:{ALICE}"))
    model = DownModel(exc)
    for _ in range(ci.MAX_WINDOW_FAILURES + 2):
        out, _ = await _run(store, model)
        assert out is None
    assert store.failures == 0 and store.skipped == [] and store.watermark == 0
    model.down = False
    out, write = await _run(store, model)
    assert store.watermark == 2 and _written(write)


@pytest.mark.asyncio
async def test_only_an_answer_the_model_gave_counts_toward_skipping() -> None:
    store = Store(_stored(2), scope=("user", f"person:{ALICE}"))
    await _run(store, Model(fail=True))  # it answered, but not in the shape asked for
    assert store.failures == 1


# ═══ the lease is renewed while a run works, and handed off cleanly ══════


@pytest.mark.asyncio
async def test_a_run_that_lost_its_lease_writes_nothing_more() -> None:
    store = Store(_stored(4), scope=("user", f"person:{ALICE}"))
    store.lease_lost = True  # another run took over while this one extracted
    out, write = await _run(store, Model())
    assert store.renewals >= 1
    write.assert_not_awaited()
    assert store.watermark == 0 and out is None


@pytest.mark.asyncio
async def test_turns_stored_while_a_run_works_are_handed_to_a_follow_up() -> None:
    store = Store(_stored(2), scope=("user", f"person:{ALICE}"))

    class Arriving(Model):
        async def generate(self, prompt: str, **kw: Any) -> str:
            if len(store.turns) == 2:
                store.turns += _stored(2, first_ref=3)  # the next exchange is saved meanwhile
            return await super().generate(prompt, **kw)

    later: list[Any] = []
    await _run(store, Arriving(), history=[], scheduled=later)
    assert store.watermark == 2 and len(later) == 1


def test_a_waiter_outlasts_a_lease() -> None:
    assert ci.CLAIM_RETRY_SECONDS * ci.MAX_FOLLOW_UPS > ci.CLAIM_LEASE_SECONDS


@pytest.mark.asyncio
async def test_held_turns_whose_follow_up_was_lost_are_taken_by_the_next_run() -> None:
    """A restart inside the hold drops the scheduled follow-up; nothing is lost,
    because the watermark never passed the held turns: the next run takes them."""
    store = Store(_stored(4), scope=("user", f"person:{ALICE}"))
    store.fresh_from = 3
    model = Model()
    await _run(store, model, scheduled=[])  # the follow-up is lost
    assert store.watermark == 2
    store.fresh_from = None  # later: the next exchange, or any run of this conversation
    await _run(store, model)
    assert store.watermark == 4
