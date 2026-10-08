"""Log one interaction with a contact: the core behind the bridge's ``/log-interaction``.

Resolve the contact by its channel identifier (creating the person when the
CRM has never seen them), append the summary as a message to their newest
conversation (opening one when they have none), write an audit row and publish
``ipc.interaction`` on the ``crm`` stream.

The bridge endpoint calls this for HTTP callers; in-process callers (the
Microsoft 365 mail ingest, :mod:`robothor.workspace.ingest.crm`) call it
directly, so both write exactly the same rows.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["log_interaction"]


def log_interaction(
    *,
    contact_name: str,
    channel: str,
    direction: str,
    content_summary: str,
    channel_identifier: str | None,
    tenant_id: str,
    source: str = "bridge",
) -> dict[str, Any]:
    """Log the interaction; return the bridge's response body.

    ``message_persisted`` is ``None`` when no message was attempted (the
    contact did not resolve, or the summary is empty), ``False`` when one was
    attempted and NOT written.
    """
    from robothor.audit.logger import log_event
    from robothor.crm import dal
    from robothor.events.bus import publish

    if not tenant_id:
        raise ValueError("explicit platform tenant required")
    channel_id = channel_identifier or contact_name
    resolved = dal.resolve_contact(channel, channel_id, contact_name, tenant_id=tenant_id)
    person_id = resolved.get("person_id")
    message_persisted: bool | None = None
    if person_id and content_summary:
        convos = dal.get_conversations_for_contact(str(person_id), tenant_id=tenant_id)
        convo_id = convos[0].get("id") if convos else None
        if not convo_id:
            convo = dal.create_conversation(str(person_id), tenant_id=tenant_id)
            convo_id = convo.get("id") if convo else None
        if convo_id:
            msg_type = "incoming" if direction == "incoming" else "outgoing"
            # The result is CHECKED, not discarded. Between 2026-04-08 and
            # 2026-08-22 this call failed on every invocation (a uuid into an
            # integer PK) and the endpoint still answered 200 "ok", so four and
            # a half months of messages went missing with nothing to show for it.
            message_persisted = (
                dal.send_message(convo_id, content_summary, msg_type, tenant_id=tenant_id)
                is not None
            )
            if not message_persisted:
                logger.warning(
                    "log_interaction: message NOT persisted for conversation %s "
                    "(contact=%s channel=%s) — the interaction was accepted but "
                    "the message row was not written",
                    convo_id,
                    contact_name,
                    channel,
                )

    log_event(
        "ipc.interaction",
        f"log_interaction: {contact_name} via {channel}",
        category="bridge",
        source_channel=channel,
        target=f"person:{person_id}" if person_id else None,
        details={
            "contact_name": contact_name,
            "channel": channel,
            "direction": direction,
            "resolved": bool(person_id),
            "message_persisted": message_persisted,
            "tenant_id": tenant_id,
            "source": source,
        },
    )
    publish(
        "crm",
        "ipc.interaction",
        {
            "contact_name": contact_name,
            "channel": channel,
            "direction": direction,
            "person_id": person_id,
        },
        source=source,
        tenant_id=tenant_id,
    )
    return {
        "status": "ok",
        "contact": contact_name,
        "resolved": bool(person_id),
        "message_persisted": message_persisted,
    }
