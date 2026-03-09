"""Data models for the federation subsystem.

All models are plain dataclasses matching the engine convention.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ConnectionState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    LIMITED = "limited"
    SUSPENDED = "suspended"


class Relationship(StrEnum):
    PARENT = "parent"
    CHILD = "child"
    PEER = "peer"


class SyncChannel(StrEnum):
    CRITICAL = "critical"  # Tasks, config, memory facts, alerts
    BULK = "bulk"  # Agent runs, tool calls, telemetry
    MEDIA = "media"  # Images, audio, documents


# ── Hybrid Logical Clock ─────────────────────────────────────────────


@dataclass
class HLC:
    """Hybrid Logical Clock — wall time + counter + instance ID.

    Provides causal ordering across distributed instances while staying
    close to wall-clock time. Based on Kulkarni et al. (2014).
    """

    wall_ms: int = 0
    counter: int = 0
    instance_id: str = ""

    def tick(self) -> HLC:
        """Advance for a local event."""
        now_ms = _now_ms()
        if now_ms > self.wall_ms:
            return HLC(wall_ms=now_ms, counter=0, instance_id=self.instance_id)
        return HLC(wall_ms=self.wall_ms, counter=self.counter + 1, instance_id=self.instance_id)

    def merge(self, remote: HLC) -> HLC:
        """Merge with a received remote clock (receive event)."""
        now_ms = _now_ms()
        max_wall = max(now_ms, self.wall_ms, remote.wall_ms)
        if max_wall == now_ms and max_wall != self.wall_ms and max_wall != remote.wall_ms:
            counter = 0
        elif max_wall == self.wall_ms and max_wall == remote.wall_ms:
            counter = max(self.counter, remote.counter) + 1
        elif max_wall == self.wall_ms:
            counter = self.counter + 1
        else:
            counter = remote.counter + 1
        return HLC(wall_ms=max_wall, counter=counter, instance_id=self.instance_id)

    def to_string(self) -> str:
        return f"{self.wall_ms}:{self.counter}:{self.instance_id}"

    @classmethod
    def from_string(cls, s: str) -> HLC:
        parts = s.split(":", 2)
        if len(parts) != 3:
            raise ValueError(f"Invalid HLC string: {s}")
        return cls(wall_ms=int(parts[0]), counter=int(parts[1]), instance_id=parts[2])

    def __lt__(self, other: HLC) -> bool:
        if self.wall_ms != other.wall_ms:
            return self.wall_ms < other.wall_ms
        if self.counter != other.counter:
            return self.counter < other.counter
        return self.instance_id < other.instance_id


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── Instance ─────────────────────────────────────────────────────────


@dataclass
class Instance:
    """This instance's identity."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    display_name: str = ""
    public_key: str = ""  # PEM-encoded Ed25519 public key
    private_key_ref: str = ""  # vault key or SOPS reference — never the raw key
    created_at: str = ""


# ── Connection ───────────────────────────────────────────────────────


@dataclass
class Connection:
    """A link between two instances."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    peer_id: str = ""
    peer_name: str = ""
    peer_endpoint: str = ""
    peer_public_key: str = ""
    relationship: Relationship = Relationship.PEER
    state: ConnectionState = ConnectionState.PENDING
    exports: list[str] = field(default_factory=list)  # capabilities we expose
    imports: list[str] = field(default_factory=list)  # capabilities we consume
    nats_account: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


# ── Invite Token ─────────────────────────────────────────────────────


@dataclass
class InviteToken:
    """One-time token for connection establishment (Consul pattern)."""

    token: str = ""  # base64-encoded blob
    issuer_id: str = ""
    issuer_name: str = ""
    issuer_endpoint: str = ""
    issuer_public_key: str = ""
    relationship: Relationship = Relationship.PEER
    connection_secret: str = ""  # shared secret for initial handshake
    created_at: str = ""
    expires_at: str = ""


# ── Sync Event ───────────────────────────────────────────────────────


@dataclass
class SyncEvent:
    """An entry in the event journal (sync buffer)."""

    id: int = 0  # DB-assigned serial
    connection_id: str = ""
    channel: SyncChannel = SyncChannel.CRITICAL
    event_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    hlc_timestamp: str = ""  # HLC as string
    synced_at: str | None = None
    created_at: str = ""


# ── Default export templates per relationship ────────────────────────

PARENT_DEFAULT_EXPORTS = [
    "memory_search",
    "crm_read",
    "config_push",
]

CHILD_DEFAULT_EXPORTS = [
    "health",
    "agent_runs",
    "sensor_data",
    "alerts",
    "escalation",
]

PEER_DEFAULT_EXPORTS: list[str] = []  # No defaults — fully negotiated


def default_exports_for(relationship: Relationship) -> list[str]:
    """Return default export capabilities for a relationship type."""
    if relationship == Relationship.PARENT:
        return list(PARENT_DEFAULT_EXPORTS)
    if relationship == Relationship.CHILD:
        return list(CHILD_DEFAULT_EXPORTS)
    return list(PEER_DEFAULT_EXPORTS)


# ── Conflict resolution strategies ───────────────────────────────────


class ConflictStrategy(StrEnum):
    """Per-entity conflict resolution strategies."""

    NO_CONFLICT = "no_conflict"  # agent_runs — each instance generates its own
    MONOTONIC_LATTICE = "monotonic_lattice"  # tasks: open < in_progress < done < archived
    ADDITIVE_MERGE = "additive_merge"  # memory facts: add-only, deactivation is monotonic
    AUTHORITY = "authority"  # config: exporting instance wins
    APPEND_ONLY = "append_only"  # logs/telemetry: no conflicts


ENTITY_CONFLICT_MAP: dict[str, ConflictStrategy] = {
    "agent_run": ConflictStrategy.NO_CONFLICT,
    "task": ConflictStrategy.MONOTONIC_LATTICE,
    "memory_fact": ConflictStrategy.ADDITIVE_MERGE,
    "config": ConflictStrategy.AUTHORITY,
    "log": ConflictStrategy.APPEND_ONLY,
    "telemetry": ConflictStrategy.APPEND_ONLY,
}

# Task status lattice for monotonic merge
TASK_STATUS_ORDER = {"open": 0, "in_progress": 1, "done": 2, "archived": 3}
