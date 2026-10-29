# Operations ledger

`robothor.operations.store` is the PostgreSQL ledger for recoverable work and
reviewable external effects. Every public method takes an explicit tenant —
`Operations(tenant_id)` — and every transaction sets row-level security as well
as filtering in SQL. Sales intelligence ([Sales intelligence](SALES_INTELLIGENCE.md))
is built on it, and a plugin can use it the same way ([Plugins](PLUGINS.md#proposing-an-external-effect)).

This page covers the part that sends things into the world: **actions**.

## The life of an action

| Step | Call | Status after |
|------|------|--------------|
| Propose an exact payload | `propose(kind, key, payload, proposed_by=None)` | `review` |
| Decide it | `decide(action_id, approved, actor)` | `approved` or `rejected` |
| Claim the approved payload | `claim_action(kind=...)` | `executing`, with a `lease_token` |
| Claim one named approved action | `claim_action_by_id(action_id)` | `executing`, with a `lease_token` |
| Record the outcome | `finish_action(action_id, lease_token, status, receipt)` | `completed`, `unknown`, `cancelled` or `failed` |
| Withdraw it before a claim | `cancel_action(action_id, reason, proposed_by=..., actor=...)` | `cancelled` |

A proposal is immutable. Its `key` is an idempotency key: proposing the same
`kind` and `key` again returns the same action id, and a different payload — or
a different `proposed_by` — under that key is refused with `Conflict`. An
approval binds the exact payload hash; if the stored payload changes after
approval, `claim_action` refuses it. A claim whose lease expires becomes
`unknown`, never ready again, so an uncertain send is never retried blindly.
`completed` requires a provider acknowledgement id in the receipt.

`claim_action_by_id` is fenced exactly like `claim_action`: it first turns
expired leases into `unknown`, locks the row with `SKIP LOCKED` so only one
executor wins, re-checks the approved payload hash, and audits `action.claimed`.
It returns `None` for an action that is not approved, has expired, belongs to
another tenant, or is already claimed.

`cancel_action` withdraws an action nobody should execute any more — a draft
superseded by a newer one, or one a rule refuses. A plugin's action is
withdrawn only by its proposer (`proposed_by="<plugin>"`); an action proposed
without one only by a named human (`actor="operator:<id>"`), so an operator's
approval is never undone anonymously. Only `review` and `approved` actions can
be cancelled, and the audit row (`action.cancelled`, actor `plugin:<name>` or
the operator) records the reason and the status it left. A claimed action is the executor's to settle with
`finish_action`.

`get_action(action_id)` and `list_actions(kinds=None, status=None,
proposed_by=None, limit=100)` read without changing anything; the list is
newest first and `limit` runs from 1 to 1000.

## Who may decide

`decide()` accepts two kinds of actor. Anything else is refused with
`Conflict`, before the database is touched.

| Actor | Who | May decide |
|-------|-----|-----------|
| `operator:<id>` | A human. The caller authenticates them before calling `decide`. | Any action in review |
| `policy:<plugin>:<version>` | An automated rule shipped by `<plugin>`, at rule version `<version>` | Only actions whose `proposed_by` is `<plugin>` |

A policy naming a different plugin than the proposer is refused, and so is a
policy deciding an action that names no proposer — every action proposed
without `proposed_by`, the sales outreach drafts included, stays operator-only.
The proposer check scopes a policy to its own plugin's drafts; it is not
authentication, since a plugin runs in the engine's process.

Each decision writes `operation_actions.decided_by` and an `operation_audit`
row (`action.approved` or `action.rejected`) whose `actor` is the full actor
string. A policy decision also records `{"policy": "<plugin>", "version":
"<version>"}` in the audit `detail`, so a reviewer can tell which version of a
rule acted. The proposal's own audit row names `plugin:<name>` as its actor, or
`system` when no proposer was given.

## Executing an approved action from a workflow

Once an approval commits, `decide` publishes one event on the `operations`
stream (Redis key `robothor:events:operations`):

| Field | Value |
|-------|-------|
| `type` | `operation_action.approved` |
| `actor` | the deciding actor |
| `tenant_id` | the action's tenant |
| `payload` | `{"action_id": "<uuid>", "kind": "<kind>", "tenant_id": "<tenant>", "proposed_by": "<plugin>" or null}` |

A rejection publishes nothing. Publishing is best effort, like every event-bus
producer: if Redis is down the action is still approved, and a scheduled sweep
calling `claim_action` will find it.

A workflow hooks the event and hands the action id to the tool that executes it:

```yaml
id: acme-execute-approved
triggers:
  - type: hook
    stream: operations
    event_type: operation_action.approved
steps:
  - id: execute
    type: tool
    tool_name: acme_execute_action
    tool_args:
      action_id: "{{ event.payload.action_id }}"
      kind: "{{ event.payload.kind }}"
```

Every approval in the engine's tenant reaches every workflow with this
trigger, so the tool reads the named action with `get_action(action_id)`, leaves it alone when
its `kind` is not one the plugin owns, and only then claims it with
`claim_action_by_id(action_id)` — a claim it then abandons would expire to
`unknown`.

The event is a prompt, not the only path. Engines read the stream in one
consumer group, so each event is handled by one engine; where several engines
share a Redis, the one that receives an approval for another tenant finds
nothing with `get_action` and moves on. Keep a scheduled job or cron workflow
that calls `claim_action(kind=...)` as the backstop: it picks up any approved
action whose event was lost or went to another engine. A workflow can also filter on
`event.payload.proposed_by` in a condition step before the tool runs. The workflow runs as the engine's
tenant; the tool reads it from `ctx.tenant_id`, and the event's `tenant_id`
says which tenant the approval was for.

`robothor/engine/tests/test_operation_approved_workflow.py` drives this path
end to end without a database, and
`robothor/engine/tests/test_plugin_operations_seam.py` runs a plugin's propose,
policy decision, claim and finish against PostgreSQL.

## Schema

The tables are created by `crm/migrations/126_durable_operations.sql`;
`crm/migrations/154_operation_action_proposer.sql` adds
`operation_actions.proposed_by`. Rows proposed before that migration have no
proposer and remain operator-only.
