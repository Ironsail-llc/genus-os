"""Search-degradation detector: page when web search runs on fallbacks.

Live, 2026-09-14: every one of the day's 29 ``web_search`` calls came back
``fallback_from: searxng`` (three of SearXNG's engines IP-blocked, no Brave
key configured) and nobody was told until the operator asked for a bakery
and got Wikipedia. Degraded search is a silent failure mode — the tool still
"returns results". This detector reads what the tool itself recorded in
``agent_run_steps`` and pages before the operator finds out the hard way.

Runs from the daemon's outage-detector tick beside ``tool_outage_detector``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.engine.detectors import _SLOW_DEDUP_TTL_SECONDS, _should_fire, detectors_enabled

logger = logging.getLogger(__name__)


def check_search_degradation(
    hours: int = 6,
    min_calls: int = 3,
    ratio: float = 0.5,
) -> dict[str, Any] | None:
    """The share of recent ``web_search`` calls that did not come from a
    primary provider, or None when search is healthy (or unused).

    Thresholds are applied in Python so they stay testable without a database.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE tool_output::text LIKE %(fallback)s
                       OR tool_output::text LIKE %(degraded)s
                       OR tool_output::text LIKE %(failed)s
                ) AS degraded,
                COUNT(*) FILTER (WHERE tool_output::text LIKE %(brave)s) AS via_brave
            FROM agent_run_steps
            WHERE tool_name = 'web_search'
              AND started_at > NOW() - make_interval(hours => %(hours)s)
            """,
            {
                "hours": hours,
                "fallback": "%fallback_from%",
                "degraded": '%"degraded"%',
                "failed": "%Search failed%",
                "brave": '%"provider": "brave"%',
            },
        )
        row = dict(cur.fetchone() or {})

    total = int(row.get("total") or 0)
    degraded = int(row.get("degraded") or 0)
    if total < min_calls:
        return None
    share = degraded / total
    if share < ratio:
        return None
    return {
        "total": total,
        "degraded": degraded,
        "share": round(share, 3),
        "via_brave": int(row.get("via_brave") or 0),
        "brave_key_set": bool(os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()),
        "hours": hours,
    }


def _severity(found: dict[str, Any]) -> str:
    """No API provider and (nearly) everything degraded is the outage shape the
    operator hit; with a key present it is a warning that the chain is limping."""
    if found["share"] >= 0.9 and not found["brave_key_set"]:
        return "critical"
    return "warning"


def _body(found: dict[str, Any]) -> str:
    head = (
        f"{found['degraded']}/{found['total']} web_search calls in the last {found['hours']}h "
        f"({found['share'] * 100:.0f}%) came from a fallback or failed; {found['via_brave']} used "
        "the Brave API.\n"
    )
    if not found["brave_key_set"]:
        return head + (
            "BRAVE_SEARCH_API_KEY is NOT set — search has no API provider and every call "
            "scrapes engines that block data-center IPs. Set the key (docs/configuration.md)."
        )
    return head + (
        "A Brave key is set; check the engine log for 'Brave search failed' / 429s and "
        "SearXNG's unresponsive_engines."
    )


async def search_degradation_detector() -> int:
    """Page when web search has been running on fallbacks. Returns alerts fired."""
    if not detectors_enabled():
        return 0
    try:
        found = check_search_degradation()
    except Exception as e:
        logger.debug("search_degradation_detector query failed: %s", e)
        return 0
    if not found:
        return 0
    from robothor.engine.alerts import alert_about_run

    severity = _severity(found)
    if not _should_fire(f"search_degraded:{severity}", _SLOW_DEDUP_TTL_SECONDS):
        return 0
    if await alert_about_run(severity, "Web search degraded", _body(found)):
        return 1
    return 0
