"""Whether a tool call comes from the instance owner (GEN-42).

One rule, shared by every surface that shows the operator's own view of the
box -- ``config_get`` / ``config_set`` and the masked dotenv branch of
``read_file`` -- so they cannot drift apart: a verified identity with role
``owner`` in the run's own tenant, on a top-level run, and never a benchmark
run. Fails closed: a context without an identity, or a run the session
registry does not know, is not the owner.

A sub-agent spawned from the owner's chat inherits the owner's identity for
attribution (``runner.py``); that is not authority, so a run with a parent is
refused -- the same rule ``host_execution.eligible`` applies to host exec.
"""

from __future__ import annotations

from typing import Any

__all__ = ["is_verified_owner"]


def is_verified_owner(ctx: Any) -> bool:
    """``True`` only for the verified owner of the run's own tenant, outside benchmarks."""
    if getattr(ctx, "is_benchmark", False):
        return False
    identity = getattr(ctx, "identity", None)
    if not (
        identity is not None
        and identity.verified
        and identity.role == "owner"
        and identity.tenant_id == getattr(ctx, "tenant_id", None)
    ):
        return False
    from robothor.engine.session_registry import lookup

    session = lookup(getattr(ctx, "run_id", ""))
    return bool(session is not None and not session.run.parent_run_id)
