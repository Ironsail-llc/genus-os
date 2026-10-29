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
| Record the outcome | `finish_action(action_id, lease_token, status, receipt)` | `completed`, `unknown`, `cancelled` or `failed` |

A proposal is immutable. Its `key` is an idempotency key: proposing the same
`kind` and `key` again returns the same action id, and a different payload — or
a different `proposed_by` — under that key is refused with `Conflict`. An
approval binds the exact payload hash; if the stored payload changes after
approval, `claim_action` refuses it. A claim whose lease expires becomes
`unknown`, never ready again, so an uncertain send is never retried blindly.
`completed` requires a provider acknowledgement id in the receipt.

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

Every approval on the instance reaches every workflow with this trigger, so the
tool should claim with its own `kind` (`claim_action(kind="acme-plugin.note")`)
and ignore events for kinds it does not own. The workflow runs as the engine's
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
