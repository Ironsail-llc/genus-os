"""Tier 1 context budget: enforce ``max_chars``, and measure what it costs.

THE PROBLEM

``max_chars`` is written at seed time by ``blocks.DEFAULT_BLOCK_SEEDS`` and read
by nothing. Measured on the live table:

    2,492 blocks
    2,092 never read even once
      130 exceed their own declared budget
   57,051 characters in the largest single block (~14k tokens)
    7.1 M characters total

A column that records a limit nobody checks is worse than no column: it gives
the appearance of a budget with none of the effect. Memory blocks are the
always-loaded tier, so every character here is paid on every single turn — this
is the number the whole overhaul is trying to move, and until now it was not
even computed.

The field has settled this. Claude Code enforces 200 lines / 25 KB on its
always-loaded index and *rejects* over-budget writes. Cline's eager load reaches
~300k tokens after five iterations and is conceded as a defect.

WHY REJECT RATHER THAN TRUNCATE

Truncation drops the end of a block, which is where the most recent context
lives, and does it without telling anyone. A rejected write is visible and the
caller can summarise; a truncated one is silent corruption.

WHY A LADDER

130 blocks are already over budget. Flipping straight to enforce would start
failing real writes immediately, so ``MEMORY_BLOCK_BUDGET`` runs
off -> observe -> enforce, per docs/runbooks/GUARDRAIL_FLIPS.md. Observe is not
silent: it returns ``over_budget: True`` on the write result, so the soak has
evidence rather than an absence of evidence.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# NULL max_chars must not mean "unlimited" — that is how a 57,051-character
# always-loaded block came to exist. ~2k tokens is a generous ceiling for a
# block that is paid on every turn.
DEFAULT_MAX_CHARS = 8000

# Rough and deliberately model-agnostic. The exact tokenizer varies by provider
# and the decision this feeds — "is one block eating the context window" — does
# not turn on a 10% error.
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class BudgetVerdict:
    """Immutable so a caller cannot flip `over` and write anyway."""

    over: bool
    content_chars: int
    max_chars: int
    overflow_chars: int
    reason: str


def budget_mode() -> str:
    """off | observe | enforce. Default off — 2,492 live blocks."""
    mode = os.environ.get("MEMORY_BLOCK_BUDGET", "").strip().lower()
    return mode if mode in ("observe", "enforce") else "off"


def estimate_tokens(text: str) -> int:
    return max(0, len(text or "") // _CHARS_PER_TOKEN)


def check_budget(content: str, *, max_chars: int | None) -> BudgetVerdict:
    """Pure. Is this content within the block's declared budget?

    A max_chars of None, 0 or negative is treated as unset and falls back to
    DEFAULT_MAX_CHARS — a misconfigured 0 must not reject every write, and a
    NULL must not permit an unbounded one.
    """
    limit = max_chars if (max_chars or 0) > 0 else DEFAULT_MAX_CHARS
    size = len(content or "")
    overflow = max(0, size - limit)
    return BudgetVerdict(
        over=overflow > 0,
        content_chars=size,
        max_chars=limit,
        overflow_chars=overflow,
        reason=(
            f"content is {size} chars, budget is {limit} — over by {overflow}"
            if overflow
            else f"content is {size} chars, within budget {limit}"
        ),
    )


def tier_token_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-tier token accounting for the always-loaded memory blocks.

    This is the metric the overhaul optimises and it did not previously exist,
    which is why a 14k-token block could sit in the always-loaded tier
    indefinitely without anyone seeing a number go up.

    ``rows`` are dicts with block_name, block_type, content and optionally
    max_chars — taken as an argument rather than queried so the arithmetic is
    testable without a database.
    """
    by_tier: dict[str, dict[str, int]] = {}
    over_budget: list[dict[str, Any]] = []

    for row in rows:
        tier = str(row.get("block_type") or "untyped")
        content = row.get("content") or ""
        bucket = by_tier.setdefault(tier, {"blocks": 0, "tokens": 0, "chars": 0})
        bucket["blocks"] += 1
        bucket["tokens"] += estimate_tokens(content)
        bucket["chars"] += len(content)

        verdict = check_budget(content, max_chars=row.get("max_chars"))
        if verdict.over:
            over_budget.append(
                {
                    "block_name": row.get("block_name"),
                    "block_type": tier,
                    "content_chars": verdict.content_chars,
                    "max_chars": verdict.max_chars,
                    "overflow_chars": verdict.overflow_chars,
                }
            )

    over_budget.sort(key=lambda b: b["overflow_chars"], reverse=True)
    return {
        "by_tier": by_tier,
        "total_tokens": sum(t["tokens"] for t in by_tier.values()),
        "total_chars": sum(t["chars"] for t in by_tier.values()),
        "total_blocks": len(rows),
        "over_budget": over_budget,
        "mode": budget_mode(),
    }


def live_tier_report(tenant_id: str | None = None) -> dict[str, Any]:
    """``tier_token_report`` over the real table."""
    from robothor.db.connection import get_connection

    sql = "SELECT block_name, block_type, content, max_chars FROM agent_memory_blocks"
    params: list[Any] = []
    if tenant_id:
        sql += " WHERE tenant_id = %s"
        params.append(tenant_id)

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
    return tier_token_report(rows)
