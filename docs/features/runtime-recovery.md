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

Keep a database backup and the previous application revision for cutover. A code
rollback leaves the additive runtime tables in place; do not delete effect or
approval receipts to retry an action. The runtime drill helpers exercise populated
upgrades, repeated migration application, daemon restarts, and interrupted goals
against a private database. Generated logs and acceptance archives are not shipped.
