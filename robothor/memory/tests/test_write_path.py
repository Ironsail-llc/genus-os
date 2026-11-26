"""The one write path: extraction shape, origin, scope and hash, with the LLM mocked.

Everything here runs without a database or a model. The database half of the
story (evidence rows, mentions, edge origin, scope isolation, the routed
writers) is ``test_write_path_integration.py``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import jsonschema
import pytest

from robothor.memory.graph_schema import FACT_ORIGINS, Origin
from robothor.memory.ingestion import VALID_CHANNELS

ALICE = "6f1c1b7e-0000-4000-8000-000000000001"
BOB = "6f1c1b7e-0000-4000-8000-000000000002"
#: How a private scope names its owner (the conversation-segments convention).
ALICE_P = f"person:{ALICE}"
BOB_P = f"person:{BOB}"


# ═══ origin mapping ══════════════════════════════════════════════════════

#: What each ingestion channel means when the caller names no speaker. Inbound
#: channels carry whoever wrote to us, so they are external; channels that only
#: an agent or a tool of ours writes through are agent-generated; CRM is CRM.
EXPECTED_CHANNEL_ORIGIN = {
    "discord": Origin.EXTERNAL,
    "email": Origin.EXTERNAL,
    "cli": Origin.AGENT_GENERATED,
    "api": Origin.AGENT_GENERATED,
    "telegram": Origin.EXTERNAL,
    "gchat": Origin.EXTERNAL,
    "voice": Origin.EXTERNAL,
    "mcp": Origin.AGENT_GENERATED,
    "camera": Origin.EXTERNAL,
    "conversation": Origin.AGENT_GENERATED,
    "crm": Origin.CRM,
}


def test_the_mapping_table_covers_every_valid_channel() -> None:
    assert set(EXPECTED_CHANNEL_ORIGIN) == set(VALID_CHANNELS)


@pytest.mark.parametrize("channel", VALID_CHANNELS)
def test_every_valid_channel_maps_to_its_origin_with_no_speaker(channel: str) -> None:
    from robothor.memory.write_path import origin_for

    assert origin_for(channel, "note") == EXPECTED_CHANNEL_ORIGIN[channel]


@pytest.mark.parametrize("channel", VALID_CHANNELS)
def test_the_speaker_decides_on_every_channel(channel: str) -> None:
    from robothor.memory.write_path import origin_for

    assert origin_for(channel, "conversation", role="user") == Origin.USER_STATED
    assert origin_for(channel, "conversation", role="assistant") == Origin.AGENT_GENERATED
    assert origin_for(channel, "conversation", role="tool") == Origin.TOOL


@pytest.mark.parametrize(
    "content_type", ["email", "web", "webpage", "web_page", "document", "upload", "pdf"]
)
def test_external_content_is_external_whoever_relays_it(content_type: str) -> None:
    from robothor.memory.write_path import origin_for

    assert origin_for("api", content_type) == Origin.EXTERNAL
    assert origin_for("mcp", content_type, role="assistant") == Origin.EXTERNAL
    # A person pasting an email into a chat does not make the email theirs.
    assert origin_for("telegram", content_type, role="user") == Origin.EXTERNAL


def test_a_tool_result_is_tool_and_crm_rows_are_crm() -> None:
    from robothor.memory.write_path import origin_for

    assert origin_for("api", "tool_result") == Origin.AGENT_GENERATED  # never raised
    assert origin_for("api", "tool_result", role="tool") == Origin.TOOL
    assert origin_for("crm", "note") == Origin.CRM
    assert origin_for("crm", "email") == Origin.EXTERNAL


def test_operator_confirmed_metadata_never_raises_trust() -> None:
    """Only the operator's own edit confirms a fact; ingest metadata cannot claim it
    (review finding; test_write_path_failures.py covers the callers)."""
    from robothor.memory.write_path import origin_for

    meta = {"operator_confirmed": {"at": "2026-01-01T00:00:00+00:00", "by": "op"}}
    assert origin_for("email", "email", metadata=meta) == Origin.EXTERNAL


@pytest.mark.parametrize("channel", ["webchat", "scheduled", "", None, "unknown-thing"])
def test_an_unknown_channel_is_never_trusted_more_than_agent_generated(channel: Any) -> None:
    from robothor.memory.graph_schema import authority_rank
    from robothor.memory.write_path import origin_for

    got = origin_for(channel, "note")
    assert authority_rank(got) <= authority_rank(Origin.AGENT_GENERATED)


def test_every_origin_the_mapping_returns_is_a_fact_origin() -> None:
    from robothor.memory.write_path import origin_for

    seen = set()
    for channel in [*VALID_CHANNELS, "webchat", None]:
        for role in (None, "user", "assistant", "tool", "system", "bogus"):
            for ctype in ("conversation", "email", "tool_result", "note", None):
                seen.add(origin_for(channel, ctype, role=role))
    assert seen <= set(FACT_ORIGINS)
    assert Origin.OPERATOR_CONFIRMED not in seen  # only metadata confirms


# ═══ scope ═══════════════════════════════════════════════════════════════


def test_the_conversation_owner_rule_mirrors_the_segments_rule_and_fails_closed() -> None:
    from robothor.memory.write_path import conversation_scope

    room = "agent:main:room:" + "1b0c8f5e-1111-4222-8333-444455556666"
    assert conversation_scope(room, ALICE) == ("shared", None)
    assert conversation_scope("agent:main:telegram:4242", ALICE) == ("user", ALICE_P)
    assert conversation_scope("agent:main:user:u-7", None) == ("user", "user:u-7")
    assert conversation_scope("agent:main:primary", None, owner_person_id=BOB) == ("user", BOB_P)
    assert conversation_scope("agent:main:telegram:4242", None) == ("user", None)


def test_writes_are_shared_unless_a_scope_is_asked_for() -> None:
    from robothor.memory.write_path import WriteContext

    plain = WriteContext.for_source(
        tenant_id="test-tenant", source_channel="telegram", content_type="conversation",
        role="user", metadata={"session_key": "agent:main:telegram:4242"},
    )  # fmt: skip
    assert (plain.scope_kind, plain.scope_id) == ("shared", None)
    asked = WriteContext.for_source(
        tenant_id="test-tenant", source_channel="telegram", content_type="conversation",
        role="user", scope=("user", ALICE_P),
    )  # fmt: skip
    assert (asked.scope_kind, asked.scope_id, asked.person_id) == ("user", ALICE_P, ALICE)
    # An owner-less private scope is refused, never widened to shared.
    with pytest.raises(ValueError):
        WriteContext.for_source(
            tenant_id="test-tenant", source_channel="telegram", content_type="note",
            scope=("user", None),
        )  # fmt: skip


# ═══ the content hash ════════════════════════════════════════════════════


def _old_hash(text: str, tenant: str, category: str, person: str | None = None) -> str:
    return hashlib.sha256(f"{text}|{tenant}|{category}|{person or ''}".encode()).hexdigest()


def test_a_shared_fact_keeps_the_hash_every_existing_row_has() -> None:
    from robothor.memory.drift import compute_fact_hash

    assert compute_fact_hash("Alice likes tea", tenant_id="t", category="preference") == _old_hash(
        "Alice likes tea", "t", "preference"
    )
    assert compute_fact_hash(
        "Alice likes tea", tenant_id="t", category="preference", scope_kind="shared"
    ) == _old_hash("Alice likes tea", "t", "preference")


def test_a_private_fact_hashes_apart_from_shared_and_from_another_person() -> None:
    from robothor.memory.drift import compute_fact_hash

    shared = compute_fact_hash("Alice likes tea", tenant_id="t", category="preference")

    def h(kind: str, sid: str) -> str:
        return compute_fact_hash(
            "Alice likes tea", tenant_id="t", category="preference", scope_kind=kind, scope_id=sid
        )

    mine, theirs, member, agent = (
        h("user", ALICE_P),
        h("user", BOB_P),
        h("user", "user:u-7"),
        h("agent", ALICE),
    )
    assert len({shared, mine, theirs, member, agent}) == 5
    # Never the digest of a SHARED row that carries the person in person_id (the
    # bridge edit and update_fact hash such rows with it): that is a collision on
    # the active-row unique index, and a private fact would merge into it.
    assert mine != _old_hash("Alice likes tea", "t", "preference", ALICE)


# ═══ predicate normalisation ═════════════════════════════════════════════


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("works at", "works_at"),
        ("  Works-At ", "works_at"),
        ("MANAGES", "manages"),
        ("built_with", "built_with"),
        ("reports  to", "reports_to"),
        ("", None),
        ("   ", None),
    ],
)
def test_predicate_normalisation(phrase: str, expected: str | None) -> None:
    from robothor.memory.write_path import normalize_predicate

    assert normalize_predicate(phrase) == expected


# ═══ the one extraction ══════════════════════════════════════════════════


def _payload(*facts: dict[str, Any]) -> str:
    return json.dumps({"facts": list(facts)})


def _fact(text: str, mentions: list[tuple[str, str]], relations=(), **kw: Any) -> dict[str, Any]:
    return {
        "fact_text": text,
        "category": kw.get("category", "project"),
        "confidence": kw.get("confidence", 0.9),
        "event_time": kw.get("event_time"),
        "mentions": [{"surface_form": s, "entity_type": t} for s, t in mentions],
        "relations": [{"source": s, "relation": r, "target": t} for s, r, t in relations],
    }


class StrictFakeModel:
    """Answers only the structured calls it is scripted for, and checks each answer
    against the schema it was asked for, the way constrained decoding would."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def generate(self, prompt: str, system: str | None = None, **kw: Any) -> str:
        from robothor.memory.facts import FACT_EXTRACTION_SCHEMA

        self.calls.append({"prompt": prompt, "system": system, **kw})
        if kw.get("format") != FACT_EXTRACTION_SCHEMA:
            raise AssertionError(f"unexpected model call: {kw.get('format')!r}")
        answer = self.answers.pop(0)
        jsonschema.validate(json.loads(answer), FACT_EXTRACTION_SCHEMA)
        return answer


