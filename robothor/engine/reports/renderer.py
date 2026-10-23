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

from robothor.engine.reports.devops_periods import period_label

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

    # Normalize the period to a clean label once; pass it to the template as
    # an explicit context var so every surface (HTML header, subject, plain
    # summary) derives from the same normalizer and never renders a dict-repr.
    label = period_label(data.get("period"))

    generated_at = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M ET")
    html = template.render(**data, generated_at=generated_at, period_label=label)

    subject = f"{report_type.replace('_', ' ').title()} — {label}"

    # Build plain text summary
    plain_summary = _build_plain_summary(report_type, data)

    return {
        "html": html,
        "subject": subject,
        "plain_summary": plain_summary,
    }


def render_devops_report(data: dict[str, Any]) -> dict[str, Any]:
    """Render the devops weekly report.

    ``data`` is either a context from
    :func:`robothor.engine.reports.devops_report.build_report_context`, or a
    ``people.json`` aggregate with the analyst narrative under ``analyst``
    (built into a context here). Every number shown comes from the aggregate.
    """
    from robothor.engine.reports.devops_report import build_report_context

    if "role_groups" not in data:
        if "members" not in data:
            return {
                "error": "report_data must be the people.json aggregate (with optional "
                "'analyst' narrative) or a built report context"
            }
        data = build_report_context(data, data.get("analyst"))
    result = render_report("devops_weekly", data)
    if "error" not in result:
        result["subject"] = f"Dev Team Operations Report — {period_label(data.get('period'))}"
        result["plain_summary"] = devops_plain_summary(data)
    return result


def _fmt(v: Any) -> str:
    return "n/a" if v is None else str(v)


def devops_plain_summary(ctx: dict[str, Any]) -> str:
    """Plain-text digest of a devops report context (Telegram, logs)."""
    team = ctx.get("team") or {}
    lines = [f"Dev Team Ops Report — {period_label(ctx.get('period'))}", ""]
    lines.append(f"PRs merged: {_fmt(team.get('prs_merged'))}")
    lines.append(f"Commits: {_fmt(team.get('commits'))}")
    lines.append(f"Tickets resolved: {_fmt(team.get('tickets_resolved'))}")
    lines.append(f"Reviews given: {_fmt(team.get('reviews_given'))}")
    lines.append(f"Merged without review: {_fmt(team.get('merged_without_review'))}")
    summary = (ctx.get("narrative") or {}).get("summary")
    if summary:
        lines += ["", summary]
    bottlenecks = (ctx.get("narrative") or {}).get("bottlenecks") or []
    if bottlenecks:
        lines += ["", "Bottlenecks:"]
        lines += [f"  {i}. {b.get('text', '')}" for i, b in enumerate(bottlenecks, 1)]
    dq = ctx.get("data_quality") or {}
    flags = []
    if dq.get("unavailable_metrics"):
        flags.append(f"{len(dq['unavailable_metrics'])} metric(s) unavailable")
    if dq.get("outside_roster"):
        flags.append(f"{len(dq['outside_roster'])} active handle(s) not on the roster")
    if dq.get("no_activity"):
        flags.append(f"{len(dq['no_activity'])} roster member(s) with no activity")
    if dq.get("usage_unavailable"):
        flags.append("Claude usage unavailable")
    if flags:
        lines += ["", "Data quality: " + "; ".join(flags)]
    lines += ["", "Full HTML report sent via email."]
    return "\n".join(lines)


def _build_plain_summary(report_type: str, data: dict[str, Any]) -> str:
    """Build a plain text summary suitable for Telegram delivery."""
    period_str = period_label(data.get("period"))
    lines = [f"{report_type.replace('_', ' ').title()} — {period_str}", ""]

    es = data.get("executive_summary", {})
    if es:
        for key, val in es.items():
            # Skip nested objects (e.g. a nested last_week dict) so we never
            # dump a raw dict/list repr into the plain-text summary.
            if isinstance(val, (dict, list)):
                continue
            label = key.replace("_", " ").title()
            lines.append(f"{label}: {val}")
        lines.append("")

    bottlenecks = data.get("bottlenecks", [])
    if bottlenecks:
        lines.append("Bottlenecks:")
        for i, b in enumerate(bottlenecks, 1):
            text = (b.get("text") or b.get("description") or "") if isinstance(b, dict) else str(b)
            lines.append(f"  {i}. {text}")
        lines.append("")

    lines.append("Full HTML report sent via email.")
    return "\n".join(lines)
