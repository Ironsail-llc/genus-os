# Service roles — moving the fleet off allow-all

## The situation this documents

`rbac` is enabled, at `enforce`, and its call site is reachable. A real
violation was fired at it and it blocked (`test_controls_are_armed.py`). It has
still logged **zero `agent_guardrail_events` rows in its entire existence**.

That is not a wiring defect. It is policy:

| | |
|---|---|
| `robothor/engine/config.py` | an agent with no `role:` gets the fleet default |
| migration 107 | `service` → `('*', 'allow')` |
| every live manifest | declares no `role:` |

So every agent resolves to `service`, `service` permits everything, and the
gate has never had anything to deny. An operator reading "RBAC: enforce" on the
dashboard is reading a true statement that means nothing — which is a worse
failure than an inert control, because an inert control at least looks
suspicious.

Verified 2026-08-27: 22 of 22 agents resolve to `service`.

## Why the default was not simply changed

Flipping it would deny every tool call on every instance on upgrade, and the
denials would read as a bug rather than a posture change. Posture is the
operator's decision. What changed is that the decision is now *cheap* and the
current state is *visible*.

## Seeing where you stand

The engine logs once per agent, at WARNING, the first time it builds a config
for an agent that resolves to the allow-all role:

```
Agent main runs UNRESTRICTED: it declares no role, so it resolves to
'service', which migration 107 seeds as ('*', 'allow'). ...
```

To list them without waiting for a run:

```bash
python - <<'PY'
from pathlib import Path
from robothor.engine.config import load_all_manifests
from robothor.engine.service_roles import unrestricted_agents
print(unrestricted_agents(load_all_manifests(Path.home() / "robothor" / "docs" / "agents")))
PY
```

## Moving the fleet

Two levers. Prefer the second for anything you actually care about.

**1. Fleet-wide default** — one env var, no manifest edits:

```
Environment=ROBOTHOR_DEFAULT_SERVICE_ROLE=member
```

`member` is seeded `get_*` / `list_*` / `search_*` allow, `*` deny. Any agent
that still declares its own `role:` keeps it. The UNRESTRICTED warning stops
once the default is no longer the allow-all role.

**2. Per-agent** — in the manifest, which survives platform upgrades:

```yaml
id: email-briefing
role: member
```

## Do this in observe first

`ROBOTHOR_RBAC_MODE=observe` logs what *would* be blocked without blocking it.
Move the default, leave rbac in observe for a few days, then read the
would-block set:

```sql
SELECT tool_name, count(*)
FROM agent_guardrail_events
WHERE guardrail_name = 'rbac' AND action = 'observed'
GROUP BY 1 ORDER BY 2 DESC;
```

Every row is a tool an agent needs and its new role does not grant. Widen the
role (or that one agent) until the set is empty, then set
`ROBOTHOR_RBAC_MODE=enforce` and record the flip in
[`GUARDRAIL_FLIPS.md`](GUARDRAIL_FLIPS.md).

Promoting on an empty would-block set proves nothing on its own — an empty set
is also what a control that never runs produces. Confirm the rows are being
written at all before reading zero as success.

## Roles available

Seeded by migration 107 and visible in `role_permissions`:

| role | grants |
|---|---|
| `service` | `*` allow — **the allow-all default** |
| `admin`, `owner`, `user` | `*` allow |
| `member` | `get_*`, `list_*`, `search_*`; `*` deny |
| `viewer` | as `member`, plus `memory_block_list` / `memory_block_read` |
| `guest` | `*` deny |
| `federation_parent` | `get_*`, `list_*`, `search_*`; `*` deny |
| `federation_child` | `*` deny |

Per-agent exceptions go in `user_permissions` keyed on the run's `user_id`
(`service:<agent-id>` for system triggers). A `user_permissions` row beats the
role outright, in both directions.

## Related

- [`GUARDRAIL_FLIPS.md`](GUARDRAIL_FLIPS.md) — the flip record every promotion writes
- [`TENANT_RLS.md`](TENANT_RLS.md) — the other control that was enabled and inert
