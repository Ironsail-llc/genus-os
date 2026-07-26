"""Write-time quality gate for extracted facts.

WHY

Everything that reaches ``store_fact`` is stored. There is no bar. Measured on
the live table (25,910 active facts): 167 are under 25 characters, 25 exceed
1,000, 16 carry confidence below 0.3. Small numbers, but they are the visible
part — the extraction path also stores the entire raw input as a single "fact"
whenever extraction returns nothing, so a bad input becomes a bad row silently.

A retrieval system's precision is bounded by what it agreed to remember. Garbage
in the table is not neutral: it occupies the top-k, and the top-k is the whole
product.

WHY A LADDER, AND WHY SHADOW WRITES EVIDENCE

off -> shadow -> enforce, per docs/runbooks/GUARDRAIL_FLIPS.md. Shadow STORES
the fact and records what it would have rejected, so the rejection rate is
measured on real traffic before anything is refused. A shadow rung that merely
stays quiet proves nothing — this repo already has one flag whose "zero events"
evidence turned out to be vacuous because observe wrote nothing at all.

The rules are deliberately mechanical. An LLM judge on the write path would add
a second model call to a path whose p50 is already 63 seconds, and would need
its own error rate measured before it could be trusted to discard data.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Under this, it is a fragment, not a claim. Measured: 167 active rows.
MIN_CHARS = 25

# Over this it is a document. A "fact" this long cannot be superseded or
# contradicted coherently, and it swamps a top-k slot. Measured: 25 rows.
MAX_CHARS = 1000

MIN_CONFIDENCE = 0.3

# First-person agent narration is the single most common junk shape: the agent
# describing its own turn rather than recording something about the world.
_AGENT_CHATTER = re.compile(
    r"^\s*(i\s+(will|'ll|am going to|should|need to|have)\b"
    r"|let me\b|let's\b|okay[,.]|sure[,.]|here('s| is) (the|a)\b"
    r"|as an ai\b|i (cannot|can't|don't have)\b)",
    re.IGNORECASE,
)

# A claim needs a verb. No letters at all is a formatting artifact.
_HAS_LETTER = re.compile(r"[a-zA-Z]")


@dataclass(frozen=True)
class QualityVerdict:
    accept: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def quality_mode() -> str:
    """off | shadow | enforce. Default off."""
    mode = os.environ.get("MEMORY_QUALITY_GATE", "").strip().lower()
    return mode if mode in ("shadow", "enforce") else "off"


def score_fact(fact_text: str, *, confidence: float | None = None) -> QualityVerdict:
    """Pure. Would this text be accepted as a fact?

    Returns every failing reason rather than the first, so a shadow soak can
    report which rule is doing the work instead of only that something failed.
    """
    text = (fact_text or "").strip()
    reasons: list[str] = []

    if not text:
        reasons.append("empty")
    else:
        if len(text) < MIN_CHARS:
            reasons.append(f"too_short (<{MIN_CHARS} chars)")
        if len(text) > MAX_CHARS:
            reasons.append(f"too_long (>{MAX_CHARS} chars)")
        if not _HAS_LETTER.search(text):
            reasons.append("no_letters")
        if _AGENT_CHATTER.match(text):
            reasons.append("agent_chatter")

    if confidence is not None and confidence < MIN_CONFIDENCE:
        reasons.append(f"low_confidence (<{MIN_CONFIDENCE})")

    return QualityVerdict(accept=not reasons, reasons=tuple(reasons))


def record_shadow_rejection(fact_id: int, tenant_id: str, verdict: QualityVerdict) -> None:
    """Persist what the gate WOULD have refused.

    Shadow only means something if it leaves a trace. Uses memory_facts_audit,
    which is append-only and already indexed on (reason, snapshot_at), so the
    rejection rate is one GROUP BY away.

    Never raises — an audit failure must not fail the write it is auditing.
    """
    import json

    from robothor.db.connection import get_connection

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO memory_facts_audit "
                "(fact_id, tenant_id, fact_text, reason, snapshot) "
                "VALUES (%s, %s, %s, 'quality_would_reject', %s::jsonb)",
                (
                    fact_id,
                    tenant_id,
                    "",
                    json.dumps({"reasons": list(verdict.reasons), "source": "quality_gate"}),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("record_shadow_rejection failed for fact %s: %s", fact_id, exc)
