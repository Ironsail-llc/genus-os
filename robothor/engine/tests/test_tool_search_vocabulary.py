"""The queries an operator actually types, and the tool each one must reach.

Every row here is a query that was measured against the live registry on
2026-09-16 and returned the wrong tool, no tool, or a tool from the wrong
system. Under deferred tool loading these searches ARE the agent's tool
discovery: ``main`` is advertised 18 schemas and not one of them is a Gmail or
Calendar tool, so "check my email" is answered by whatever ``tool_search``
ranks first. It ranked ``browser``.

The table is run twice — over the full registry, and over a fixture with the
same shape as ``main``'s 98-name allow-set — because a ranking that only works
when every tool is a candidate is not the ranking the agent gets.
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.constants import CORE_TOOLS, GWS_TOOLS
from robothor.engine.tools.registry import ToolRegistry


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.fixture(scope="module")
def all_names(registry: ToolRegistry) -> list[str]:
    return list(registry._schemas)


@pytest.fixture(scope="module")
def broad_agent_names(registry: ToolRegistry) -> list[str]:
    """A broad-access allow-set shaped like the fleet's main agent's.

    Not a copy of any instance's manifest — that is instance data. The shape is
    what matters: every Gmail and Calendar tool, the CORE set that is advertised
    under deferral, the CRM mail tools that compete with Gmail, and the
    file/memory/vision tools that beat Gmail in the measured results.
    """
    wanted = {
        *GWS_TOOLS,
        *CORE_TOOLS,
        "browser",
        "look",
        "who_is_here",
        "desktop_window_list",
        "get_inbox",
        "get_conversation",
        "list_messages",
        "ack_notification",
        "send_notification",
        "leave_breadcrumb",
        "get_entity",
        "search_people",
        "get_person",
        "create_person",
        "list_tasks",
        "create_task",
        "update_task",
        "resolve_task",
        "approve_task",
        "reject_task",
        "record_resolution",
        "classify_run_failure",
        "register_user_cron",
        "list_agent_schedules",
        "vault_set",
        "set_vision_mode",
        "store_memory",
        "memory_block_write",
        "find_procedure",
        "get_contact_360",
        "list_contact_messages",
        "merge_contacts",
    }
    return sorted(n for n in wanted if n in registry._schemas)


def _top(registry: ToolRegistry, names: list[str], query: str, n: int = 5) -> list[str]:
    return [hit["name"] for hit in registry.search_tools(names, query, limit=n)]


# ``(query, must be first, must appear in the top three)``. A row with an empty
# ``first`` only constrains the top three.
_TABLE: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # ── Mail: reading ──
    ("check my email", "gws_gmail_search", ()),
    ("read my emails", "gws_gmail_search", ()),
    ("do I have any new email", "gws_gmail_search", ()),
    ("any unread mail", "gws_gmail_search", ()),
    ("look at my inbox", "", ("gws_gmail_search",)),
    ("what did alice say in her email", "", ("gws_gmail_search", "gws_gmail_get")),
    ("open that email and read the body", "", ("gws_gmail_get", "gws_gmail_search")),
    ("read the whole email thread", "", ("gws_gmail_get", "gws_gmail_search")),
    # ── Mail: writing ──
    ("send an email", "gws_gmail_send", ()),
    ("write an email to alice", "gws_gmail_send", ()),
    ("compose a new message", "gws_gmail_send", ()),
    ("reply to that email", "gws_gmail_reply", ()),
    ("respond to the thread", "gws_gmail_reply", ()),
    ("answer her message", "", ("gws_gmail_reply",)),
    # ── Mail: state ──
    ("mark the email as read", "gws_gmail_modify", ()),
    ("archive that email", "gws_gmail_modify", ()),
    ("label the message", "gws_gmail_modify", ()),
    # ── Calendar ──
    ("what is on my calendar today", "gws_calendar_list", ()),
    # "my schedule" is genuinely ambiguous — the agent's own cron schedules are
    # also called that — so this row only asks that reading the calendar beats
    # writing to it.
    ("what is on my schedule", "", ("gws_calendar_list",)),
    ("am I free this afternoon", "", ("gws_calendar_list",)),
    ("show me tomorrow's meetings", "gws_calendar_list", ()),
    ("schedule a meeting", "gws_calendar_create", ()),
    ("set up a meeting with attendees", "gws_calendar_create", ()),
    ("book a call", "", ("gws_calendar_create",)),
    ("invite someone to a meeting", "", ("gws_calendar_create",)),
    ("cancel the meeting", "gws_calendar_delete", ()),
    ("delete that appointment", "gws_calendar_delete", ()),
    # ── Mail: the outbound intents the keyword table missed ──
    #
    # `draft` and `forward` appeared in no keyword list and no intent verb, so
    # the only matching term was the shared `email` keyword and the family tie
    # fell to rank_bias + alphabetical — putting gws_gmail_send LAST of five on
    # two plainly outbound requests.
    ("draft an email", "gws_gmail_send", ()),
    ("forward this email", "gws_gmail_send", ()),
    # "who emailed me" and "who sent me that" are among the most natural
    # phrasings there are, and reached no mail tool at all: `_stem` was a
    # plural singulariser, so "emailed" never became "email", and `who` was not
    # a stopword so it won an exact name hit on the webcam tool `who_is_here`.
    ("who emailed me", "gws_gmail_search", ()),
    ("who sent me that", "", ("gws_gmail_search",)),
    ("check messages", "", ("gws_gmail_search",)),
    ("inbox zero", "", ("gws_gmail_search",)),
    ("read email body", "gws_gmail_get", ()),
    # ── Mail: archive is a Gmail verb, never a CRM noun ──
    ("archive this", "gws_gmail_modify", ()),
    ("archive that email", "gws_gmail_modify", ()),
    # ── Calendar ──
    ("what meetings do I have tomorrow", "gws_calendar_list", ()),
    ("list my calendars", "gws_calendar_list", ()),
    ("delete the event", "gws_calendar_delete", ()),
    # ── Chat ──
    ("post a message in the google chat space", "", ("gws_chat_send",)),
    ("what was said in that chat space", "", ("gws_chat_list_messages",)),
)


@pytest.mark.parametrize(("query", "first", "in_top"), _TABLE, ids=[r[0] for r in _TABLE])
def test_the_full_registry_ranks_the_right_tool(
    registry: ToolRegistry, all_names: list[str], query: str, first: str, in_top: tuple[str, ...]
) -> None:
    top = _top(registry, all_names, query)
    if first:
        assert top[0] == first, top
    for name in in_top:
        assert name in top[:3], top


@pytest.mark.parametrize(("query", "first", "in_top"), _TABLE, ids=[r[0] for r in _TABLE])
def test_a_broad_agents_allow_set_ranks_the_right_tool(
    registry: ToolRegistry,
    broad_agent_names: list[str],
    query: str,
    first: str,
    in_top: tuple[str, ...],
) -> None:
    top = _top(registry, broad_agent_names, query)
    if first:
        assert top[0] == first, top
    for name in in_top:
        assert name in top[:3], top


# ── The specific wrong answers, named ─────────────────────────────────


def test_browser_no_longer_wins_check_my_email(
    registry: ToolRegistry, broad_agent_names: list[str]
) -> None:
    """The measured defect: a three-way tie at 7.28 broken alphabetically, with
    `browser` ahead because its description contains the word "check"."""
    assert "browser" not in _top(registry, broad_agent_names, "check my email", n=3)


def test_reading_email_beats_reading_a_file(
    registry: ToolRegistry, broad_agent_names: list[str]
) -> None:
    """`_INTENT_VERBS` gave read_file and memory_block_read four points for the
    verb alone, with nothing in either about email."""
    top = _top(registry, broad_agent_names, "read my emails", n=3)
    assert "read_file" not in top, top
    assert "memory_block_read" not in top, top


def test_the_calendar_understands_the_word_meeting(
    registry: ToolRegistry, broad_agent_names: list[str]
) -> None:
    """ "meeting" appeared in no schema, so three calendar queries returned []."""
    for query in ("schedule a meeting", "cancel the meeting", "invite someone to a meeting"):
        top = _top(registry, broad_agent_names, query, n=3)
        assert any(t.startswith("gws_calendar_") for t in top), (query, top)


def test_listing_the_calendar_outranks_creating_an_event(
    registry: ToolRegistry, broad_agent_names: list[str]
) -> None:
    """ "what is on my calendar today" ranked create before list, which is a
    question answered by writing to the operator's calendar."""
    for query in ("what is on my calendar today", "what is on my schedule"):
        top = _top(registry, broad_agent_names, query, n=4)
        assert top.index("gws_calendar_list") < top.index("gws_calendar_create"), (query, top)


