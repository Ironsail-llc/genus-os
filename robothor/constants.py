"""Platform-wide constants for Genus OS."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_TENANT = os.environ.get("ROBOTHOR_DEFAULT_TENANT", "default")

#: Prefix every benchmark-sandbox write refusal starts with (crm.py,
#: memory.py handler gates; robothor/crm/dal.py's create_session_goal guard).
#: Lives here rather than in robothor.engine.benchmark_sandbox because
#: robothor/crm/dal.py must stay importable without the engine (see the
#: layering note above `_benchmark_sandbox` in that module) while the engine
#: side (log_tool_event's sandbox-denial classification, detectors.py) also
#: needs it — robothor.constants is the one module both layers already
#: import from.
SANDBOX_DENIAL_PREFIX = "benchmark sandbox:"

#: agent_tool_events.error_type value for a failure whose message starts
#: with SANDBOX_DENIAL_PREFIX. Shared between tracking.py (which sets it)
#: and detectors.py (which excludes it) so the label can't drift between them.
SANDBOX_DENIED_ERROR_TYPE = "sandbox_denied"

#: crm_agent_notifications.notification_type for an alert raised about (or
#: inside) a benchmark run. Here for the same layering reason as
#: SANDBOX_DENIAL_PREFIX: robothor/engine/alerts.py WRITES the type and
#: robothor/crm/dal.py's get_agent_inbox must EXCLUDE it by default, and the
#: DAL has to stay importable without the engine. Allowed by the CHECK
#: constraint since migration 116 — an INSERT of a value not in that list is
#: rejected and the row silently lost, which is what happened to alert_digest
#: before migration 099.
BENCHMARK_DIGEST_NOTIFICATION_TYPE = "benchmark_digest"


def tenant_env_conflict() -> str | None:
    """Describe a disagreement between the two tenant env vars, or None.

    ``ROBOTHOR_TENANT_ID`` is the tenant a process operates AS — it is what the
    RLS connection binding uses. ``ROBOTHOR_DEFAULT_TENANT`` is what DAL calls
    tag rows with when the caller names no tenant. When they disagree, every
    such write is refused by the RLS WITH CHECK, logged at WARNING, and the
    caller simply gets None.

    That is not hypothetical. The Delphi engine unit set
    ``ROBOTHOR_TENANT_ID=delphi`` while ``/etc/robothor/robothor.env`` pinned
    ``ROBOTHOR_DEFAULT_TENANT=robothor-primary``, and systemd applies
    ``EnvironmentFile=`` BEFORE the unit's own ``Environment=`` lines, so only
    one of the two was overridden. Measured 2026-08-22: 218 refusals in seven
    days and zero delphi rows in memory_insights since 2026-07-14.

    Read at call time rather than import time so a process can be checked after
    it has finished loading its environment.
    """
    operating_as = (os.environ.get("ROBOTHOR_TENANT_ID") or "").strip()
    write_default = (os.environ.get("ROBOTHOR_DEFAULT_TENANT") or "").strip()
    if not operating_as or not write_default or operating_as == write_default:
        return None
    return (
        f"tenant env conflict: ROBOTHOR_TENANT_ID={operating_as!r} but "
        f"ROBOTHOR_DEFAULT_TENANT={write_default!r}. Connections bind to "
        f"{operating_as!r} for row-level security while default-tenant writes are "
        f"tagged {write_default!r}, so RLS refuses every one of them and the "
        f"caller receives None. Set ROBOTHOR_DEFAULT_TENANT={operating_as!r} for "
        "this process."
    )


def owner_config_path() -> Path:
    """Canonical, platform-hardcoded location of the operator identity file.

    Resolves to ``~/.robothor/owner.yaml`` — a conventional user-level
    dotfile, independent of ``ROBOTHOR_WORKSPACE`` (which holds project
    data, not identity). The *path* is tracked in the platform; the
    *content* at that path is per-instance and gitignored.
    """
    return Path.home() / ".robothor" / "owner.yaml"
