"""Report rendering tool handlers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _parse_data(args: dict[str, Any], key: str = "report_data") -> dict[str, Any] | None:
    """Parse report_data from args (accepts JSON string or dict)."""
    data = args.get(key)
    if not data:
        return None
    if isinstance(data, str):
        return json.loads(data)
    return data


@_handler("render_report")
async def _render_report(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Render any report type by template name."""
    report_type = args.get("report_type", "")
    if not report_type:
        return {"error": "report_type is required (e.g. 'devops_weekly')"}

    try:
        report_data = _parse_data(args)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in report_data: {e}"}
    if not report_data:
        return {"error": "report_data is required"}

    try:
        from robothor.engine.reports.renderer import render_report

        return render_report(report_type, report_data)
    except Exception as e:
        return {"error": f"Report rendering failed: {e}"}


@_handler("render_devops_report")
async def _render_devops_report(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Render the devops weekly report as HTML (convenience shortcut)."""
    try:
        report_data = _parse_data(args)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in report_data: {e}"}
    if not report_data:
        return {"error": "report_data is required"}

    try:
        from robothor.engine.reports.renderer import render_devops_report

        return render_devops_report(report_data)
    except Exception as e:
        return {"error": f"Report rendering failed: {e}"}
