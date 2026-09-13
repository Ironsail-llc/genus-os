"""Id-shape validation for CRM tool arguments — one gate, every dispatcher.

CRM ids are uuid (people, companies, notes, tasks, notifications) or serial
integers (conversations). An LLM-invented placeholder — ``task_jkl012``,
``bob.quill@example.com``, ``85105`` — reaches the typed SQL parameter
verbatim and raises ``psycopg2.errors.InvalidTextRepresentation``, which the
caller reports to the agent as a raw tool crash rather than something it can
act on.

The check lives here, in a module with no engine or DAL imports, because the
same tools are dispatched from **two** places:

* ``robothor.engine.tools.handlers.crm`` — the agent fleet's tool path,
* ``robothor.api.mcp.handle_tool_call`` — the MCP surface the operator's own
  Claude sessions use.

``get_task`` grew its own copy of this guard in 2026-08; its siblings in the
same file did not, and ``get_person`` / ``list_tasks`` crashed again on
2026-09-13. Guarding one dispatcher and leaving the other is the same mistake
one file over, so both call ``id_argument_error``.

The refusal is data, not an exception: a model can recover from "that is not a
UUID, use search_people" on its next turn, where a psycopg2 traceback burns an
iteration and can derail the run.
"""

from __future__ import annotations

import uuid
from typing import Any

#: The CRM tool surface this validator speaks for. The engine's handler
#: module guards every tool it registers (they are all CRM tools); the MCP
#: dispatcher handles memory, vision and tenancy tools alongside these, so it
#: needs to know where the CRM argument vocabulary applies. Kept honest by
#: test_crm_id_validation.py, which asserts this set equals the engine
#: handlers' registry — adding a handler without adding it here fails CI.
CRM_TOOLS: frozenset[str] = frozenset(
    {
        "ack_notification",
        "approve_task",
        "create_company",
        "create_message",
        "create_note",
        "create_person",
        "create_task",
        "delete_company",
        "delete_note",
        "delete_person",
        "delete_task",
        "get_company",
        "get_contact_360",
        "get_conversation",
        "get_inbox",
        "get_metadata_objects",
        "get_note",
        "get_object_metadata",
        "get_person",
        "get_task",
        "list_agent_tasks",
        "list_companies",
        "list_contact_messages",
        "list_conversations",
        "list_messages",
        "list_my_tasks",
        "list_notes",
        "list_people",
        "list_tasks",
        "list_tasks_summary",
        "merge_companies",
        "merge_contacts",
        "merge_people",
        "reject_task",
        "resolve_task",
        "search_records",
        "send_notification",
        "toggle_conversation_status",
        "update_company",
        "update_note",
        "update_person",
        "update_task",
    }
)

#: Arguments the CRM tools hand to uuid-typed SQL parameters.
UUID_ID_ARGS: frozenset[str] = frozenset(
    {
        "id",
        "personId",
        "companyId",
        "taskId",
        "parentTaskId",
        "keeperId",
        "loserId",
        "notificationId",
    }
)

#: Arguments that address a serial integer key (``crm_conversations.id``).
INT_ID_ARGS: frozenset[str] = frozenset({"conversationId"})

#: Id-looking names that are deliberately NOT row ids, kept explicit so the
#: drift test (test_crm_id_validation.py) can tell "classified as free text"
#: from "nobody thought about it":
#:
#: * ``agentId`` — an agent *name* (``main``, ``crm-hygiene``), defaulting to
#:   ``ctx.agent_id``.
#: * ``threadId`` / ``eventId`` / ``escalationId`` — dedup markers parsed out
#:   of a task *body* (``create_task``), carrying external provider ids.
NOT_AN_ID: frozenset[str] = frozenset({"agentId", "threadId", "eventId", "escalationId"})

#: Tools whose id argument is mandatory — absent or blank is itself the error.
#: Everything else treats a missing id as "no filter" (``list_tasks``'s
#: personId) or has its own fallback (``get_contact_360`` resolves an
#: ``identifier`` instead), so absence is left to the handler.
REQUIRED_ID_ARGS: dict[str, tuple[str, ...]] = {
    "get_person": ("id",),
    "update_person": ("id",),
    "delete_person": ("id",),
    "get_company": ("id",),
    "update_company": ("id",),
    "delete_company": ("id",),
    "get_note": ("id",),
    "update_note": ("id",),
    "delete_note": ("id",),
    "get_task": ("id",),
    "update_task": ("id",),
    "delete_task": ("id",),
    "resolve_task": ("id",),
    "approve_task": ("id",),
    "reject_task": ("id",),
    "ack_notification": ("notificationId",),
    "merge_people": ("keeperId", "loserId"),
    "merge_contacts": ("keeperId", "loserId"),
    "merge_companies": ("keeperId", "loserId"),
    "get_conversation": ("conversationId",),
    "list_messages": ("conversationId",),
    "create_message": ("conversationId",),
    "toggle_conversation_status": ("conversationId",),
}