def test_the_reply_description_keeps_its_decisive_sentence(
    registry: ToolRegistry, broad_agent_names: list[str]
) -> None:
    """248 characters, cut at 200: the half that went was "Use this instead of
    gws_gmail_send for all replies"."""
    hits = {
        h["name"]: h["description"]
        for h in registry.search_tools(broad_agent_names, "reply to that email", limit=5)
    }
    assert "gws_gmail_send" in hits["gws_gmail_reply"], hits["gws_gmail_reply"]


def test_the_whose_calendar_sentence_survives_search(
    registry: ToolRegistry, broad_agent_names: list[str]
) -> None:
    """`gws_calendar_create`'s description was 596 characters, over
    `_SEARCH_DESC_MAX`, so search showed `when_to_use` alone — which said
    nothing about whose calendar. Under deferral `tool_search` IS the agent's
    discovery path, so the most important new fact in this change was absent
    from the first surface it reads."""
    hits = {
        h["name"]: h["description"]
        for h in registry.search_tools(broad_agent_names, "schedule a meeting", limit=5)
    }
    shown = hits["gws_calendar_create"]

    assert "OPERATOR" in shown
    assert "calendar='own'" in shown
    assert "invitations_requested" in shown


def test_no_gws_description_is_long_enough_to_be_cut_to_its_first_sentence(
    registry: ToolRegistry,
) -> None:
    """A description over the cap loses everything after `when_to_use` unless
    the fallback carries the rest — and for the calendar tools the rest is the
    whole point."""
    for name, schema in registry._schemas.items():
        if not name.startswith("gws_calendar_"):
            continue
        description = schema["function"]["description"]
        shown = registry._search_description(name, description)
        assert len(shown) >= min(len(description), 240), (name, len(shown))


