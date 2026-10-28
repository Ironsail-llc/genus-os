"""Whether a tool call comes from the instance owner (GEN-42).

One rule, shared by every surface that shows the operator's own view of the
box -- ``config_get`` / ``config_set`` and the masked dotenv branch of
``read_file`` -- so they cannot drift apart: a verified identity with role
``owner`` in the run's own tenant, and never a benchmark run. Fails closed: a
context without an identity is not the owner.
"""

from __future__ import annotations

from typing import Any

__all__ = ["is_verified_owner"]


def is_verified_owner(ctx: Any) -> bool:
    """``True`` only for the verified owner of the run's own tenant, outside benchmarks."""
    if getattr(ctx, "is_benchmark", False):
        return False
    identity = getattr(ctx, "identity", None)
    return bool(
        identity is not None
        and identity.verified
        and identity.role == "owner"
        and identity.tenant_id == getattr(ctx, "tenant_id", None)
    )
