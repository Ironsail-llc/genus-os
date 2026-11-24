"""
Cross-Channel Ingestion Pipeline for Genus OS Memory System.

Accepts content from multiple channels (discord, email, cli, api, telegram)
and stores it through the one write path.

Architecture:
    Content + channel -> origin and scope (write_path.WriteContext)
        -> write_path.ingest_text: one extraction (facts with typed mentions,
           relations and event time) -> per fact: quality gate, conflict
           resolution, mentions, relations with evidence -> result
"""

from __future__ import annotations

import logging
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine.sanitize import sanitize_log
from robothor.memory import write_path

logger = logging.getLogger(__name__)

VALID_CHANNELS = [
    "discord",
    "email",
    "cli",
    "api",
    "telegram",
    "gchat",
    "voice",
    "mcp",
    "camera",
    "conversation",
    "crm",
]


async def ingest_content(
    content: str,
    source_channel: str,
    content_type: str,
    metadata: dict[str, Any] | None = None,
    tenant_id: str = "",
    *,
    role: str | None = None,
    read_only_context: str | None = None,
    scope: tuple[str, str | None] | None = None,
) -> dict[str, Any]:
    """Ingest content from any channel, extracting and storing facts.

    Args:
        content: The raw content to ingest.
        source_channel: Channel the content came from (discord, email, etc.).
        content_type: Type of content (conversation, email, decision, etc.).
        metadata: Optional channel-specific metadata (``session_key`` names the
            conversation the content belongs to).
        tenant_id: Tenant the content belongs to. Empty falls through to
            DEFAULT_TENANT, preserving behaviour for callers that never passed
            one.
        role: Who said it (``user``, ``assistant``, ``tool``), when known. It
            decides the facts' origin (see ``write_path.origin_for``).
        read_only_context: Background shown to the extraction model and never
            extracted from (the other side of a conversation).
        scope: ``(scope_kind, scope_id)`` for a caller that explicitly wants
            the facts private (``write_path.conversation_scope`` gives a
            conversation's owner). Absent, they are shared, as they always were.

    Returns:
        Dict with ingestion results including facts processed, the graph
        written, and whether extraction failed (the raw content is then stored).

    Raises:
        ValueError: If content is empty.
    """
    if not content or not content.strip():
        raise ValueError("Content cannot be empty")

    context = write_path.WriteContext.for_source(
        tenant_id=tenant_id or DEFAULT_TENANT,
        source_channel=source_channel,
        content_type=content_type,
        role=role,
        metadata=metadata,
        scope=scope,
    )
    logger.info(
        "Ingesting %s content (%d chars) via %s as %s",
        sanitize_log(content_type),
        len(content),
        sanitize_log(source_channel),
        context.origin,
    )
    result = await write_path.ingest_text(content, context, read_only_context=read_only_context)

    # Route verbatim reference values (account ids, phone numbers) to the
    # Knowledge Vault so they survive byte-for-byte. Gated, best-effort (WS-6).
    from robothor.memory.vault import _vault_populate_enabled, populate_vault_from_content

    vault_stored = 0
    if _vault_populate_enabled():
        try:
            vault_stored = len(
                await populate_vault_from_content(
                    content, source=source_channel, tenant_id=tenant_id
                )
            )
        except Exception as e:
            logger.warning("Vault auto-populate failed: %s", e)

    fact_ids = list(result.get("fact_ids") or [])
    return {
        "source_channel": source_channel,
        "content_type": content_type,
        "origin": context.origin,
        "scope_kind": context.scope_kind,
        "facts_processed": int(result.get("facts_processed", len(fact_ids))),
        "facts_skipped": int(result.get("facts_skipped", 0)),
        "facts_kept_confirmed": int(result.get("facts_kept_confirmed", 0)),
        "facts_refused": int(result.get("facts_refused", 0)),
        "fact_ids": fact_ids,
        "existing_ids": list(result.get("existing_ids") or []),
        "mentions_written": int(result.get("mentions_written", 0)),
        # Kept for callers that log it; mentions are what is written now.
        "entities_stored": int(result.get("mentions_written", 0)),
        "relations_stored": int(result.get("relations_stored", 0)),
        "relations_unresolved": int(result.get("relations_unresolved", 0)),
        "relations_private_deferred": int(result.get("relations_private_deferred", 0)),
        "extraction_failed": bool(result.get("extraction_failed", False)),
        "extraction_error": result.get("extraction_error"),
        "vault_stored": vault_stored,
    }
