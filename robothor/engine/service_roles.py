"""Which role an autonomous agent runs as, and whether that role restricts it.

`rbac` is enabled, at enforce, and its call site is reachable — a real
violation was fired at it and it blocked. It has still logged zero events ever,
and the reason is policy rather than wiring:

    config.py      service_role = manifest.get("role", manifest.get(...), "service")
    migration 107  service -> ('*', 'allow')

Every live manifest declares no role, so every agent resolves to `service`, and
`service` permits everything. The gate has never had anything to deny. An
operator reading "RBAC: enforce" is reading a true statement that means
nothing, which is a worse failure than an inert control — inert controls at
least look suspicious.

This module does not fix the posture, because flipping the default would deny
every tool call on every instance on upgrade. It makes the posture *movable*
and *visible*: one env var changes the whole fleet, and an agent running
unrestricted says so.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

#: Seeded by migration 107 as ('*', 'allow'). Named here so the one role that
#: makes RBAC a no-op is greppable rather than a bare string in three files.
ALLOW_ALL_ROLE = "service"

#: Move the whole fleet without editing every manifest.
DEFAULT_SERVICE_ROLE_ENV = "ROBOTHOR_DEFAULT_SERVICE_ROLE"

#: One warning per agent per process. 1,757 exec calls in 7 days means a
#: per-call warning is a flood, and a flood is how the credential pool logged
#: the same outage 452 times while paging zero.
_warned: set[str] = set()


def default_service_role() -> str:
    """The role an agent gets when its manifest declares none.

    Still `service` out of the box. Changing that default on someone's behalf
    would take their fleet down on upgrade — every tool call denied, with the
    denial looking like a bug rather than a posture change.
    """
    configured = os.environ.get(DEFAULT_SERVICE_ROLE_ENV, "").strip()
    if not configured:
        # A blank value is far more likely a deployment slip than a request for
        # a role-less agent — and an empty role fails closed in
        # check_tool_permission ("Missing execution role"), which would take
        # the fleet down for a typo.
        return ALLOW_ALL_ROLE
    return configured


def resolve_service_role(agent_id: str, declared: str | None) -> str:
    """The role this agent actually runs as, warning if it is unrestricted."""
    role = (declared or "").strip() or default_service_role()
    if role == ALLOW_ALL_ROLE and agent_id not in _warned:
        _warned.add(agent_id)
        logger.warning(
            "Agent %s runs UNRESTRICTED: it declares no role, so it resolves to "
            "'%s', which migration 107 seeds as ('*', 'allow'). RBAC evaluates "
            "this agent and permits every tool. Set `role:` in its manifest, or "
            "move the whole fleet with %s.",
            agent_id,
            ALLOW_ALL_ROLE,
            DEFAULT_SERVICE_ROLE_ENV,
        )
    return role


def unrestricted_agents(manifests: Iterable[dict[str, Any]]) -> list[str]:
    """Agent ids that resolve to the allow-all role.

    One number, read by the readiness probe, the dashboard and the ratchet,
    rather than three places each re-deriving it slightly differently — which
    is how a hand-maintained agent-name list drifted from what was registered
    and produced three separate bug reports for one defect.
    """
    unrestricted = []
    for manifest in manifests:
        declared = str(manifest.get("role", manifest.get("service_role", "")) or "").strip()
        if (declared or default_service_role()) == ALLOW_ALL_ROLE:
            unrestricted.append(str(manifest.get("id", "")))
    return unrestricted