@pytest.mark.parametrize(
    ("conjugated", "base"),
    [
        ("cancelled", "cancel"),
        ("cancelling", "cancel"),
        ("labelled", "label"),
        ("shipped", "ship"),
        ("planned", "plan"),
        ("referred", "refer"),
    ],
)
def test_a_doubled_consonant_meets_its_base_form(conjugated: str, base: str) -> None:
    """Minor 13: English doubles the final consonant before `-ed`/`-ing`, and
    the stemmer stripped the suffix while leaving the double — so `cancelled`
    became `cancell` and never met `cancel`."""
    from robothor.engine.tools.registry import _stem

    assert _stem(conjugated) == _stem(base), (conjugated, _stem(conjugated), _stem(base))


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("addressed", "address"),
        ("processed", "process"),
        ("passed", "pass"),
        ("called", "call"),
        ("settings", "setting"),
    ],
)
def test_collapsing_does_not_pull_real_words_apart(a: str, b: str) -> None:
    """The collapse is applied to EVERY word, the same way `_drop_silent_e` is,
    so the two sides cannot disagree about one — including the words where the
    double is not a suffix artefact at all (`address`, `pass`)."""
    from robothor.engine.tools.registry import _stem

    assert _stem(a) == _stem(b), (a, _stem(a), b, _stem(b))


def test_a_conjugated_intent_verb_still_earns_its_bonus(registry: ToolRegistry) -> None:
    """The observable half of Minor 13. "cancel the meeting" earned the intent
    bonus that picks `gws_calendar_delete` out of a family that all shares the
    noun; "cancelled the meeting" earned zero, and `cancell` was counted as a
    NOUN on top, polluting the object set the bonus is gated on."""
    from robothor.engine.tools.registry import (
        _intent_bonus,
        _object_terms,
        _query_terms,
        _stems,
    )

    fn = registry._schemas["gws_calendar_delete"]["function"]
    vocabulary = _stems(fn["name"]) | _stems(fn["description"])
    for keyword in fn.get("keywords", []):
        vocabulary |= _stems(keyword)

    scores = {}
    for query in ("cancel the meeting", "cancelled the meeting", "cancelling the meeting"):
        terms = _query_terms(query)
        scores[query] = _intent_bonus(vocabulary, terms, _object_terms(terms))

    assert len(set(scores.values())) == 1, scores
    assert all(v > 0 for v in scores.values()), scores


