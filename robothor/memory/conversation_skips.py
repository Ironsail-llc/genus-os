"""Conversation windows that were skipped, and retrying them.

Conversation ingest skips a window only after the model ANSWERED unusably
(empty, or not the JSON asked for) three runs in a row; an outage never skips
anything. Each skipped window is a row in ``ingested_items``
(``conversation_facts_skipped``, ``item_id`` = ``<session key>#<first>-<last>``
message ids). This lists them and, only when asked, extracts them again: a
window that now extracts has its facts written as conversation ingest writes
them and its row removed; one that still fails stays listed.

    python -m robothor.memory.conversation_skips --tenant <id>          # status
    python -m robothor.memory.conversation_skips --tenant <id> --retry  # retry
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

from robothor.db.connection import get_connection
from robothor.engine.sanitize import sanitize_log
from robothor.memory import conversation_ingest as ci
from robothor.memory.conversation_facts import extract_conversation_facts

logger = logging.getLogger(__name__)


def _skipped(tenant_id: str) -> list[dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT item_id, content_hash FROM ingested_items WHERE tenant_id = %s "
            "AND source_name = %s ORDER BY ingested_at, id",
            (tenant_id, ci._SKIPPED_SOURCE),
        )
        rows = cur.fetchall()
    out = []
    for item_id, reason in rows:
        key, _, span = str(item_id).rpartition("#")
        start, _, end = span.partition("-")
        if key and start.isdigit() and end.isdigit():
            out.append(
                {"session_key": key, "start_ref": int(start), "end_ref": int(end), "reason": reason}
            )
    return out


def _forget(tenant_id: str, window: dict[str, Any]) -> None:
    with get_connection() as conn:
        conn.cursor().execute(
            "DELETE FROM ingested_items WHERE tenant_id = %s AND source_name = %s AND item_id = %s",
            (
                tenant_id,
                ci._SKIPPED_SOURCE,
                f"{window['session_key']}#{window['start_ref']}-{window['end_ref']}",
            ),
        )
        conn.commit()


async def _retry_one(tenant_id: str, window: dict[str, Any]) -> bool:
    key = window["session_key"]
    mark = await asyncio.to_thread(ci._claim, key, tenant_id)
    if mark is None:
        return False  # a run holds the conversation now; try again later
    try:
        conv = await asyncio.to_thread(
            ci._load_conversation,
            key,
            tenant_id,
            window["start_ref"] - 1,
            ci.WINDOW_MAX_TURNS * ci.MAX_WINDOWS_PER_RUN,
            window["end_ref"],
        )
        if conv is None or not conv.turns:
            await asyncio.to_thread(_forget, tenant_id, window)  # nothing left to extract
            return True
        owner = ci.owner_of(conv, key)
        metadata = {"session_key": key, "agent_id": "skip-retry", "run_id": "skip-retry"}
        for turns in ci.windows([ci._clipped(t) for t in conv.turns]):
            extraction = await extract_conversation_facts(turns, context=conv.context)
            if not await asyncio.to_thread(ci._renew, key, tenant_id, mark.token):
                return False  # the claim lapsed while the model worked: write nothing
            if extraction.error is not None:
                return False
            part = await ci._write_window(
                extraction.facts,
                turns,
                session_key=key,
                tenant_id=tenant_id,
                trigger_type="conversation",
                metadata=metadata,
                owner=owner,
            )
            if part.get("facts_failed"):
                return False
        await asyncio.to_thread(_forget, tenant_id, window)
        return True
    finally:
        await asyncio.to_thread(ci._release, key, tenant_id, mark.token)


async def retry_skipped(tenant_id: str, *, apply: bool = False) -> dict[str, Any]:
    """The tenant's skipped windows (dry run), or retry them all (``apply``)."""
    windows = await asyncio.to_thread(_skipped, tenant_id)
    report: dict[str, Any] = {
        "tenant_id": tenant_id,
        "dry_run": not apply,
        "skipped": len(windows),
        "windows": windows,
    }
    if not apply:
        return report
    retried = 0
    for window in windows:
        if await _retry_one(tenant_id, window):
            retried += 1
        else:
            logger.warning(
                "skipped window %s#%d-%d still does not extract",
                sanitize_log(window["session_key"]),
                window["start_ref"],
                window["end_ref"],
            )
    report["retried"] = retried
    report["still_failing"] = len(windows) - retried
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m robothor.memory.conversation_skips",
        description="List, and optionally retry, conversation windows ingest skipped.",
    )
    parser.add_argument("--tenant", required=True, help="the tenant to look at")
    parser.add_argument("--retry", action="store_true", help="extract them again")
    args = parser.parse_args(argv)
    report = asyncio.run(retry_skipped(args.tenant, apply=args.retry))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
