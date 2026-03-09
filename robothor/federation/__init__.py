"""Federation — peer-to-peer instance networking for Robothor.

Enables independent Robothor instances to form explicit connections
with scoped permissions. No hub-spoke designation — any instance
becomes a hub organically when many connect to it.
"""

from __future__ import annotations

from robothor.federation.models import (
    Connection,
    ConnectionState,
    Instance,
    Relationship,
    SyncChannel,
    SyncEvent,
)

__all__ = [
    "Connection",
    "ConnectionState",
    "Instance",
    "Relationship",
    "SyncChannel",
    "SyncEvent",
]
