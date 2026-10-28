# Durable engine execution

Interactive Telegram and dashboard runs, plans, deep work, and goal attempts use
one execution context for identity, deadlines, cancellation, and provider budgets.
Interrupted actions retain durable evidence. Recovery reads that evidence before
attempting another mutation; an unknown outcome is never reported as verified.

Apply the canonical database migrations before restarting the engine. This change
adds runtime context, provider reservations, goal task controls, chat approval
receipts, and effect records. Existing model choices, feature switches, operator
permissions, and goal spending ceilings remain in effect. Paused goals stay paused.

A saved plan can be approved once. Dashboard clients can recover an interrupted
reply and record a stop request without resubmitting the original action. A tenant
owner or administrator can resolve a terminal interactive action's uncertainty
through authenticated `POST /chat/reconcile-effect`, supplying `session_key`,
`request_id`, `effect_id`, and an audit `note`. This records an explicitly
unverified operator reconciliation; it does not assert that the action succeeded.
Goal-family uncertainty uses the goal reconciliation control instead.

## How long an unknown outcome blocks a tool

An uncertain action always blocks a repeat of the same call, and any other
write in the same request, until it is reconciled. Whether it also blocks a
reworded call of the same tool in a later request depends on the tool:

- **CRM tasks and notes** have a readback verifier, which settles the action
  by reading the stored record, so the block lasts until that happens.
- **Shell commands, code runs, the browser and file writes** act only on the
  host, where the agent can check the result itself, so they do not block
  later calls.
- **Every other tool** (mail, calendar, messages, other CRM writes) cannot be
  read back. A reworded "send that again" waits for reconciliation, but for
  24 hours at most. After that, housekeeping releases the action as
  `expired`. Chat shows it as released automatically, never as an operator's
  reconciliation, and its outcome stays unverified.

Keep a database backup and the previous application revision for cutover. A code
rollback leaves the additive runtime tables in place; do not delete effect or
approval receipts to retry an action. The runtime drill helpers exercise populated
upgrades, repeated migration application, daemon restarts, and interrupted goals
against a private database. Generated logs and acceptance archives are not shipped.
