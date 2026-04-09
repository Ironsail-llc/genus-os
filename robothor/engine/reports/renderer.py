"""Jinja2 report renderer — generic dispatcher with per-type templates.

Template convention:
    robothor/engine/reports/templates/{report_type}.html

Each template can define Jinja2 blocks for subject and plain_summary,
or the renderer falls back to sensible defaults.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=True,
)


def render_report(report_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Render any report type by name.

    Args:
        report_type: Template name without extension (e.g. "devops_weekly")
        data: Template context data

    Returns:
        {"html": str, "subject": str, "plain_summary": str}
    """
    template_name = f"{report_type}.html"
    try:
        template = _env.get_template(template_name)
    except Exception as e:
        return {"error": f"Template '{template_name}' not found: {e}"}

    generated_at = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M ET")
    html = template.render(**data, generated_at=generated_at)

    # Try to extract subject from a {% block subject %} in the template
    period = data.get("period", "Report")
    subject = f"{report_type.replace('_', ' ').title()} — {period}"

    # Build plain text summary
    plain_summary = _build_plain_summary(report_type, data)

    return {
        "html": html,
        "subject": subject,
        "plain_summary": plain_summary,
    }


def render_devops_report(data: dict[str, Any]) -> dict[str, Any]:
    """Render the devops weekly report (convenience wrapper)."""
    result = render_report("devops_weekly", data)
    if "error" not in result:
        period = data.get("period", "Weekly Report")
        result["subject"] = f"Dev Team Operations Report — {period}"
    return result


def _build_plain_summary(report_type: str, data: dict[str, Any]) -> str:
    """Build a plain text summary suitable for Telegram delivery."""
    period = data.get("period", "Report")
    lines = [f"{report_type.replace('_', ' ').title()} — {period}", ""]

    es = data.get("executive_summary", {})
    if es:
        for key, val in es.items():
            label = key.replace("_", " ").title()
            lines.append(f"{label}: {val}")
        lines.append("")

    bottlenecks = data.get("bottlenecks", [])
    if bottlenecks:
        lines.append("Bottlenecks:")
        for i, b in enumerate(bottlenecks, 1):
            text = b.get("text", "") if isinstance(b, dict) else str(b)
            lines.append(f"  {i}. {text}")
        lines.append("")

    lines.append("Full HTML report sent via email.")
    return "\n".join(lines)