def test_the_schema_is_strict_and_asks_for_typed_mentions_relations_and_event_time() -> None:
    from robothor.memory.entities import VALID_ENTITY_TYPES
    from robothor.memory.facts import FACT_EXTRACTION_SCHEMA

    item = FACT_EXTRACTION_SCHEMA["properties"]["facts"]["items"]
    assert FACT_EXTRACTION_SCHEMA["additionalProperties"] is False
    assert item["additionalProperties"] is False
    assert (
        set(item["required"])
        == set(item["properties"])
        == {
            "fact_text",
            "category",
            "confidence",
            "event_time",
            "mentions",
            "relations",
        }
    )
    mention = item["properties"]["mentions"]["items"]
    assert mention["properties"]["entity_type"]["enum"] == VALID_ENTITY_TYPES
    assert set(mention["required"]) == {"surface_form", "entity_type"}
    rel = item["properties"]["relations"]["items"]
    assert set(rel["required"]) == {"source", "relation", "target"}
    assert item["properties"]["event_time"]["type"] == ["string", "null"]
    # A missing field is rejected, as constrained decoding would reject it.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"facts": [{"fact_text": "x"}]}, FACT_EXTRACTION_SCHEMA)


@pytest.mark.asyncio
async def test_one_call_returns_facts_with_typed_mentions_relations_and_event_time() -> None:
    from robothor.memory.facts import extract_facts_detailed

    model = StrictFakeModel(
        _payload(
            _fact(
                "Alice Moreno joined Northwind Traders on 2026-03-02.",
                [("Alice Moreno", "person"), ("Northwind Traders", "organization")],
                [("Alice Moreno", "joined", "Northwind Traders")],
                event_time="2026-03-02",
            ),
            _fact(
                "Bob Chen prefers Python for Project Helios.",
                [("Bob Chen", "person"), ("Python", "technology"), ("Project Helios", "project")],
                [("Bob Chen", "prefers", "Python")],
                event_time="sometime soon",  # not a date: never kept
            ),
        )
    )
    with patch("robothor.memory.generation.generate", new=model.generate):
        out = await extract_facts_detailed("Alice Moreno joined Northwind Traders ...")

    assert len(model.calls) == 1
    assert out.error is None
    first, second = out.facts
    assert first["mentions"] == [
        {"surface_form": "Alice Moreno", "entity_type": "person"},
        {"surface_form": "Northwind Traders", "entity_type": "organization"},
    ]
    assert first["relations"] == [
        {"source": "Alice Moreno", "relation": "joined", "target": "Northwind Traders"}
    ]
    assert first["event_time"] == "2026-03-02"
    assert second["event_time"] is None
    # The compatibility column is still filled, from the mentions.
    assert first["entities"] == ["Alice Moreno", "Northwind Traders"]
    assert second["entities"] == ["Bob Chen", "Python", "Project Helios"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-03-02", "2026-03-02"),
        ("2026-03-02T14:30:00", "2026-03-02T14:30:00"),
        ("2026-03-02T14:30:00+00:00", "2026-03-02T14:30:00+00:00"),
        (None, None),
        ("", None),
        ("yesterday", None),
        ("2026-13-45", None),
        (20260302, None),
    ],
)
def test_event_time_is_kept_only_when_it_is_a_real_date(raw: Any, expected: Any) -> None:
    from robothor.memory.facts import parse_extraction_response

    facts = parse_extraction_response(
        _payload(
            _fact("Alice Moreno met Bob Chen in Lisbon.", [("Alice Moreno", "person")])
        ).replace('"event_time": null', f'"event_time": {json.dumps(raw)}')
    )
    assert facts[0]["event_time"] == expected


