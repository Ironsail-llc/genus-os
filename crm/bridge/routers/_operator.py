"""The Helm's single authorization primitive.

Extracted from ``controls.py`` (Phase 1) so every operator-scoped Helm route —
Controls, Fleet, Runs, Workflows, Health — shares ONE gate. Four independent
conditions, none of which may be weakened:

1. ``auth is None``            → no verified session at all.
2. ``auth.is_service``         → agent/service tokens, structurally barred.
3. ``auth.role not in OPERATOR_ROLES`` → dashboard SSO admits any verified org
   member; only owner/admin are operators.
4. ``auth.tenant_id != PLATFORM_TENANT`` → the flags/health/fleet surfaces are
   platform-global; an operator of another tenant must not see or touch them.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Request

from robothor.constants import DEFAULT_TENANT

# ROBOTHOR_PLATFORM_TENANT overrides; otherwise the gate agrees with the rest
# of the bridge on DEFAULT_TENANT (which itself honors ROBOTHOR_DEFAULT_TENANT).
# Never a hardcoded instance tenant id — see CLAUDE.md rule 1.
PLATFORM_TENANT = os.environ.get("ROBOTHOR_PLATFORM_TENANT") or DEFAULT_TENANT

OPERATOR_ROLES = frozenset({"owner", "admin"})


def require_operator(request: Request) -> str:
    auth = getattr(request.state, "auth", None)
    if (
        auth is None
        or auth.is_service
        or auth.role not in OPERATOR_ROLES
        or auth.tenant_id != PLATFORM_TENANT
    ):
        raise HTTPException(status_code=403, detail="operator role required")
    return f"operator:{auth.actor_id}"
