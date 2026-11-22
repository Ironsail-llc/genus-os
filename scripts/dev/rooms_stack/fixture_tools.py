"""The dry run's data source: one read-only tool with deterministic figures.

``sample_sales_figures`` stands in for a company's sales system. Each day's
figures are a pure function of the date, so a period always reports the same
numbers, and a run's provenance names a real tool call.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

NAME = "sample_sales_figures"

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "Sample sales figures for a period (dry-run fixture data): revenue and "
            "orders per day. With no dates, the period the current run is for, "
            "else yesterday."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "First day, YYYY-MM-DD"},
                "end": {"type": "string", "description": "Last day, YYYY-MM-DD"},
            },
            "required": [],
        },
    },
}


def figures_for(start: date, end: date) -> dict[str, Any]:
    """Revenue and orders for each day from ``start`` to ``end``, inclusive."""
    days = []
    day = start
    while day <= end and len(days) < 93:
        ordinal = day.toordinal()
        days.append(
            {
                "date": day.isoformat(),
                "revenue": 1000 + (ordinal % 17) * 25,
                "orders": 40 + ordinal % 7,
            }
        )
        day += timedelta(days=1)
    revenue = sum(d["revenue"] for d in days)
    orders = sum(d["orders"] for d in days)
    period = start.isoformat() if start == end else f"{start.isoformat()} to {end.isoformat()}"
    return {
        "period": period,
        "days": days,
        "revenue_total": revenue,
        "orders_total": orders,
        "note": f"Revenue {revenue:,} from {orders} orders ({period}).",
    }


def _period(args: dict[str, Any]) -> tuple[date, date]:
    try:
        start = date.fromisoformat(str(args["start"]))
        end = date.fromisoformat(str(args.get("end") or args["start"]))
        return start, end
    except (KeyError, ValueError):
        pass
    from robothor.rooms.run_context import current_room_run

    run = current_room_run()
    if run is not None and run.period_start and run.period_end:
        return run.period_start, run.period_end
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    return yesterday, yesterday


async def handle(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    start, end = _period(args or {})
    return figures_for(start, end)
