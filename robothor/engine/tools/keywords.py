"""What a tool is FOR, in the words an operator would use for it.

A tool schema is written for the model that will call it, so its description
says what the tool does in the platform's vocabulary: ``gws_gmail_search``
"searches Gmail messages". Nobody asks for that. They ask to "check my email",
to "schedule a meeting", to "cancel the meeting" — and none of "email",
"meeting" or "cancel" appears anywhere in the schemas those requests should
reach. Measured on 2026-09-16 against the live registry:

    'check my email'              -> browser, gws_gmail_reply, gws_gmail_send
    'read my emails'              -> memory_block_read, read_file, ...
    'cancel the meeting'          -> []

``browser`` won the first because its description contains "check" and
"email" is not a substring of ``gws_gmail_*``. The calendar ones returned
nothing at all. Under deferred tool loading (Rip 16) ``tool_search`` is the
ONLY way the agent reaches a Gmail or Calendar tool, so a ranking that cannot
find them is the difference between replying to an email and shelling out to a
CLI the instance docs ban.

This module is the vocabulary layer: one table, keyed by registered tool name,
carrying

* ``keywords`` — the words a person uses for this capability. The ranker
  treats a keyword hit like a hit on the tool's own name, so "email" reaches
  ``gws_gmail_*`` and "meeting" reaches ``gws_calendar_*``.
* ``rank_bias`` — a tie-breaking nudge between siblings whose vocabulary is
  necessarily identical.
* ``when_to_use`` — one sentence, the FIRST sentence of the tool's
  description, for the tools that compete with a sibling (reply vs send, list
  vs create). Search returns it whole, so the disambiguating sentence is never
  the half that the length cap drops.

Why a table here rather than the keywords inline in each schema: the CRM mail
tools (``list_messages``, ``get_conversation``, ``get_inbox``) come from
``robothor.api.mcp.get_tool_definitions()``, not from ``get_engine_schemas()``,
and they were the other half of the defect — they rank for mail queries while
being ingested CRM conversations and the agent's own notification queue. One
table reaches both sources; two tables would drift.

Nothing here reaches the wire. :func:`robothor.engine.tools.registry.builtin_schemas`
stamps these onto the registry's copy of each schema, and the registry strips
them again before the schemas are advertised to a model, because ``keywords``
and ``when_to_use`` are not part of the OpenAI function-schema contract and a
strict provider rejects a request carrying an unknown key.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "ABSENT_CAPABILITIES",
    "TOOL_HINTS",
    "ToolHint",
    "absent_capability_note",
    "hint_for",
]


@dataclass(frozen=True)
class ToolHint:
    """The search vocabulary for one registered tool."""

    #: Single lowercase words a person would use for this capability. Matched
    #: after the same crude singularisation the query gets, so "email" covers
    #: "emails" and "meeting" covers "meetings" — list the singular.
    keywords: tuple[str, ...] = field(default_factory=tuple)

    #: One sentence saying when to pick THIS tool over its sibling. Must be the
    #: opening sentence of the tool's own description (a test asserts it), so
    #: the model reads it in the schema and search can return it uncut.
    when_to_use: str = ""

    #: A small nudge, added to the score of a tool that already matched.
    #:
    #: A family's tools necessarily share a vocabulary — every Gmail tool is
    #: about email — so the word "email" cannot order them and the tie fell out
    #: alphabetically, putting ``gws_gmail_get`` (which needs a message id the
    #: agent does not have yet) ahead of ``gws_gmail_search`` (which needs only
    #: a query). Positive for the one tool in a family an agent holding no ids
    #: must call first. Deliberately smaller than a single keyword hit, so it
    #: breaks near-ties and decides nothing else.
    rank_bias: float = 0.0


# ── The table ─────────────────────────────────────────────────────────
#
# Keywords are the *object* words as much as the verb words: the ranker will
# not credit an intent verb ("read", "check", "cancel") to a tool whose
# vocabulary does not mention the object the query named, which is what kept
# `read_file` and `memory_block_read` ahead of Gmail for "read my emails".

_MAIL: tuple[str, ...] = ("email", "mail", "gmail", "message")
_CALENDAR: tuple[str, ...] = (
    "calendar",
    "meeting",
    "event",
    "appointment",
    "schedule",
    "call",
)
_CHAT: tuple[str, ...] = ("chat", "space", "room", "hangout")

TOOL_HINTS: dict[str, ToolHint] = {
    # ── Live Gmail ────────────────────────────────────────────────────
    "gws_gmail_search": ToolHint(
        keywords=(
            *_MAIL,
            "inbox",
            "unread",
            "new",
            "sender",
            "sent",
            "received",
            "from",
            "read",
            "check",
            "search",
            "find",
            "thread",
            "sender",
        ),
        rank_bias=1.0,
        when_to_use=(
            "Use this first for any question about live email — it is the only tool that "
            "finds messages in the real mailbox."
        ),
    ),
    "gws_gmail_get": ToolHint(
        keywords=(*_MAIL, "read", "open", "body", "text", "thread", "attachment", "content"),
        when_to_use=(
            "Use this when you already have a message or thread id and need the text of "
            "the email itself."
        ),
    ),
    "gws_gmail_reply": ToolHint(
        keywords=(*_MAIL, "reply", "respond", "answer", "thread", "follow"),
        when_to_use="Use this instead of gws_gmail_send for every reply to an existing thread.",
    ),
    "gws_gmail_send": ToolHint(
        keywords=(
            *_MAIL,
            "send",
            "compose",
            "write",
            "draft",
            "forward",
            "outbound",
            "cc",
            "bcc",
            "recipient",
        ),
        when_to_use=(
            "Use this only to start a NEW conversation; for anything that answers an "
            "existing thread use gws_gmail_reply, which threads it correctly."
        ),
    ),
    "gws_gmail_modify": ToolHint(
        keywords=(*_MAIL, "label", "archive", "mark", "star", "move", "spam"),
        when_to_use=(
            "Use this to change an email's state — mark read or unread, archive, add or "
            "remove labels — never to read or send one."
        ),
    ),
    # ── Calendar ──────────────────────────────────────────────────────
    "gws_calendar_list": ToolHint(
        keywords=(
            *_CALENDAR,
            "agenda",
            "today",
            "tomorrow",
            "week",
            "busy",
            "free",
            "availability",
            "upcoming",
        ),
        rank_bias=1.0,
        when_to_use=(
            "Use this to READ the calendar — what is on it today, when someone is free, "
            "or to find the event id another calendar tool needs."
        ),
    ),
    "gws_calendar_create": ToolHint(
        keywords=(*_CALENDAR, "book", "invite", "attendee", "invitation", "arrange", "set"),
        when_to_use=(
            "Use this only to put a NEW event on the OPERATOR's calendar, which is where "
            "it goes by default — pass calendar='own' for your own, which the operator "
            "never sees."
        ),
    ),
    "gws_calendar_delete": ToolHint(
        keywords=(*_CALENDAR, "cancel", "delete", "remove", "drop"),
        when_to_use=(
            "Use this to cancel or remove an event you already have the id for; find the "
            "id with gws_calendar_list."
        ),
    ),
    # ── Google Chat ───────────────────────────────────────────────────
    "gws_chat_send": ToolHint(
        keywords=(*_CHAT, "send", "post", "message", "notify"),
        when_to_use=(
            "Use this for Google Chat spaces only — it is not email and not the "
            "operator's own channel."
        ),
    ),
    "gws_chat_list_spaces": ToolHint(
        keywords=(*_CHAT, "list", "member", "channel"),
    ),
    "gws_chat_list_messages": ToolHint(
        keywords=(*_CHAT, "message", "history", "read", "conversation", "thread"),
    ),
    # ── CRM-ingested mail: NOT the live mailbox ───────────────────────
    #
    # These three came back for mail queries and for "look at my inbox" beside
    # the Gmail tools with nothing to tell them apart. Their vocabulary
    # deliberately omits "email", "mail" and "gmail": what they hold is the
    # CRM's ingested record of a conversation, which can be days stale and
    # never contains an unread message. The when_to_use sentence names the
    # tool to use instead, because search returns it whole. "contact" is not
    # among them either: "add a contact" is a CRM person, not a conversation —
    # and neither is "archive", which in operator speech is the Gmail label
    # action and never a CRM noun. With it here, "archive that email" returned
    # these two at positions 2 and 3, which is the masquerade this table exists
    # to end.
    "list_messages": ToolHint(
        keywords=("crm", "conversation", "correspondence", "history", "record", "ingested"),
        when_to_use=(
            "Use this for the CRM's stored record of past correspondence with a contact; "
            "for live email use gws_gmail_search."
        ),
    ),
    "get_conversation": ToolHint(
        keywords=("crm", "conversation", "transcript", "history", "record", "ingested"),
        when_to_use=(
            "Use this for one stored CRM conversation thread; for a live Gmail thread use "
            "gws_gmail_get."
        ),
    ),
    # ── Tasks: a family of eight that ties on the noun ──
    #
    # "my google tasks" put `approve_task` first — eight *_task tools tie on
    # the `task` keyword with no intent verb to separate them, and the answer
    # fell out alphabetically. The same entry-point rule the mail and calendar
    # families use: the tool an agent with no task id must call first wins the
    # tie. It also stops the absent-capability note, which names list_my_tasks,
    # from contradicting the ranking printed beside it.
    "list_my_tasks": ToolHint(
        keywords=("task", "todo", "queue", "assigned", "work", "backlog"),
        rank_bias=1.0,
    ),
    "get_inbox": ToolHint(
        keywords=("notification", "alert", "agent", "queue", "pending", "unacked"),
        when_to_use=(
            "Use this for the agent's own notification queue, which is not email; for the "
            "operator's mailbox use gws_gmail_search."
        ),
    ),
    # ── Images: one picture, or a folder of them ──────────────────────
    #
    # Two tools that necessarily share a vocabulary, and the reason this table
    # exists: under deferred loading `tool_search` is how the agent reaches
    # either, and "sort these photos" matched neither name. No `rank_bias` on
    # either, because the vocabularies already separate — "sort", "label",
    # "categorise", "many" and "each" reach only the batch tool, while "look",
    # "read", "chart" and "diagram" reach only the single one. A bias here
    # would distort a query the keywords already answer.
    "view_image": ToolHint(
        keywords=(
            "image",
            "picture",
            "photo",
            "screenshot",
            "chart",
            "diagram",
            "graph",
            "figure",
            "scan",
            "look",
            "see",
            "view",
            "show",
            "read",
            "visual",
            "png",
            "jpg",
            "jpeg",
        ),
        when_to_use="Look at ONE image file — a photo, screenshot, chart, diagram or scan.",
    ),
    "analyze_image": ToolHint(
        keywords=(
            "image",
            "picture",
            "photo",
            "screenshot",
            "sort",
            "label",
            "categorise",
            "categorize",
            "classify",
            "filter",
            "search",
            "folder",
            "batch",
            "many",
            "each",
            "which",
            "caption",
            "describe",
            "visual",
            "png",
            "jpg",
            "jpeg",
        ),
        when_to_use="Ask one question about up to 200 images at once.",
    ),
    # The words a model reaches for when it is ABOUT to write fifty turns of
    # the same call. Measured 2026-09-16: the three benchmark tasks we scored
    # zero on are the three where the competing harness looped its tools from
    # inside code, and none of "loop", "batch", "for each" or "every" appeared
    # anywhere in a schema that could have answered.
    "execute_code": ToolHint(
        keywords=(
            "loop",
            "batch",
            "each",
            "every",
            "many",
            "bulk",
            "iterate",
            "repeat",
            "script",
            "python",
            "code",
            "programmatic",
            "automate",
            "ids",
            "list",
            "fetch",
            "process",
            "parse",
            "transform",
        ),
        when_to_use=(
            "Use this when the SAME tool call repeats over many items — loop over ids, "
            "fetch each page, check every file."
        ),
    ),
}


# ── Capabilities this platform does not have ──────────────────────────
#
# An agent that searches for a Drive or Sheets tool gets whatever ranked
# least badly — `find_procedure` for "find a document in google drive",
# CRM list_* tools for "my google tasks" — and then calls it. Saying so
# outright is cheaper than any ranking change, and it is the same answer
# docs/TOOLS.md gives a human.
#: ``(trigger words, the sentence search adds)``. Every trigger must match for
#: the note to fire, so "drive" alone does not accuse a disk of being Google's.
ABSENT_CAPABILITIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("google", "drive"),
        "There is no Google Drive tool on this platform. Use exec with a Workspace CLI.",
    ),
    (
        ("google", "doc"),
        "There is no Google Docs tool on this platform. Use exec with a Workspace CLI.",
    ),
    (
        ("google", "sheet"),
        "There is no Google Sheets tool on this platform. Use exec with a Workspace CLI.",
    ),
    (
        ("google", "task"),
        "There is no Google Tasks tool on this platform. The task tools here are the "
        "CRM's own — list_my_tasks to read them, create_task to add one. Use exec "
        "with a Workspace CLI for Google Tasks.",
    ),
    (
        ("google", "contact"),
        "There is no Google Contacts tool on this platform. The contact tools here are "
        "the CRM's own (list_people, get_person, create_person). Use exec with a "
        "Workspace CLI.",
    ),
    (
        ("spreadsheet",),
        "There is no spreadsheet tool on this platform. Use exec with a CLI, or "
        "read_file / write_file for a CSV.",
    ),
)


def hint_for(name: str) -> ToolHint | None:
    """The search vocabulary for one tool, or ``None`` if it has none."""
    return TOOL_HINTS.get(name)


def absent_capability_note(query: str) -> str:
    """A sentence naming a capability this platform does not have, or ``""``.

    Matched on the raw lowercased query rather than the ranker's content words
    so that "docs" and "document" both reach the Docs answer.
    """
    lowered = query.lower()
    for triggers, note in ABSENT_CAPABILITIES:
        if all(trigger in lowered for trigger in triggers):
            return note
    return ""
