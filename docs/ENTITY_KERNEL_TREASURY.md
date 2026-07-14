# Entity Kernel treasury boundary

The first Entity Kernel slice is a domain and policy boundary. It does **not**
connect to a payment network, issue a card, move money, or establish PCI DSS
compliance by itself.

## Payment classes

- `ClientPaymentMethodReference` represents payment data owned by a client or
  customer. Genus accepts only the payment provider's opaque token. PAN, expiry,
  CVC/CVV, and cryptograms are outside this model and rejected as extra input.
- `OperationalVirtualCardReference` represents a provider-issued virtual card
  owned by the Genus organization. Genus stores its internal card ID, safe
  display metadata, and an opaque provider reference. It never stores or logs
  the card PAN or CVC/CVV.

Provider references are credentials. The models use `SecretStr`, omit them from
`repr`, and expose only a correlation fingerprint in their audit form. A real
adapter must persist them through an encrypted secrets boundary and must not put
them in normal application tables, traces, metrics, exceptions, or logs.

## Decision order

`SpendPolicyEngine` evaluates a proposal in a fixed, fail-closed order:

1. require an enabled and complete policy owned by the same tenant and
   organization;
2. require an active operational virtual card owned by that organization;
3. enforce category, vendor, and currency allowlists;
4. enforce per-transaction, daily, and monthly caps using an explicit usage
   snapshot;
5. require approval at or above the configured threshold; and
6. allow only proposals that pass every preceding check.

Idempotency keys are scoped to tenant and organization. A retry of the same
proposal returns the original decision; reuse of the key for a different
proposal is denied. Production callers must provide a persistent
`DecisionStore` and compute usage with both committed and reserved spend inside
the same transactional boundary.

## Ledger and providers

`AppendOnlyTreasuryLedger` has append and query operations but no update or
delete operation. The local implementation redacts inputs, enforces scoped
idempotency, returns immutable event values, and hash-chains events for tamper
evidence. It is a local/test adapter, not a substitute for durable database or
WORM retention controls.

The client-payment and operational-treasury provider adapters are separate
Protocols. There is deliberately no live provider implementation. An adapter
must receive a matching `allow` decision before constructing an operational
authorization request.

## Authority boundary

The organization owns the `EntityAuthorityPolicy`. Observation and reversible
learning can be delegated. Controlled spend requires both a treasury decision
and explicit autonomous-spend authority. Production changes, code
self-modification, and expansion of Genus' own authority always require human
approval; policy configuration cannot waive those boundaries.

Before enabling a real payment integration, complete provider due diligence,
threat modeling, key management, durable ledger and idempotency storage,
approval identity and separation-of-duties controls, webhook verification,
reconciliation, dispute/refund handling, incident response, retention rules,
and an independent compliance assessment for the actual deployment and data
flows.
