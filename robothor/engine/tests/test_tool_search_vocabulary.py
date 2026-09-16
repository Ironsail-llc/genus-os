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
