# Federation — running an organization of instances

Genus OS instances can be linked into a hierarchy: a parent that can see and
direct its children, and children that hold **no** control over the parent.
This is the feature that lets one operator run a fleet of subordinate
instances — a regional office, a client deployment, a boat — while remaining
the only party who can reach downward.

## The shape, and why the child issues the invite

Counter-intuitively, **the subordinate instance issues the invite**:

```
child$   robothor federation invite --relationship child
         → hands the token to the parent's operator
parent$  robothor federation connect <token>
```

The issuer is the side that gets dialled *into*. So a child inviting a parent
makes the parent a client inside the child's system — a principal in the
child's own authorization model, which is exactly what "Robothor comes in and
works with those systems as subsystems" means. The reverse (parent invites
child) also works and gives the child a one-way channel to report upward.

## Three independent layers enforce the asymmetry

Configuration is not enforcement. Every inbound op crosses all three, and each
refuses on its own — the soak proves that by running the negative case three
times with two of the three disabled each time.

| layer | mechanism | what it stops |
|---|---|---|
| broker | per-connection NATS account, one importable service, publish allow-list | a peer forming a request for any other subject at all |
| connection | `exports` on the connection row | an op outside what this peer was granted |
| authorization | `check_tool_permission(role, tenant, tool, user_id)` | any tool the principal's role denies |

Plus tenancy: every inbound op runs inside `tenant_scope`, so a tool that
forgets its `WHERE tenant_id` still cannot leak. That layer is only real while
row-level security is enforcing, so **activation refuses when RLS is inert** —
see [`TENANT_RLS.md`](TENANT_RLS.md). Override with
`ROBOTHOR_FEDERATION_ALLOW_INERT_RLS=1` if you are federating two of your own
instances on one box and accept that inbound ops are not tenant-isolated.

## What a peer can do by default

| relationship | default exports |
|---|---|
| a child of yours | `report_up` — telemetry upward, nothing else |
| a parent of yours | `read_health`, `read_runs` |

`trigger_agent` is in **no** default on either side. Granting it takes two
deliberate steps, because the capability alone is not enough:

```bash
# 1. the capability
robothor federation export <connection-id> trigger_agent

# 2. the permission — federation_parent is a READ-ONLY role, so the
#    authorization layer still refuses without this
psql -c "INSERT INTO user_permissions (tenant_id, user_id, tool_pattern, access)
         VALUES ('<tenant>', 'federation:<connection-id>', 'exec', 'allow')"
```

A `user_permissions` row beats the role outright, in both directions. That is
the intended per-connection control path — and it means `federation_child`
being seeded `'*' -> deny` is a strong default, **not** a hard ceiling.

## Prerequisites

Both instances need an identity and a reachable broker:

```bash
robothor federation init
```

The issuing side needs its engine credential set, or the daemon cannot connect
to its own broker:

```
Environment=ROBOTHOR_NATS_URL=nats://127.0.0.1:4222
Environment=ROBOTHOR_NATS_USER=engine
Environment=ROBOTHOR_NATS_PASSWORD=<from the broker config>
Environment=ROBOTHOR_PUBLIC_ENDPOINT=nats://<host-the-peer-can-reach>:4222
```

`federation invite` writes the peer's account into
`/etc/nats/federation.d/accounts.conf` and reloads the broker. If the reload
fails it says so and exits non-zero — the token would not have worked, and
that is worth failing on rather than discovering later.

## Checking it actually works

```bash
robothor federation status
```

This reports the **wire**, not the `state` column. A link marked `active` whose
transport is dead reads `NOT ATTACHED`, and the command exits non-zero. That
distinction exists because for five months this instance reported `active` for
connections that had never carried a message.

| verdict | meaning |
|---|---|
| `attached` | the daemon holds it and the heartbeat has confirmed it |
| `pairing` | waiting for the peer's handshake; not yet carrying traffic |
| `stale` | attached once, silent since — the age is printed |
| `never attached` | marked active, transport has never reported |
| `suspended` | you turned it off |

## Suspending a child

```bash
robothor federation suspend <connection-id>
robothor federation invite --relationship child   # regenerates the broker accounts
```

Suspension stops traffic at the responder immediately and removes the peer's
broker account on the next provisioning pass. A suspended peer cannot
handshake its way back — that would make the kill switch last exactly as long
as it takes the peer to resend a hello.

## Proving a change did not break it

```bash
python scripts/federation_soak.py
```

Two databases, two processes running the real daemon startup path, a real
`nats-server`, a restart, and the asymmetry proved three times. 34 checks.
Nothing in it mocks the transport — a green unit suite coexisted with a
feature that had never carried a single message, so the gate is deliberately
made of things that cannot be faked.

## Deliberately not in v1

`push_config` and write-back sync (a parent silently overwriting a child's
config is an RCE primitive if config drives behaviour), JetStream-backed event
sync, leafnodes, and `search_memory` for a remote principal until it has been
reviewed against `identity/scope.py`. The data model supports nesting deeper
than two levels; the transport for it is not built yet.