def test_the_stemmer_does_not_promise_idempotence(registry: ToolRegistry) -> None:
    """`_stem`'s docstring said the one-pass order made it idempotent. It does
    not: the `break` after the first verb suffix leaves a second one in place,
    so `proceedings` -> `proceed` while `proceed` -> `proc`.

    Nothing stems already-stemmed text — every caller starts from raw words —
    so this is a documentation defect, not a live one. The test pins the
    property that IS relied on (both sides transformed identically from raw
    text) and records the counterexample so the false claim cannot come back.
    """
    from robothor.engine.tools.registry import _stem

    assert _stem("proceedings") != _stem(_stem("proceedings"))
    assert _stem("scheduled") == _stem("schedule")
    assert _stem("meetings") == _stem("meeting")


def test_every_deciding_description_is_shown_whole(registry: ToolRegistry) -> None:
    """`_SEARCH_DESC_MAX`'s own comment says it was "chosen above the longest
    disambiguating description in the registry, not below it" — and it was not:
    `gws_calendar_create` stood at 411 against a 400 cap, so the one tool whose
    description this change exists to deliver was the one tool search cut.

    A tool carrying a `when_to_use` sentence is one we decided needed
    disambiguating. Its description is the text that does the disambiguating,
    so search shows it whole. This test is the enforcement: an edit that pushes
    one over fails here instead of silently losing its tail in a search hit.
    """
    over = {
        name: len(schema["function"]["description"])
        for name, schema in registry._schemas.items()
        if schema.get("function", {}).get("when_to_use")
        and len(schema["function"]["description"]) > ToolRegistry._SEARCH_DESC_MAX
    }
    assert not over, (
        f"over the {ToolRegistry._SEARCH_DESC_MAX}-char cap: {over}. Trim the "
        "description, or raise the cap and say why the longer hit still scans."
    )


def test_an_over_cap_description_still_leads_with_the_deciding_sentence(
    registry: ToolRegistry,
) -> None:
    """The fallback that keeps `when_to_use` first and fills the rest of the
    budget existed for exactly ONE real tool, by eleven characters — so the
    test above, which removes that overshoot, would have left the branch
    unreachable and unproven. It is a defensive path: it has to hold for
    whatever description a future edit writes, not for one accident of length.
    Hence a synthetic schema.
    """
    when = "Use this only when the thing you want is the second thing."
    rest = "Tail sentence that must not be lost entirely. " * 20
    description = f"{when} {rest}"
    assert len(description) > ToolRegistry._SEARCH_DESC_MAX

    registry._schemas["_fixture_tool"] = {
        "type": "function",
        "function": {
            "name": "_fixture_tool",
            "description": description,
            "when_to_use": when,
        },
    }
    try:
        shown = registry._search_description("_fixture_tool", description)
    finally:
        del registry._schemas["_fixture_tool"]

    assert shown.startswith(when), shown
    assert len(shown) <= ToolRegistry._SEARCH_DESC_MAX + 1, len(shown)
    # Not the deciding sentence alone — the budget left over carries some of
    # the rest, which is the whole reason this branch exists.
    assert len(shown) > len(when) + 100, shown


def test_the_crm_mail_tools_stop_masquerading_as_gmail(
    registry: ToolRegistry, broad_agent_names: list[str]
) -> None:
    """`get_inbox` is the agent's notification queue and `list_messages` is the
    CRM's ingested record; both came back for mail queries beside the Gmail
    tools with nothing to tell them apart."""
    for query in ("check my email", "read my emails", "any unread mail"):
        top = _top(registry, broad_agent_names, query, n=3)
        assert "get_inbox" not in top, (query, top)
        assert "list_messages" not in top, (query, top)

    hits = {
        h["name"]: h["description"]
        for h in registry.search_tools(broad_agent_names, "look at my inbox", limit=8)
    }
    if "get_inbox" in hits:
        assert "gws_gmail_search" in hits["get_inbox"], hits["get_inbox"]

    # "archive" is the single most common Gmail label action, and both CRM
    # tools carried it as a keyword — so they took positions 2 and 3 for it.
    for query in ("archive this", "archive that email"):
        top = _top(registry, broad_agent_names, query, n=3)
        assert "get_conversation" not in top, (query, top)
        assert "list_messages" not in top, (query, top)