def test_the_legacy_array_shape_still_parses() -> None:
    from robothor.memory.facts import parse_extraction_response

    raw = json.dumps(
        [
            {
                "fact_text": "Alice Moreno leads Project Helios",
                "category": "project",
                "entities": ["Alice Moreno", "Project Helios"],
                "confidence": 0.9,
            }
        ]  # fmt: skip
    )
    (fact,) = parse_extraction_response(raw)
    assert fact["entities"] == ["Alice Moreno", "Project Helios"]
    assert fact["mentions"] == [
        {"surface_form": "Alice Moreno", "entity_type": None},
        {"surface_form": "Project Helios", "entity_type": None},
    ]
    assert fact["relations"] == [] and fact["event_time"] is None


def test_a_mention_of_an_unknown_type_is_kept_untyped() -> None:
    from robothor.memory.facts import parse_extraction_response

    raw = _payload(_fact("Alice Moreno visited the Lisbon office.", [("Alice Moreno", "person")]))
    raw = raw.replace('"person"', '"spaceship"')
    (fact,) = parse_extraction_response(raw)
    assert fact["mentions"] == [{"surface_form": "Alice Moreno", "entity_type": None}]


@pytest.mark.asyncio
async def test_extraction_failure_is_reported_not_silently_empty() -> None:
    from robothor.memory.facts import extract_facts_detailed

    with patch("robothor.memory.generation.generate", new=AsyncMock(return_value="not json")):
        out = await extract_facts_detailed("Alice Moreno met Bob Chen.", max_retries=2)
    assert out.facts == [] and out.error == "unparseable"

    with patch("robothor.memory.generation.generate", new=AsyncMock(side_effect=OSError("down"))):
        out = await extract_facts_detailed("Alice Moreno met Bob Chen.", max_retries=1)
    assert out.facts == [] and out.error and "OSError" in out.error

    async def _slow(*_a: Any, **_k: Any) -> str:
        import asyncio

        await asyncio.sleep(5)
        return "[]"

    with patch("robothor.memory.generation.generate", new=_slow):
        out = await extract_facts_detailed("Alice Moreno met Bob Chen.", timeout=0.05)
    assert out.facts == [] and out.error == "timeout"

    # The model answered, and there was nothing to remember: not a failure.
    with patch("robothor.memory.generation.generate", new=AsyncMock(return_value='{"facts": []}')):
        out = await extract_facts_detailed("hi!", max_retries=1)
    assert out.facts == [] and out.error is None


