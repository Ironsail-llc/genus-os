"""How a channel reaches back to somebody who spoke first.

Telegram and Slack can address a person from an id alone: a chat id is an
address. The Bot Framework family — Teams is the first channel here to use it —
cannot. A proactive message needs a **conversation reference**: the
tenant-specific ``serviceUrl`` the messages are POSTed to, and the conversation
id the platform minted. Both arrive only on an inbound activity, so a channel
that does not write them down can never start a conversation, reply to a
briefing thread, or send a pairing code.

Why this is not a column on ``user_channel_identities``
------------------------------------------------------
Two reasons, and the second is the one that matters.

*A reference exists before any grant does.* The first thing an unknown sender
gets is a pairing code, and that reply has to go somewhere. An identity row is
created only by :func:`robothor.engine.channels.identities.approve_pairing`,
from an operator-gated actor — so a table that could only hold references for
paired senders would make pairing itself undeliverable.

*A reference is routing, and routing is not authorization.* The values on this
row come from the sender's own platform. Putting them one column away from
``role`` and ``paired_by`` invites a write path that touches both, and the whole
point of migration 118 is that nothing reachable from an inbound message can
write the columns that decide what somebody may do. :data:`COLUMNS` is asserted
against that in the suite.

What is validated here
----------------------
``service_url`` must be HTTPS. It is the one field on the row an attacker would
most like to choose — the next proactive send carries a bearer token to it — and
while the inbound half also insists the *signed* ``serviceUrl`` claim matches the
activity, a store that would happily persist ``http://`` is one bug away from
shipping that token in the clear. Refused loudly, before any SQL runs.

**Sync, by design**, exactly like :mod:`robothor.engine.channels.identities`:
psycopg2, shared with the bridge and the CLI, and reached from async code
through ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

__all__ = [
    "COLUMNS",
    "TABLE",
    "ConversationRef",
    "record",
    "reference",
    "reference_for_target",
]

#: The table. Named once so a test can prove every statement stays inside it.
TABLE = "channel_conversation_refs"

#: Every column this module reads or writes. Deliberately inspectable: the
#: invariant "a reference carries no grant" is checkable rather than asserted in
#: a comment, and a future column called ``role`` fails the suite.
COLUMNS: tuple[str, ...] = (
    "tenant_id",
    "channel",
    "native_id",
    "conversation_id",
    "service_url",
    "display_name",
)


@dataclass(frozen=True)
class ConversationRef:
    """Where a channel sends when it wants to reach this sender again."""

    channel: str
    native_id: str
    conversation_id: str
    service_url: str
    display_name: str = ""
    tenant_id: str = DEFAULT_TENANT


def _clean_service_url(service_url: str) -> str:
    """The service URL, or a raise. HTTPS and a host, nothing else accepted."""
    value = (service_url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(
            "a conversation reference needs an https:// service URL with a host; "
            "refusing to store one a proactive send would carry a bearer token to"
        )
    return value


def record(
    channel: str,
    native_id: str,
    *,
    tenant_id: str = DEFAULT_TENANT,
    conversation_id: str,
    service_url: str,
    display_name: str = "",
) -> None:
    """Remember how to reach ``native_id`` on ``channel``.

    An upsert: the same person speaking again from a different conversation
    replaces the reference rather than accumulating rows, because the newest
    conversation is the one a reply belongs in and a stale ``serviceUrl`` is how
    a proactive send ends up posting into a tenant's old region endpoint.

    Raises:
        ValueError: for a non-HTTPS service URL or an empty conversation id.
            Both are refused BEFORE any SQL — a malformed reference that reached
            the table would be a send failure much later and much further away.
    """
    conversation = (conversation_id or "").strip()
    if not conversation:
        raise ValueError("a conversation reference needs a conversation id")
    url = _clean_service_url(service_url)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""INSERT INTO {TABLE}
                    (tenant_id, channel, native_id, conversation_id, service_url, display_name)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, channel, native_id) DO UPDATE SET
                    conversation_id = EXCLUDED.conversation_id,
                    service_url     = EXCLUDED.service_url,
                    display_name    = EXCLUDED.display_name,
                    updated_at      = NOW()""",  # noqa: S608 - TABLE is a module constant
            (tenant_id, channel, native_id, conversation, url, display_name or ""),
        )


def _row_to_ref(row: dict[str, Any] | None) -> ConversationRef | None:
    if not row:
        return None
    return ConversationRef(
        channel=str(row.get("channel") or ""),
        native_id=str(row.get("native_id") or ""),
        conversation_id=str(row.get("conversation_id") or ""),
        service_url=str(row.get("service_url") or ""),
        display_name=str(row.get("display_name") or ""),
        tenant_id=str(row.get("tenant_id") or DEFAULT_TENANT),
    )


def reference(
    channel: str, native_id: str, *, tenant_id: str = DEFAULT_TENANT
) -> ConversationRef | None:
    """The reference for one sender, or ``None``.

    ``None`` is a fact, not a failure: the channel turns it into a loud
    ``failed:<channel>_no_conversation_reference`` receipt. Guessing at a
    neighbouring conversation would post somebody's briefing into a room they
    did not choose.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} "  # noqa: S608 - module constants
            "WHERE tenant_id = %s AND channel = %s AND native_id = %s",
            (tenant_id, channel, native_id),
        )
        return _row_to_ref(cur.fetchone())


def reference_for_target(
    channel: str, target: str, *, tenant_id: str = DEFAULT_TENANT
) -> ConversationRef | None:
    """The reference a delivery target names, by sender id or conversation id.

    A manifest's ``delivery.to`` is written by an operator, who will name the
    room ("19:…@thread.v2") as readily as the person. Both are looked up; the
    sender id first, because that is the one that is unique per row.
    """
    clean = (target or "").strip()
    if not clean:
        return None
    found = reference(channel, clean, tenant_id=tenant_id)
    if found is not None:
        return found
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} "  # noqa: S608 - module constants
            "WHERE tenant_id = %s AND channel = %s AND conversation_id = %s "
            "ORDER BY updated_at DESC LIMIT 1",
            (tenant_id, channel, clean),
        )
        return _row_to_ref(cur.fetchone())