# ── Capabilities this platform does not have ──────────────────────────


@pytest.mark.parametrize(
    ("query", "fragment"),
    [
        ("my google tasks", "no Google Tasks tool"),
        ("find a document in google drive", "no Google Drive tool"),
        ("open the google doc", "no Google Docs tool"),
        ("update the spreadsheet", "no spreadsheet tool"),
        ("add a google contact", "no Google Contacts tool"),
    ],
)
def test_an_absent_capability_says_so(registry: ToolRegistry, query: str, fragment: str) -> None:
    assert fragment in registry.absent_capability_note(query)


def test_an_ordinary_query_gets_no_absent_capability_note(registry: ToolRegistry) -> None:
    for query in ("check my email", "schedule a meeting", "read a file from disk"):
        assert registry.absent_capability_note(query) == ""


# ── The hint table itself ─────────────────────────────────────────────


def test_every_hinted_tool_is_registered(registry: ToolRegistry) -> None:
    """A vocabulary entry for a tool that does not exist ranks nothing and
    hides the fact that the tool is gone."""
    from robothor.engine.tools.keywords import TOOL_HINTS

    missing = sorted(set(TOOL_HINTS) - set(registry._schemas))
    assert missing == []


def test_every_tool_name_in_an_absent_capability_note_is_registered(
    registry: ToolRegistry,
) -> None:
    """These strings are handed straight to the model by `tool_search`.

    One of them named `search_people`, which is a CRM DAL function and not a
    registered tool — a brand-new phantom introduced by the change whose §5 was
    written about phantoms, in the one file that talks directly to the agent.
    `test_registered_tool_names.py` only scans deny tables, so nothing caught it.
    """
    import re

    from robothor.engine.tools.keywords import ABSENT_CAPABILITIES

    phantoms: dict[str, list[str]] = {}
    for triggers, note in ABSENT_CAPABILITIES:
        looks_like_a_tool = set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", note))
        missing = sorted(looks_like_a_tool - set(registry._schemas))
        if missing:
            phantoms["/".join(triggers)] = missing
    assert phantoms == {}


def test_an_absent_capability_note_agrees_with_the_ranking(
    registry: ToolRegistry, broad_agent_names: list[str]
) -> None:
    """The note said "the task tools here are the CRM's own (list_my_tasks,
    create_task)" while the ranking beside it put `approve_task` first — one
    tool result contradicting itself."""
    top = _top(registry, broad_agent_names, "my google tasks", n=3)
    note = registry.absent_capability_note("my google tasks")

    assert "list_my_tasks" in note
    assert top[0] == "list_my_tasks", top


def test_when_to_use_opens_the_description(registry: ToolRegistry) -> None:
    """The sentence search returns uncut has to be the one the model reads in
    the schema, or the two surfaces disagree about which tool to pick."""
    from robothor.engine.tools.keywords import TOOL_HINTS

    for name, hint in TOOL_HINTS.items():
        if not hint.when_to_use:
            continue
        description = registry._schemas[name]["function"]["description"]
        assert description.startswith(hint.when_to_use), name


def test_the_hint_keys_never_reach_the_wire(registry: ToolRegistry) -> None:
    """`keywords` and `when_to_use` are not part of the OpenAI function-schema
    contract; a provider that validates its request body rejects the call."""
    from robothor.engine.models import AgentConfig
    from robothor.engine.tools.registry import wire_schema

    config = AgentConfig(id="fixture-agent", name="Fixture", tools_allowed=sorted(GWS_TOOLS))
    for schema in registry.build_for_agent(config):
        assert set(schema["function"]) <= {"name", "description", "parameters"}, schema

    hinted = registry._schemas["gws_gmail_reply"]
    assert "keywords" in hinted["function"], "the registry copy keeps its vocabulary"
    assert "keywords" not in wire_schema(hinted)["function"]
