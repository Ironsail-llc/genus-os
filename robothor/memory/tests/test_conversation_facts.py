"""The conversation extraction mode: what the model is shown and what is kept.

No database and no model: the prompt is inspected as text and the model's
answer is given as JSON. Assertions are about properties (who said it, which
turns, what survives the filters), never about exact model wording.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import jsonschema
import pytest

from robothor.memory.conversation_facts import (
    CONVERSATION_EXTRACTION_SCHEMA,
    ConversationTurn,
    build_conversation_prompt,
    extract_conversation_facts,
    parse_conversation_response,
)
from robothor.memory.facts import build_extraction_prompt, parse_extraction_response

AT = datetime(2023, 5, 20, 14, 3, tzinfo=UTC)


def _turns() -> list[ConversationTurn]:
    return [
        ConversationTurn(ref=101, role="user", text="I prefer dark roast coffee.", at=AT),
        ConversationTurn(
            ref=102,
            role="assistant",
            text="Try these three: Kona, Sumatra and French roast.",
            at=AT,
        ),
        ConversationTurn(ref=103, role="user", text="I bought a road bike yesterday.", at=AT),
    ]


def _fact(text: str, turns: list[int], speaker: str = "user", **kw: Any) -> dict[str, Any]:
    return {
        "fact_text": text,
        "category": kw.get("category", "preference"),
        "kind": kw.get("kind", "preference"),
        "confidence": kw.get("confidence", 0.9),
        "event_time": kw.get("event_time"),
        "about_user": kw.get("about_user", True),
        "speaker": speaker,
        "turns": turns,
        "mentions": kw.get("mentions", []),
        "relations": [],
    }


def _answer(*facts: dict[str, Any]) -> str:
    return json.dumps({"facts": list(facts)})


# ═══ the prompt sees the exchange ════════════════════════════════════════


def test_the_prompt_interleaves_turns_with_speaker_and_time_in_order() -> None:
    prompt = build_conversation_prompt(_turns())
    body = prompt[prompt.index("I prefer dark roast") - 80 :]
    first = body.index("I prefer dark roast")
    second = body.index("Kona, Sumatra")
    third = body.index("road bike")
    assert first < second < third
    for line in [ln for ln in body.splitlines() if "dark roast coffee" in ln or "Kona" in ln]:
        assert "2023-05-20" in line  # each turn carries its own time
    assert any("User" in ln and "dark roast" in ln for ln in body.splitlines())
    assert any("Assistant" in ln and "Kona" in ln for ln in body.splitlines())


def test_the_prompt_asks_for_facts_per_speaker_and_keeps_first_person_facts() -> None:
    prompt = build_conversation_prompt(_turns()).lower()
    assert "speaker" in prompt
    assert "the user" in prompt  # first-person statements become facts about the user
    # No named-entity requirement in this mode.
    assert "must reference at least one specific named entity" not in prompt
    assert "don't mention any specific person" not in prompt
    for kept in ("preference", "plan", "recommend", "bought"):
        assert kept in prompt


def test_the_prompt_resolves_dates_against_each_turn_not_today() -> None:
    today = datetime.now(UTC).date().isoformat()
    prompt = build_conversation_prompt(_turns())
    assert today not in prompt
    assert "relative to today" not in prompt.lower()
    assert "timestamp" in prompt.lower() or "time of the turn" in prompt.lower()


def test_context_turns_are_shown_read_only_before_the_window() -> None:
    earlier = [ConversationTurn(ref=90, role="user", text="We talked about hiking.", at=AT)]
    prompt = build_conversation_prompt(_turns(), context=earlier)
    assert prompt.index("We talked about hiking.") < prompt.index("I prefer dark roast")
    head = prompt[: prompt.index("We talked about hiking.")].lower()
    assert "already" in head or "read-only" in head


def test_room_turns_are_labelled_with_their_author() -> None:
    turns = [ConversationTurn(ref=1, role="user", text="I like tea.", at=AT, speaker="Alice")]
    prompt = build_conversation_prompt(turns)
    assert any("Alice" in ln and "I like tea." in ln for ln in prompt.splitlines())


def test_the_schema_is_strict_and_accepts_a_conversation_answer() -> None:
    jsonschema.validate(
        json.loads(_answer(_fact("The user prefers dark roast coffee.", [1]))),
        CONVERSATION_EXTRACTION_SCHEMA,
    )
    item = CONVERSATION_EXTRACTION_SCHEMA["properties"]["facts"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])


# ═══ what the parse keeps ════════════════════════════════════════════════


def test_a_first_person_fact_with_no_named_entity_is_kept() -> None:
    (fact,) = parse_conversation_response(
        _answer(_fact("The user prefers dark roast coffee.", [1])), _turns()
    )
    assert fact["fact_text"] == "The user prefers dark roast coffee."
    assert fact["about_user"] is True
    assert fact["entities"] == []


def test_each_fact_is_linked_to_the_turns_it_came_from() -> None:
    facts = parse_conversation_response(
        _answer(
            _fact("The user prefers dark roast coffee.", [1]),
            _fact(
                "The assistant recommended three coffees: Kona, Sumatra and French roast.",
                [2],
                speaker="assistant",
                kind="recommendation",
                about_user=False,
            ),
        ),
        _turns(),
    )
    refs = {f["fact_text"][:20]: f["turn_refs"] for f in facts}
    assert refs["The user prefers dar"] == [101]
    assert refs["The assistant recomm"] == [102]


def test_a_user_statement_is_user_stated_and_an_assistant_suggestion_is_not() -> None:
    facts = parse_conversation_response(
        _answer(
            _fact("The user prefers dark roast coffee.", [1]),
            _fact("Kona is a good coffee for the user.", [2], speaker="assistant"),
        ),
        _turns(),
    )
    roles = {f["fact_text"]: f["speaker_role"] for f in facts}
    assert roles == {
        "The user prefers dark roast coffee.": "user",
        "Kona is a good coffee for the user.": "assistant",
    }


def test_assistant_text_never_becomes_user_stated_whatever_the_model_claims() -> None:
    facts = parse_conversation_response(
        _answer(
            # The model says the user said it, but cites the assistant's turn.
            _fact("The user will drink Kona.", [2], speaker="user"),
            # Cites both: the least trusted speaker wins.
            _fact("The user chose Sumatra after the suggestion.", [1, 2], speaker="user"),
            # Cites nothing usable: it cannot be shown to be the user's.
            _fact("The user likes French roast.", [99], speaker="user"),
        ),
        _turns(),
    )
    assert {f["speaker_role"] for f in facts} == {"assistant"}
    uncited = next(f for f in facts if "French roast" in f["fact_text"])
    assert uncited["turn_refs"] == [101, 102, 103]  # linked to the whole window


def test_a_fact_only_from_context_turns_is_not_extracted_again() -> None:
    earlier = [ConversationTurn(ref=90, role="user", text="I live in Lisbon.", at=AT)]
    facts = parse_conversation_response(
        _answer(
            _fact("The user lives in Lisbon.", [1]),
            _fact("The user prefers dark roast coffee.", [2]),
        ),
        _turns(),
        context=earlier,
    )
    assert [f["fact_text"] for f in facts] == ["The user prefers dark roast coffee."]
    assert facts[0]["turn_refs"] == [101]


def test_the_model_cannot_supply_an_identity_anchor() -> None:
    item = _fact("The user prefers dark roast coffee.", [1])
    item["mentions"] = [
        {"surface_form": "Alice Moreno", "entity_type": "person", "crm_person_id": "x"}
    ]
    (fact,) = parse_conversation_response(_answer(item), _turns())
    assert all(set(m) == {"surface_form", "entity_type"} for m in fact["mentions"])


def test_garbage_is_still_filtered() -> None:
    facts = parse_conversation_response(
        _answer(
            _fact("Hi there", [1]),
            _fact("The user prefers dark roast coffee.", [1], confidence=0.1),
            _fact("ok", [1]),
        ),
        _turns(),
    )
    assert facts == []


def test_the_kind_and_mention_time_are_kept() -> None:
    (fact,) = parse_conversation_response(
        _answer(_fact("The user bought a road bike.", [3], kind="possession")), _turns()
    )
    assert fact["kind"] == "possession"
    assert fact["mentioned_at"].startswith("2023-05-20")


# ═══ one call per window ═════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_one_window_is_one_model_call_with_the_strict_schema() -> None:
    gen = AsyncMock(return_value=_answer(_fact("The user prefers dark roast coffee.", [1])))
    with patch("robothor.memory.generation.generate", new=gen):
        out = await extract_conversation_facts(_turns())
    assert gen.await_count == 1
    assert gen.call_args.kwargs["format"] == CONVERSATION_EXTRACTION_SCHEMA
    assert out.error is None and len(out.facts) == 1


@pytest.mark.asyncio
async def test_a_window_with_nothing_to_remember_is_not_asked_again() -> None:
    gen = AsyncMock(return_value=_answer())
    with patch("robothor.memory.generation.generate", new=gen):
        out = await extract_conversation_facts(_turns())
    assert gen.await_count == 1
    assert out.error is None and out.facts == []


@pytest.mark.asyncio
async def test_an_unparseable_answer_is_retried_then_reported() -> None:
    gen = AsyncMock(return_value="{ not json")
    with patch("robothor.memory.generation.generate", new=gen):
        out = await extract_conversation_facts(_turns(), max_retries=2)
    assert gen.await_count == 2
    assert out.error == "unparseable"


# ═══ other callers keep today's behaviour ════════════════════════════════


def test_the_default_extraction_mode_is_unchanged() -> None:
    prompt = build_extraction_prompt("Alice Moreno chose Redis.")
    assert "MUST reference at least one specific named entity" in prompt
    raw = json.dumps(
        {
            "facts": [
                {
                    "fact_text": "The user prefers dark roast coffee.",
                    "category": "preference",
                    "confidence": 0.9,
                    "event_time": None,
                    "mentions": [],
                    "relations": [],
                }
            ]
        }
    )
    assert parse_extraction_response(raw) == []  # still needs a named entity


# ═══ relative dates resolve against the turn that says them ══════════════


def _said(text: str, at: datetime = AT) -> list[ConversationTurn]:
    return [ConversationTurn(ref=7, role="user", text=text, at=at)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I bought a road bike yesterday.", "2023-05-19"),
        ("I'm flying to Rome tomorrow.", "2023-05-21"),
        ("I sold my old car three days ago.", "2023-05-17"),
        ("We adopted a puppy two weeks ago.", "2023-05-06"),
        ("My exam is in 10 days.", "2023-05-30"),
        ("I started yoga last Tuesday.", "2023-05-16"),  # AT is a Saturday
        ("I'm seeing the dentist next Monday.", "2023-05-22"),
        ("I finished the book the day before yesterday.", "2023-05-18"),
    ],
)
def test_a_relative_date_is_counted_from_the_turns_own_time(text: str, expected: str) -> None:
    from robothor.memory.relative_dates import resolve_relative_date

    assert resolve_relative_date(text, AT) is not None
    assert resolve_relative_date(text, AT).isoformat() == expected  # type: ignore[union-attr]


def test_no_date_or_two_different_dates_resolve_to_nothing() -> None:
    from robothor.memory.relative_dates import resolve_relative_date

    assert resolve_relative_date("I like tea.", AT) is None
    assert resolve_relative_date("I bought it yesterday and return it tomorrow.", AT) is None
    assert resolve_relative_date("yesterday", None) is None


def test_event_time_is_filled_from_the_turn_when_the_model_left_it_empty() -> None:
    (fact,) = parse_conversation_response(
        _answer(_fact("The user bought a road bike.", [1], kind="possession")),
        _said("I bought a road bike yesterday."),
    )
    assert fact["event_time"] == "2023-05-19"


def test_a_date_the_model_counted_from_today_is_counted_from_the_turn_instead() -> None:
    from datetime import timedelta

    from_today = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    (fact,) = parse_conversation_response(
        _answer(_fact("The user bought a road bike.", [1], event_time=from_today)),
        _said("I bought a road bike yesterday."),
    )
    assert fact["event_time"] == "2023-05-19"


def test_a_date_the_model_gave_is_kept_when_the_turn_names_no_relative_date() -> None:
    (fact,) = parse_conversation_response(
        _answer(_fact("The user moves to Lisbon in June 2023.", [1], event_time="2023-06-01")),
        _said("We move to Lisbon in June."),
    )
    assert fact["event_time"] == "2023-06-01"


@pytest.mark.parametrize(
    ("text", "model_said", "expected"),
    [
        ("I switched to an iPhone last Tuesday.", "2023-05-15", "2023-05-16"),
        ("I'm flying to Rome in two weeks.", "2023-05-27", "2023-06-03"),
    ],
)
def test_a_relative_date_the_model_miscounted_is_counted_from_the_turn(
    text: str, model_said: str, expected: str
) -> None:
    (fact,) = parse_conversation_response(
        _answer(_fact("The user did something worth remembering.", [1], event_time=model_said)),
        _said(text),
    )
    assert fact["event_time"] == expected


def test_today_only_fills_an_empty_event_time() -> None:
    # "today" frames the turn more often than it dates the fact.
    (fact,) = parse_conversation_response(
        _answer(_fact("The user booked a trip for June 2023.", [1], event_time="2023-06-10")),
        _said("Today I booked a trip for next month."),
    )
    assert fact["event_time"] == "2023-06-10"


def test_the_prompt_asks_for_absolute_dates_and_no_date_for_a_suggestion() -> None:
    prompt = build_conversation_prompt(_turns()).lower()
    assert "absolute date" in prompt
    assert "not when it was said" in prompt
