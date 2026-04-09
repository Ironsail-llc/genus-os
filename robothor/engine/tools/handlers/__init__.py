"""Tool handler modules — each exposes a HANDLERS dict mapping tool name → async handler."""

from __future__ import annotations

from robothor.engine.tools.handlers import (  # noqa: F401
    crm,
    devops_metrics,
    federation,
    filesystem,
    git,
    github_api,
    gws,
    identity,
    jira,
    memory,
    observability,
    pdf,
    reasoning,
    reports,
    spawn,
    vault,
    vision,
    voice,
    web,
)
