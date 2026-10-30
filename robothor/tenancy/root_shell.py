"""Moves between tenants are the root operator's: run from the root's shell.

``genus tenant move-account`` and ``genus tenant move-workload`` re-tenant rows
in tenants under one root. Run from a shell whose engine tenant is a child, or
another root, they would act in tenants that shell does not administer. Both
check this before anything is written, through :func:`root_shell_refusal`.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["root_shell_refusal", "shell_tenant", "tenant_ancestry"]


def shell_tenant() -> str:
    """The tenant this process's engine runs as, in the order the engine reads it."""
    from robothor.constants import DEFAULT_TENANT

    return (
        os.environ.get("ROBOTHOR_TENANT_ID", "")
        or os.environ.get("ROBOTHOR_DEFAULT_TENANT", "")
        or DEFAULT_TENANT
    )


def tenant_ancestry(cur: Any, tenant: str) -> list[str]:
    """``tenant`` and its ancestors, nearest first; the last is its root.

    ``cur`` returns rows as mappings (a ``RealDictCursor``). Empty for a tenant
    with no row.
    """
    cur.execute(
        """WITH RECURSIVE up(id, parent, depth) AS (
               SELECT id, parent_tenant_id, 0 FROM crm_tenants WHERE id = %s
               UNION ALL
               SELECT t.id, t.parent_tenant_id, up.depth + 1
               FROM crm_tenants t JOIN up ON t.id = up.parent
               WHERE up.depth < 32)
           SELECT id FROM up ORDER BY depth""",
        (tenant,),
    )
    return [r["id"] for r in cur.fetchall()]


def root_shell_refusal(command: str, roots: set[str]) -> str | None:
    """Why this shell may not run ``command`` on tenants under ``roots``, or None.

    The shell's engine tenant must be the one root every tenant involved sits
    under. Tenants under two roots can never satisfy that.
    """
    shell = shell_tenant()
    if roots == {shell}:
        return None
    if len(roots) == 1:
        (root,) = roots
        return (
            f"this shell's engine tenant is {shell!r}, but the tenants are under the root "
            f"{root!r}; run {command} from a root shell (ROBOTHOR_TENANT_ID unset or "
            f"{root!r}, and ROBOTHOR_DEFAULT_TENANT {root!r})"
        )
    named = ", ".join(repr(r) for r in sorted(roots))
    return (
        f"the tenants are under different roots ({named}); {command} runs from the "
        f"shell of their one root, and this shell's engine tenant is {shell!r}"
    )