@pytest.mark.asyncio
async def test_read_only_context_is_delimited_and_not_the_content() -> None:
    from robothor.memory.facts import extract_facts_detailed

    model = StrictFakeModel(_payload())
    with patch("robothor.memory.generation.generate", new=model.generate):
        await extract_facts_detailed(
            "User: book it for Friday", context="Assistant: Shall I book Lisbon?"
        )
    prompt = model.calls[0]["prompt"]
    assert "Shall I book Lisbon?" in prompt
    ctx_at = prompt.index("Shall I book Lisbon?")
    content_at = prompt.index("book it for Friday")
    assert ctx_at < content_at
    assert "do not extract" in prompt.lower()


# ═══ the ingest path makes one model call, not two ═══════════════════════


@pytest.mark.asyncio
async def test_ingest_content_makes_no_separate_entity_extraction_call(monkeypatch: Any) -> None:
    import robothor.memory.ingestion as ing

    monkeypatch.delenv("MEMORY_VAULT_POPULATE", raising=False)
    write = AsyncMock(return_value={"fact_ids": [1], "facts_processed": 1})
    with (
        patch("robothor.memory.entities.extract_entities", new=AsyncMock()) as ent,
        patch("robothor.memory.write_path.ingest_text", new=write),
        patch("robothor.memory.vault._vault_populate_enabled", return_value=False),
    ):
        out = await ing.ingest_content("Alice Moreno met Bob Chen.", "email", "email",
                                       tenant_id="test-tenant")  # fmt: skip
    ent.assert_not_awaited()
    ctx = write.call_args.args[1]
    assert ctx.origin == Origin.EXTERNAL and ctx.tenant_id == "test-tenant"
    assert out["fact_ids"] == [1]


