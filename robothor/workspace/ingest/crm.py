"""New mail -> one CRM interaction per message.

The Google instance's sync script posts each new email to the bridge's
``/log-interaction`` with ``channel=email``, ``direction=incoming``, the
sender's name and address, and ``"<From>: '<subject>'"`` as the summary. The
Microsoft 365 mail ingestor sends the same body (:func:`interaction_payload`)
through an :data:`InteractionLogger`; the daemon's is
:func:`dal_interaction_logger`, which calls the bridge's own core
(:func:`robothor.crm.interactions.log_interaction`) in process.

Once per message: the ingestor marks ``crm:<message id>`` in the seen set
after a successful log and never logs a message whose key is there, so a
retried round or a resync does not log it again.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

__all__ = [
    "InteractionLogger",
    "crm_key",
    "dal_interaction_logger",
    "interaction_payload",
    "log_new_mail",
    "parse_sender",
]

_logger = logging.getLogger(__name__)

#: ``await log(payload)`` -> True when the CRM wrote the interaction.
InteractionLogger = Callable[[dict[str, Any]], Awaitable[bool]]

_NAMED = re.compile(r'"?([^"<]*?)"?\s*<([^>]+)>')
_BARE = re.compile(r"([^@\s]+@[^@\s]+)")


def crm_key(message_id: str) -> str:
    """The seen-set key that records a message as logged to the CRM."""
    return f"crm:{message_id}"


def parse_sender(from_field: str | None) -> tuple[str | None, str | None]:
    """``(name, address)`` from a From header; either may be ``None``.

    ``"Name" <a@b>`` and ``Name <a@b>`` give the name; a bare or bracketed
    address gives its local part as the name; anything else is a name only.
    """
    if not from_field:
        return None, None
    match = _NAMED.match(from_field)
    if match:
        name, address = match.group(1).strip(), match.group(2).strip()
        return name or address.split("@")[0], address
    match = _BARE.match(from_field)
    if match:
        address = match.group(1)
        return address.split("@")[0], address
    return from_field, None


def interaction_payload(entry: dict[str, Any]) -> dict[str, Any] | None:
    """The ``/log-interaction`` body for one email-log entry; ``None`` without a sender."""
    sender = entry.get("from")
    if not sender:
        return None
    name, address = parse_sender(str(sender))
    if not name:
        return None
    subject = entry.get("subject") or "(no subject)"
    return {
        "contact_name": name,
        "channel": "email",
        "direction": "incoming",
        "content_summary": f"{sender}: '{subject}'",
        "channel_identifier": address or str(sender),
    }


def dal_interaction_logger(
    tenant_id: str, *, source: str = "workspace_ingest"
) -> InteractionLogger:
    """An :data:`InteractionLogger` writing through the CRM in process, for ``tenant_id``.

    True only when the message row was written: a contact that did not
    resolve, or a message the CRM did not persist, is not "logged".
    """
    if not tenant_id:
        raise ValueError("explicit platform tenant required")

    def _write(payload: dict[str, Any]) -> dict[str, Any]:
        from robothor.crm.interactions import log_interaction
        from robothor.db.connection import tenant_scope

        with tenant_scope(tenant_id):
            return log_interaction(**payload, tenant_id=tenant_id, source=source)

    async def log(payload: dict[str, Any]) -> bool:
        result = await asyncio.to_thread(_write, payload)
        return bool(result.get("message_persisted"))

    return log


async def log_new_mail(
    entries: list[dict[str, Any]],
    *,
    log_interaction: InteractionLogger | None,
    seen: Callable[[list[str]], Awaitable[set[str]]],
    mark: Callable[[list[str]], Awaitable[None]],
    stamp: Callable[[], str],
    label: str,
) -> int:
    """One CRM interaction per new message, before it is published; the number logged.

    Before, so a round that dies between the two re-publishes next round
    without logging again (the ``crm:`` key is already seen). A CRM failure
    is logged by type and never holds the message back from the pipeline.
    ``label`` names the ingest in that log line.
    """
    if log_interaction is None or not entries:
        return 0
    keys = {str(e["id"]): crm_key(str(e["id"])) for e in entries}
    done = await seen(list(keys.values()))
    logged_count = 0
    for entry in entries:
        key = keys[str(entry["id"])]
        payload = interaction_payload(entry)
        if key in done or payload is None:
            continue
        try:
            logged = await log_interaction(payload)
        except Exception as exc:  # noqa: BLE001 - the CRM must never stop ingestion
            _logger.warning("%s mail CRM log deferred: %s", label, type(exc).__name__)
            continue
        if logged:
            await mark([key])
            entry["crmLoggedAt"] = stamp()
            logged_count += 1
    return logged_count
