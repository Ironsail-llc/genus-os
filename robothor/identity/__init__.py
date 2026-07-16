"""Unified identity context — channel-native identifier → CRM-enriched identity.

Genus OS already verifies *access* (RBAC, audit) but historically the agent
never learned *who* it was talking to beyond a raw channel identifier. This
package is the platform seam every channel resolves through:

    from robothor.identity import resolve_identity, enrich_identity

    ctx = resolve_identity("telegram", telegram_user_id, tenant_id)
    if ctx is not None:
        enriched = enrich_identity(ctx)
        prompt_section = ctx.prompt_block(enriched)
"""

from __future__ import annotations

from robothor.identity.context import EnrichedIdentity, IdentityContext
from robothor.identity.enrichment import enrich_identity
from robothor.identity.resolvers import clear_cache, resolve_identity
from robothor.identity.scope import DataScope, scope_for

__all__ = [
    "DataScope",
    "EnrichedIdentity",
    "IdentityContext",
    "clear_cache",
    "enrich_identity",
    "resolve_identity",
    "scope_for",
]