# ═══ store_memory goes through the write path ════════════════════════════


@pytest.mark.asyncio
async def test_store_memory_uses_the_write_path_with_agent_origin_and_stays_shared(
    monkeypatch: Any,
) -> None:
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers.memory import HANDLERS
    from robothor.identity.context import IdentityContext

    monkeypatch.delenv("MEMORY_ASYNC_WRITE", raising=False)
    write = AsyncMock(return_value={"fact_ids": [41, 42], "facts_processed": 2})
    ident = IdentityContext(
        tenant_id="test-tenant", channel="telegram", identifier="4242", verified=True,
        person_id=ALICE,
    )  # fmt: skip
    with patch("robothor.memory.write_path.ingest_text", new=write):
        out = await HANDLERS["store_memory"](
            {"content": "Alice prefers email.", "content_type": "preference"},
            ToolContext(agent_id="example-agent", tenant_id="test-tenant", identity=ident),
        )
    assert out["id"] == 41 and out["facts_stored"] == 2
    ctx = write.call_args.args[1]
    assert ctx.origin == Origin.AGENT_GENERATED
    # A verified person on the run does not make the fact private: who can
    # recall what is unchanged unless a caller explicitly asks for a scope.
    assert (ctx.scope_kind, ctx.scope_id) == ("shared", None)
    assert write.call_args.kwargs["fallback_on_empty"] is True
    assert write.call_args.kwargs["conflict_check"] is True


@pytest.mark.asyncio
async def test_store_memory_writes_a_private_scope_only_when_asked(monkeypatch: Any) -> None:
    from robothor.engine.tools.handlers.memory import store_memory_content

    write = AsyncMock(return_value={"fact_ids": [7], "facts_processed": 1})
    with patch("robothor.memory.write_path.ingest_text", new=write):
        await store_memory_content("Alice prefers email.", tenant_id="test-tenant")
        await store_memory_content(
            "Alice prefers email.", tenant_id="test-tenant", scope=("user", ALICE_P)
        )
    shared, private = (c.args[1] for c in write.call_args_list)
    assert (shared.scope_kind, shared.scope_id) == ("shared", None)
    assert (private.scope_kind, private.scope_id) == ("user", ALICE_P)


# ═══ conversation ingest extracts per role ═══════════════════════════════


