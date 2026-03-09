# Federation — Peer-to-Peer Instance Networking

Robothor federation lets independent instances form explicit connections with scoped permissions. No hub-spoke designation — an instance becomes a "hub" organically when many connect to it.

## Quick Start

```bash
# On the parent instance (already running):
robothor federation init
robothor federation invite --relationship child --ttl 48
# → prints a one-time token

# On the new instance:
git clone https://github.com/Ironsail-llc/robothor.git
cd robothor && pip install -e ".[all]"
robothor init
robothor federation init
robothor federation connect <token>
robothor engine start
```

## Architecture

### Instance

Every Robothor installation is an instance with:
- An identity (UUID, display name, Ed25519 public key)
- Its own PostgreSQL database, agents, tools, memory
- Zero or more connections to other instances

### Connection

A bilateral link between two instances, established via token exchange:

1. Instance A runs `robothor federation invite` → generates signed token
2. Token transferred out-of-band (copy-paste, email, etc.)
3. Instance B runs `robothor federation connect <token>` → connection created
4. Both sides see the connection in PENDING state
5. Activate when NATS handshake completes → ACTIVE

Each connection has:
- **Relationship**: parent, child, or peer (sets default capability templates)
- **State**: pending → active → optionally limited or suspended
- **Exports**: what this instance exposes to the peer
- **Imports**: what this instance consumes from the peer
- **No transitive trust**: A↔B and B↔C does NOT mean A↔C

### Relationship Defaults

| Relationship | Default Exports | Default Imports |
|---|---|---|
| Parent (to child) | memory_search, crm_read, config_push | health, agent_runs, sensor_data, alerts |
| Child (to parent) | health, agent_runs, sensor_data, alerts, escalation | memory_search, crm_read |
| Peer | None (fully negotiated) | None (fully negotiated) |

"Parent" and "child" are opposite sides of the same connection. If A invites B as "child," then B sees A as its "parent."

### Connection States

```
PENDING ──→ ACTIVE ──→ LIMITED ──→ ACTIVE
   │           │           │
   └──→ SUSPENDED ←────────┘
           │
           └──→ ACTIVE / PENDING
```

## Transport: NATS + JetStream

NATS handles all inter-instance communication:
- **Hub instances**: Run `nats-server` with JetStream enabled (port 4222)
- **Leaf instances**: Run a NATS leaf node connecting to the hub's port 7422
- **Peer topology**: Both run servers, connected via gateways

### Subject Namespace

```
robothor.{connection_id}.sync.critical    # Tasks, config, memory facts, alerts
robothor.{connection_id}.sync.bulk        # Agent runs, tool calls, telemetry
robothor.{connection_id}.sync.media       # Images, audio, documents
robothor.{connection_id}.command          # Remote agent triggers, queries
robothor.{connection_id}.status           # Health + sync watermarks
```

### NATS Setup

Hub instance (this server):
```bash
# Already installed as systemd service
sudo systemctl status robothor-nats
# Config: /etc/nats/nats-server.conf
# JetStream store: /var/lib/nats/jetstream
# Ports: 4222 (client), 7422 (leaf nodes)
```

Leaf instance (connecting peer):
```bash
# Install nats-server on the peer device
# Create /etc/nats/nats-server.conf:
listen: 127.0.0.1:4222
leafnodes {
    remotes [{
        url: "nats-leaf://hub-address:7422"
    }]
}
jetstream {
    store_dir: /var/lib/nats/jetstream
    max_mem: 128MB
    max_file: 512MB
}
```

## Sync Protocol

### Three Channels

| Channel | Contents | Priority |
|---|---|---|
| Critical | Tasks, config, memory facts, alerts | Sync first |
| Bulk | Agent runs, tool calls, telemetry | When bandwidth allows |
| Media | Images, audio, documents | Background |

### Hybrid Logical Clocks (HLC)

Events are timestamped with HLC (wall time + counter + instance ID) for causal ordering across distributed instances. Format: `{wall_ms}:{counter}:{instance_id}`.

### Conflict Resolution

| Entity | Strategy |
|---|---|
| Agent runs | No conflict — each instance generates its own |
| Tasks | Monotonic lattice (open < in_progress < done < archived) |
| Memory facts | Additive merge; deactivation is monotonic |
| Config | Exporting instance is authoritative |
| Logs/telemetry | Append-only, no conflicts |

## Security

- **Ed25519 signatures**: Invite tokens are cryptographically signed to prevent tampering
- **One-time tokens**: Each invite generates a unique connection secret (SHA-256 hashed)
- **Scoped permissions**: Exports/imports are explicit per connection — no implicit access
- **Private key isolation**: Keys stored at `~/.robothor/identity.key` with `0600` permissions
- **NATS accounts**: Each connection gets isolated subject namespaces

## CLI Reference

| Command | Purpose |
|---|---|
| `robothor federation init` | Generate instance identity (Ed25519 keypair) |
| `robothor federation invite [--relationship peer\|parent\|child] [--ttl 24]` | Generate invite token |
| `robothor federation connect <token>` | Accept connection from peer |
| `robothor federation status` | Show instance identity + all connections |
| `robothor federation list` | List connections (compact) |
| `robothor federation export <connection> <capability>` | Expose capability to peer |
| `robothor federation suspend <connection>` | Pause a connection |
| `robothor federation remove <connection>` | Delete a connection |

## Agent Tools

Three tools available to agents with `federation_*` in their `tools_allowed`:

- **federation_query**: Query a connected instance's health, agent runs, or memory
- **federation_trigger**: Trigger an agent run on a connected instance
- **federation_sync_status**: Check sync watermarks and pending event counts

## Database Tables

Created by `001_init.sql` (fresh install) or `025_federation.sql` (upgrade):

- `federation_identity` — this instance's ID, name, public key
- `federation_connections` — peer links with state, exports, imports
- `federation_events` — append-only event journal for sync

## Files

| Path | Purpose |
|---|---|
| `robothor/federation/models.py` | Data models (Instance, Connection, HLC, SyncEvent, etc.) |
| `robothor/federation/config.py` | Config from env + `~/.robothor/federation.yaml` |
| `robothor/federation/identity.py` | Ed25519 keypair, token create/decode/consume |
| `robothor/federation/connections.py` | Connection state machine, DB persistence |
| `robothor/federation/sync.py` | Event journal, HLC, conflict resolution |
| `robothor/federation/nats.py` | NATS/JetStream transport layer |
| `robothor/federation/commands.py` | Cross-instance command dispatch |
| `robothor/engine/tools/handlers/federation.py` | Agent-usable federation tools |
| `crm/migrations/025_federation.sql` | Upgrade migration for existing instances |
| `/etc/nats/nats-server.conf` | NATS server configuration |
