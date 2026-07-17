"""Unified identity context — the shared shape every channel resolves to.

``IdentityContext`` is the answer to "who is the operator talking to right
now, and how sure are we?" Every channel (webchat, Telegram, vision, ...)
resolves its own native identifier down to this one shape via
``robothor.identity.resolvers.resolve_identity``, so the rest of the platform
(prompt assembly, permissions, audit) only ever has to reason about one
identity model instead of one per channel.

``EnrichedIdentity`` is optional CRM/memory-graph context (company, job
title, known relationships, interaction history) layered on top when a
``person_id`` is resolvable — see ``robothor.identity.enrichment``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IdentityContext:
    """A channel-resolved identity for the human on the other end of a run.

    ``verified`` distinguishes a DB/crypto-verified identity (webchat session,
    a registered Telegram user) from a fabricated or merely probabilistic one
    (an unrecognized vision face label) — callers must not extend privileged
    trust to an unverified identity.
    """

    tenant_id: str
    channel: str  # "webchat" | "telegram" | "vision" | "cli" | "service"
    identifier: str  # channel-native id (user_accounts.id, telegram user id, face label)
    verified: bool
    display_name: str = ""
    role: str = ""
    user_account_id: str | None = None
    tenant_user_id: str | None = None  # stable tenant_users.user_id
    person_id: str | None = None
    email: str | None = None

    def prompt_block(self, enriched: EnrichedIdentity | None = None) -> str:
        """Render the ``--- CURRENT USER ---`` prompt section.

        Optional lines (affiliation, relationships, history) only appear when
        ``enriched`` carries that data. An unverified identity always gets an
        explicit trailing warning — the LLM must not treat a probabilistic
        vision match the way it treats a signed-in webchat session.
        """
        lines = ["--- CURRENT USER ---"]
        lines.append(f"Name: {self.display_name or self.identifier}")
        lines.append(
            f"Role: {self.role or 'unknown'} | Channel: {self.channel} | "
            f"Verified: {'yes' if self.verified else 'NO'}"
        )

        if enriched and (enriched.job_title or enriched.company):
            affiliation = ", ".join(p for p in (enriched.job_title, enriched.company) if p)
            lines.append(f"Affiliation: {affiliation}")

        if enriched and enriched.relationships:
            lines.append(f"Known relationships: {'; '.join(enriched.relationships)}")

        if enriched and enriched.last_touched_at:
            total = sum(enriched.activity_counts.values())
            lines.append(f"History: {total} prior interactions, last {enriched.last_touched_at}")

        lines.append(
            "Address them by name. Do not conflate them with other people sharing the same name."
        )

        if not self.verified:
            lines.append(
                "Identity is NOT verified. Do not disclose private information "
                "or take privileged actions on their behalf."
            )

        return "\n".join(lines)


@dataclass(frozen=True)
class EnrichedIdentity:
    """CRM/memory-graph enrichment for a resolved identity's ``person_id``."""

    company: str | None = None
    job_title: str | None = None
    relationships: tuple[str, ...] = ()  # e.g. "colleague_of → <name>"
    last_touched_at: str | None = None  # ISO 8601
    activity_counts: dict[str, int] = field(default_factory=dict)