#: (marker, subject, where to find a real one). Matched in order against the
#: argument name — or against the tool name for the bare ``id`` and for the
#: merge arguments, which is where the subject actually lives.
_ID_SUBJECTS: tuple[tuple[str, str, str], ...] = (
    ("notification", "notification", "get_inbox"),
    ("conversation", "conversation", "list_conversations"),
    ("contact", "person", "search_people or list_people"),
    ("people", "person", "search_people or list_people"),
    ("person", "person", "search_people or list_people"),
    ("compan", "company", "list_companies"),
    ("note", "note", "list_notes"),
    ("task", "task", "list_tasks or list_my_tasks"),
    ("message", "conversation", "list_conversations"),
)

#: Tools that accept a non-id way in, appended to the error so the model sees
#: the route that would have worked. ``get_contact_360`` takes an email or
#: phone as ``identifier``; being told "use search_people" when an email is
#: exactly what it can resolve is advice that walks past the answer.
_ALTERNATIVE_ROUTE: dict[str, str] = {
    "get_contact_360": " — or pass identifier='<email or phone>' with channel='email'",
}

#: ``uuid.UUID`` accepts the RFC 4122 URN form; Postgres's uuid input does
#: not, so a ``urn:uuid:…`` value would pass the guard and then crash at the
#: SQL boundary — the one thing this module exists to prevent. Braced,
#: upper-case and 32-hex-no-dash forms are accepted by both, so they pass.
_URN_PREFIX = "urn:uuid:"


def id_subject(tool: str, arg: str) -> tuple[str, str]:
    """What kind of record an id argument names, and how to find a real one."""
    key = tool if arg in ("id", "keeperId", "loserId") else arg
    lowered = key.lower()
    for marker, subject, finder in _ID_SUBJECTS:
        if marker in lowered:
            return subject, finder
    return "record", "a list_* tool"


def id_argument_error(tool: str, args: dict[str, Any]) -> dict[str, str] | None:
    """The error dict for the first unusable id in ``args``, else ``None``.

    Surrounding whitespace is stripped **in place** before the shape check, so
    an id copied out of a previous tool result with a trailing newline is
    accepted and reaches SQL in the form Postgres takes — rather than being
    refused with a message quoting a value that looks perfectly valid.
    """
    required = REQUIRED_ID_ARGS.get(tool, ())
    seen: set[str] = set()
    for arg in (*required, *(a for a in args if a in UUID_ID_ARGS or a in INT_ID_ARGS)):
        if arg in seen:
            continue
        seen.add(arg)

        value = args.get(arg)
        if isinstance(value, str) and value != value.strip():
            value = args[arg] = value.strip()

        subject, finder = id_subject(tool, arg)
        expected = "an integer id" if arg in INT_ID_ARGS else "a UUID"
        route = _ALTERNATIVE_ROUTE.get(tool, "")

        if value is None or (isinstance(value, str) and not value):
            if arg not in required:
                continue  # an optional filter that was simply not passed
            return {
                "error": (
                    f"{tool} needs a {subject} id — expected {expected}; "
                    f"use {finder} to find real {subject} ids{route}"
                )
            }

        if not _is_usable(arg, value):
            return {
                "error": (
                    f"{subject} id {value!r} is not a valid id — expected {expected}; "
                    f"use {finder} to find real {subject} ids{route}"
                )
            }
    return None


def _is_usable(arg: str, value: Any) -> bool:
    """Whether ``value`` will survive the typed SQL parameter it is bound to."""
    text = str(value)
    try:
        if arg in INT_ID_ARGS:
            int(text)
        else:
            if text.lower().startswith(_URN_PREFIX):
                return False  # parses in Python, rejected by Postgres
            uuid.UUID(text)
    except (TypeError, ValueError):
        return False
    return True
