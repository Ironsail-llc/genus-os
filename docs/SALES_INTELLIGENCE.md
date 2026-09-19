# Native sales intelligence

The Sales view lets tenant owners and admins inspect evidence-backed prospects,
review exact outreach drafts, pause integration stages, stop outreach to a
contact, and take over a conversation. Agents can research and propose work;
they cannot approve messages or select another tenant.

Structured workers cover discovery, research, deterministic qualification,
contact research, provider verification, initial drafting, inbound conversation
classification and activation guidance. Discovery admits at most 20 new domains
per configured local day by default and stops at the review backlog limit.
Candidates already known to Genus do not consume another admission.

**Implementation status:** the domain, review API, view, provider adapters and
explicitly constructed workers are available as a foundation. Importing the
package does not install a schedule, activate integrations or send email.
Automated deployment, the complete stage runtime, provider event ingestion,
strict per-request spending enforcement, and the real pilot remain deployment
gates. Keep integration switches off until those gates are satisfied.

## Records and responsibilities

Genus stores companies and people in its native CRM. Sales records add versioned
research, dated evidence URLs, unknowns, deterministic qualification, approvals,
external IDs and conversation history. Customer associations require human
confirmation; names alone are not a safe identity match.

Pipedrive promotion follows human acceptance of a specific dossier and policy
version. Existing possible matches are held for identity review. Provider writes
have durable receipts so partial failures do not create another organization,
person or lead on retry. An onboarding application's existing signup funnel
should retain ownership of its deals and stage progression.

Instantly receives one reviewed message at a time. An initial message creates
one dated, single-recipient, single-step campaign; activation has a second
authorization check. Follow-ups and replies require a known thread and their
own approvals. Campaign activation records **scheduled**, not delivered.

## Review and stopping

- Open **Sales**, select a prospect and inspect its evidence and unknowns before
  accepting it. A changed dossier or qualification invalidates a stale decision.
- Inspect the exact sender, recipient, subject, body, claim IDs and evidence IDs
  before approving an email. The executor rechecks current evidence, contact
  verification, suppression, ownership, conversation revision and active policy.
- Turning delivery off queues provider campaign pauses. Contact suppression and
  human takeover also queue stop work. The stop worker must remain running while
  sending is paused. Provider-side propagation is asynchronous; a message already
  delivered cannot be recalled.
- An uncertain write is **unknown**, never eligible for automatic resend. Check
  the provider and reconcile its receipt through an authenticated operator path.
  `Effects.reconcile` is the current library boundary; a console reconciliation
  workflow is still required before production activation.

Default pilot delivery policy admits weekdays from 09:00–16:00 in the configured
IANA timezone. Queued and uncertain messages consume the per-mailbox daily cap.
Mailbox admission requires a current human readiness review, active provider
status, known warmup history of at least 14 days and a reported score of at least
90. These are conservative initial settings, not guarantees of deliverability;
domain authentication and mailbox usage outside this workflow need operator review.

## Configuration and secrets

The authenticated `/api/sales` API derives tenant and human actor from verified
claims. Settings include separate research, enrichment, promotion, sending and
outcome switches; sender identities; postal address and opt-out URL; daily and
monthly allowances in integer micro-USD; active policy/knowledge versions; and
stage-to-agent mappings. All integration switches default off and spend limits
default to zero. `mailbox_approved_until` records readiness review expiry.

`verification_allowance_units` must be configured from the actual subscription's
credit economics before paid verification runs. The worker books that complete
allowance per lookup; provider credits are not assumed to be dollars. A pending
verification is polled by email without purchasing it again. Only a provider
result for the requested email, marked verified with a definitive false catch-all
flag, becomes a valid address. Unknown purchases retain their budget reservation.

Conversation decisions cannot grant send permission. Opt-outs immediately create
local suppression and provider stop work; complaints and custom commitments set
human ownership. Reply drafts must address the triggering thread. A new customer
milestone invalidates older pending or claimed message authorizations, just as a
new conversation revision does. Activation status derives from recorded fulfilled
orders, independently of the agent's proposed onboarding explanation.

Keep credentials in the tenant vault:

| Provider | Keys |
|---|---|
| Pipedrive | `providers/pipedrive/api_key`, `providers/pipedrive/company_domain` |
| Instantly | `providers/instantly/api_key` |

Application-specific signup and fulfillment adapters belong in the private
instance deployment. They must pin the authenticated organization and minimize
returned business fields. Signup readiness, order placement, administrative close-out and verified
fulfillment are separate milestones. Outcome import needs an authoritative
business-only projection and a confirmed practice mapping; webhook names alone
do not prove delivery. Patient records and clinical details are not sales inputs.

## Native agent deployment

Store manifests under the instance's `docs/agents/`, instructions and the shared
Sales Brain under `brain/`, and workflow declarations in its native workflow
directory. Keep business-specific claims, buying cases, pricing, geography and
provider configuration in a private version-controlled instance repository.
Scaffold through the native agent builder; do not put instance data in platform
source. Workers run the existing Genus runner with explicit tenant, service role,
cost allowance, timeout and structured output contract.

Declare `role: sales_agent` and narrow each manifest's `tools_allowed`. The role
grants web research, constrained file writes and the four sales tools; it has
no approval, vault or provider-send capability. File writes additionally need
the `write_path_restrict` guardrail and an explicit status-file allowlist.

## Recovery and validation

Migrations 126–127 add tenant-scoped work, budgets, event inbox, audit, immutable
actions, external effects and sales records. Work leases are fenced; domain
updates and follow-on jobs commit together. Unknown costs retain their reservation
until reconciled. Run-level reservations do not yet provide a hard per-request
spend ceiling, so autonomous scheduling remains gated on that engine upgrade.

The automated tests exercise real isolated PostgreSQL transactions and synthetic
HTTP transports. They cover tenant boundaries, concurrent budgets, stale reviews,
suppression races, exact-message authorization, uncertain writes, provider
contracts, native stage output and mature retention windows. They do not establish
provider production connectivity, model research quality or live sales results.

`test_pipeline_rehearsal.py` runs the workers from discovery through a reviewed
initial message, reviewed reply, opt-out and verified business outcome in the
isolated test database. Its model answers, operator decisions, external receipts
and customer milestones are synthetic. This is component integration evidence;
it is not a native deployed-runtime test or a successful customer pilot.

An end-to-end pilot additionally needs calibrated buying cases, reviewed dossiers,
healthy authenticated mailboxes, individually approved test and prospect messages,
provider send/reply/opt-out evidence, restart recovery, and independently verified
customer fulfillment. Report 30/60/90-day retention only for mature cohorts.