@pytest.mark.asyncio
async def test_conversation_ingest_extracts_user_and_assistant_turns_separately(
    monkeypatch: Any,
) -> None:
    import robothor.memory.conversation_ingest as ci

    monkeypatch.delenv("MEMORY_INGEST_SKIP_GENERATED", raising=False)
    history = [
        {"role": "user", "content": "I moved to Lisbon in May."},
        {"role": "assistant", "content": "Noted: you moved to Lisbon."},
        {"role": "user", "content": "Bob Chen is my manager now."},
        {"role": "assistant", "content": "The email from Northwind says the deal closed."},
    ]
    write = AsyncMock(side_effect=[{"fact_ids": [1]}, {"fact_ids": [2, 3]}])
    with (
        patch.object(ci, "is_already_ingested", return_value=False),
        patch.object(ci, "record_ingested") as rec,
        patch("robothor.memory.write_path.ingest_text", new=write),
    ):
        out = await ci.ingest_conversation_session(
            "agent:main:telegram:4242", history, "main", "telegram", "run-1", "test-tenant"
        )

    assert write.await_count == 2
    by_origin = {c.args[1].origin: c for c in write.call_args_list}
    user_call = by_origin[Origin.USER_STATED]
    asst_call = by_origin[Origin.AGENT_GENERATED]
    assert "Lisbon in May" in user_call.args[0] and "Bob Chen" in user_call.args[0]
    assert "Northwind" not in user_call.args[0]
    assert "Northwind" in asst_call.args[0] and "Lisbon in May" not in asst_call.args[0]
    # The other role is shown as read-only context.
    assert "Northwind" in user_call.kwargs["read_only_context"]
    assert "Lisbon in May" in asst_call.kwargs["read_only_context"]
    for c in (user_call, asst_call):
        ctx = c.args[1]
        assert (ctx.scope_kind, ctx.scope_id) == ("shared", None)  # as before this story
        assert ctx.conversation_ref == "agent:main:telegram:4242"
        assert ctx.tenant_id == "test-tenant"
    assert sorted(out["fact_ids"]) == [1, 2, 3]
    assert sorted(rec.call_args.args[3]) == [1, 2, 3]


# ═══ write jobs carry what the write path needs ══════════════════════════


@pytest.mark.asyncio
async def test_a_write_job_replays_through_the_write_path_with_its_context() -> None:
    from robothor.memory import write_jobs
    from robothor.memory.write_path import WriteContext

    ctx = WriteContext(
        tenant_id="test-tenant", source_channel="mcp", content_type="note",
        origin=Origin.AGENT_GENERATED, scope_kind="user", scope_id=ALICE_P,
    )  # fmt: skip
    row = ("Alice prefers email.", "note", "test-tenant", ctx.to_json())

    class _Cur:
        def execute(self, *_a: Any) -> None: ...

        def fetchone(self) -> Any:
            return row

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_a: Any) -> None: ...

        def cursor(self) -> _Cur:
            return _Cur()

        def commit(self) -> None: ...

    write = AsyncMock(return_value={"fact_ids": [5]})
    with (
        patch.object(write_jobs, "get_connection", return_value=_Conn()),
        patch.object(write_jobs, "_finish") as fin,
        patch("robothor.memory.write_path.ingest_text", new=write),
    ):
        assert await write_jobs.process_write_job(9) is True
    replayed = write.call_args.args[1]
    assert replayed == ctx
    assert fin.call_args.kwargs["fact_ids"] == [5]


def test_write_context_round_trips_and_defaults_safely() -> None:
    from robothor.memory.write_path import WriteContext

    ctx = WriteContext(
        tenant_id="test-tenant", source_channel="telegram", content_type="conversation",
        origin=Origin.USER_STATED, scope_kind="user", scope_id=ALICE_P,
        conversation_ref="agent:main:telegram:4242", metadata={"run_id": "r1"},
    )  # fmt: skip
    assert WriteContext.from_json(ctx.to_json(), tenant_id="test-tenant") == ctx
    assert ctx.person_id == ALICE
    legacy = WriteContext.from_json(None, tenant_id="test-tenant", content_type="note")
    assert legacy.origin == Origin.AGENT_GENERATED
    assert (legacy.scope_kind, legacy.scope_id) == ("shared", None)
    # A job payload is never allowed to name another tenant.
    spoof = WriteContext.from_json({**json.loads(json.dumps(ctx.to_json())), "tenant_id": "x"},
                                   tenant_id="test-tenant")  # fmt: skip
    assert spoof.tenant_id == "test-tenant"


