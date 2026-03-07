"""Tool handler modules — each exposes a HANDLERS dict mapping tool name → async handler."""

from __future__ import annotations

from robothor.engine.tools.handlers import (  # noqa: F401
    crm,
    filesystem,
    git,
    gws,
    impetus,
    memory,
    observability,
    pdf,
    reasoning,
    spawn,
    vault,
    vision,
    voice,
    web,
)
