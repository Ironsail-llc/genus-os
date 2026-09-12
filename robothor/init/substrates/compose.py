"""Docker Compose — the enterprise pilot substrate. Not built yet.

Present as a named seam rather than an absent one: ``--substrate compose``
should tell an operator when it lands, not that it has never been thought
about. The design is in the productization spec (workstream A, A10): a
one-shot ``migrate`` service that engine, bridge and orchestrator wait on with
``service_completed_successfully``, GHCR images rather than bind-mounted
source, and GPU reservations in an overlay added only when ``nvidia-smi``
succeeds.
"""

from __future__ import annotations

from typing import NoReturn

__all__ = ["substrate"]


def substrate() -> NoReturn:
    raise NotImplementedError("lands in A10/A11/A12")