# ═══ no blocking database work on the event loop ═════════════════════════


@pytest.mark.asyncio
async def test_store_memory_does_its_database_work_off_the_event_loop(monkeypatch: Any) -> None:
    """Each database step of the write path (similarity search, insert, graph)
    takes 200 ms here; a loop callback scheduled as each starts must still run
    within 20 ms (the probe pattern of test_offloop_sync_calls)."""
    import asyncio
    import threading
    import time
    from contextlib import contextmanager

    from robothor.engine.tools.handlers.memory import store_memory_content
    from robothor.memory import conflicts, facts, write_path

    loop = asyncio.get_running_loop()
    gate = threading.Barrier(4, timeout=2)
    await asyncio.gather(*(asyncio.to_thread(gate.wait) for _ in range(4)))
    lags: list[float] = []

    def _slow() -> None:
        entered = time.monotonic()
        loop.call_soon_threadsafe(lambda: lags.append(time.monotonic() - entered))
        time.sleep(0.2)

    class _Cur:
        def execute(self, *_a: Any, **_k: Any) -> None: ...

        def fetchall(self) -> list[Any]:
            return []

        def fetchone(self) -> Any:
            return (7,)

    class _Conn:
        def cursor(self, *_a: Any, **_k: Any) -> _Cur:
            return _Cur()

    @contextmanager
    def _conn(*_a: Any, **_k: Any) -> Any:
        _slow()
        yield _Conn()

    def _graph(*_a: Any, **_k: Any) -> dict[str, int]:
        _slow()
        return {}

    extracted = facts.Extraction(
        [
            {
                "fact_text": "Alice Moreno prefers email.",
                "category": "preference",
                "confidence": 0.9,
                "entities": ["Alice Moreno"],
                "mentions": [],
                "relations": [],
                "event_time": None,
            }
        ]
    )
    monkeypatch.setattr(facts, "extract_facts_detailed", AsyncMock(return_value=extracted))
    monkeypatch.setattr(facts, "get_connection", _conn)
    monkeypatch.setattr(conflicts, "get_connection", _conn)
    monkeypatch.setattr(conflicts, "apply_hnsw_session", lambda _c: None)
    monkeypatch.setattr(write_path, "_write_graph", _graph)
    monkeypatch.setattr(write_path, "_embed", AsyncMock(return_value=[[0.1] * 4]))
    monkeypatch.delenv("MEMORY_QUALITY_GATE", raising=False)

    out = await store_memory_content("Alice prefers email.", tenant_id="test-tenant")
    deadline = time.monotonic() + 2
    while len(lags) < 3 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert out["facts_stored"] == 1
    assert len(lags) >= 3, lags  # search, insert, graph
    assert max(lags) <= 0.02, f"a loop callback waited {max(lags) * 1000:.0f} ms"


def test_an_email_mention_reaches_identity_as_a_strong_key() -> None:
    """Identity no longer answers an email by alias lookup; the write side must
    hand it over as a strong key."""
    from robothor.memory.write_path import WriteContext, _mention_inputs

    ctx = WriteContext(
        tenant_id="test-tenant", source_channel="email", content_type="email",
        origin=Origin.EXTERNAL,
    )  # fmt: skip
    fact = {
        "fact_text": "Alice.Moreno@Example.com confirmed the Helios kickoff.",
        "mentions": [
            {"surface_form": "Alice.Moreno@Example.com", "entity_type": "person"},
            {"surface_form": "Helios", "entity_type": "project"},
        ],
        "relations": [],
    }
    email, project = _mention_inputs(None, fact, ctx)
    assert dict(email.strong_keys) == {"email": "alice.moreno@example.com"}
    assert dict(project.strong_keys) == {}
