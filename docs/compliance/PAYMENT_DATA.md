# Payment-data boundary

Version: 1.0
Last updated: 2026-07-13

This document defines the intended technical boundary; it is not a PCI DSS
attestation, scope determination, or legal opinion. Outsourcing payment capture
can reduce scope, but it does not automatically remove Genus OS or its operator
from PCI DSS obligations. Validate the implemented data flow with the acquirer,
payment provider, and a qualified assessor.

## Non-negotiable rule

Genus OS does not accept, persist, log, embed, back up, or transmit raw primary
account numbers (PAN), full track data, PIN/PIN blocks, card-verification codes
(CVC/CVV/CID), or cryptograms.

That rule applies to both customer cards and cards owned by the organization or
assigned to Genus. Ownership is not a security or compliance exemption. In
particular, card-verification codes must not be retained after authorization,
including for recurring or card-on-file use. See the PCI SSC guidance on
[outsourced processing and merchant responsibility](https://www.pcisecuritystandards.org/faqs/1217/)
and [storage of card-verification codes](https://blog.pcisecuritystandards.org/faq-can-cvc-be-stored-for-card-on-file-or-recurring-transactions).

## Customer/client payments

The customer enters card data only into a provider-hosted page, redirect,
iframe, or provider-controlled SDK that sends the card directly to the payment
provider. Genus receives only:

- an opaque provider/customer/payment-method token;
- safe display metadata such as brand and last four digits when supplied by
  the provider;
- provider event/transaction IDs, amount, currency, status, and timestamps;
- the minimum billing/contact metadata required by the documented workflow.

`ClientPaymentMethodReference` rejects unknown/raw-card fields. Provider tokens
are credentials: they must be encrypted through the secret boundary, redacted
from representations and errors, access-scoped, rotated/revoked when supported,
and excluded from analytics, prompts, traces, and ordinary application tables
unless a reviewed encrypted persistence design explicitly requires them.

## Genus operational spend

Genus may operate an organization-owned virtual card only through an issuer or
payment provider reference. `OperationalVirtualCardReference` contains an
internal ID, safe display metadata, ownership/scope, and an opaque provider
reference. It does not contain the PAN or CVC/CVV.

If a vendor checkout requires card entry, use a provider-controlled reveal or
checkout mechanism that keeps raw credentials outside Genus OS. Do not give an
agent a screenshot, clipboard value, browser recording, prompt, tool argument,
environment variable, log entry, database column, or memory fact containing the
card number or verification code.

Every proposed spend must pass the deny-default `SpendPolicyEngine`, including:

- matching tenant and organization ownership;
- an active approved operational card reference;
- vendor, category, currency, transaction, daily, and monthly constraints;
- committed plus reserved usage accounting;
- idempotency and collision detection;
- the required human approval threshold and authority tier.

Production changes, code self-modification, policy mutation, and expansion of
Genus's authority always require human approval. A spend policy cannot waive
those boundaries.

## Provider integration requirements

There is intentionally no live payment adapter in the production-hardening
slice. Before adding one:

- complete provider/acquirer due diligence and an environment-specific PCI
  scope/data-flow assessment;
- use separate adapters and credentials for customer payments and operational
  treasury;
- verify webhook signatures against the raw body, enforce timestamp windows,
  and persist replay/idempotency state transactionally;
- model authorization, capture, settlement, decline, reversal, refund, dispute,
  partial failure, timeout, and reconciliation explicitly;
- add durable decision, reservation, approval, and append-only ledger storage;
- implement separation of duties, card freeze/rotation, provider-side limits,
  anomaly alerts, statements, and daily reconciliation;
- test that logs, traces, metrics, exceptions, snapshots, LLM prompts, and audit
  payloads never contain provider secrets or prohibited card data;
- maintain incident response, vendor contacts, evidence retention, and tested
  revocation procedures.

## Snapshots and regulated data

Provider tokens, transaction records, customer information, and approval/audit
events can still be sensitive or regulated even though raw card data is absent.
Production snapshots must be encrypted, access-logged, retained/deleted under
policy, replicated to a separate failure domain, and restored only through an
approved incident or rehearsal process. Snapshot encryption does not change
PCI scope or permit prohibited card data to enter the database.

## Current enforcement and gaps

Implemented:

- distinct strict domain models for customer tokens and Genus virtual-card
  references;
- rejection of raw-card-shaped fields;
- secret-safe representations, audit fingerprints, and ledger redaction;
- deterministic deny-default spend decisions, authority tiers, and local
  idempotency/tamper-evident ledger contracts;
- no live provider or network transaction implementation.

Required before live use:

- durable transactional stores and migration;
- a reviewed provider adapter and secret/key lifecycle;
- webhook/reconciliation/dispute workflows;
- independent security testing and actual PCI scope determination;
- operator policies, training, monitoring, incident response, contracts, and
  evidence.
