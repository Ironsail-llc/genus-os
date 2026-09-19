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
Live deployment verification, provider-state and subscription reconciliation,
provider billing reconciliation, and the real pilot remain deployment gates. Keep integration switches off until those gates are satisfied.

The [fleet artifact compiler](deployment.md#verified-fleet-artifacts) can package
the instance agents, workflows, shared knowledge, inactive settings and adapter
wheels as one verified candidate. Its fingerprint detects artifact drift; it does
not establish runtime deployment, provider connectivity or pilot approval.

## Native workflow execution

`fleet_release_id` optionally selects a verified release by its 64-character
SHA-256 fingerprint. The native sales runner loads it from
`$ROBOTHOR_WORKSPACE/.robothor/fleet-releases/<fingerprint>`, verifies the complete
artifact and captures its bytes before executing the selected agent. Missing or
changed artifacts stop the run; they do not fall back to loose manifests. A null
selection retains the existing manifest-directory behavior.

The selected run uses the release's native agent configuration and captured
instruction, bootstrap and declared warmup-context files. It does not merge
ambient fleet, project or environment configuration overrides. Mutable status
files remain in the normal workspace under the agent's existing write allowlist.
The request budget and tool restrictions still apply. The native run's trigger
detail records the release fingerprint, as does a structured stage's recovery
checkpoint. An admitted run retains its captured knowledge across subsequent
file changes; a later admission verifies the artifact again.

This selection pins agent configuration and knowledge for native sales stages.
The durable coordinator described below installs the selected workflow schedules
and coordinates database settings with runtime cutover. It verifies the deployed
platform and already installed plugins; staging alone does not perform these steps. Live business context,
platform behavioral rules and the skill catalog remain runtime inputs rather
than being frozen by the knowledge snapshot.

Queue admission also takes the tenant's shared `sales-fleet` maintenance gate.
An exclusive maintenance transaction blocks new ticks and is refused while a tick
is still active. Stop and inbox work continue independently alongside research
when no exclusive maintenance is running. Caller cancellation drains the bounded
worker before releasing its gate. The coordinator complements this primitive
with durable lease and unresolved-effect checks.

The [durable coordinator](deployment.md#durable-sales-deployment-transitions)
adds a preparing/committed/aborted transition ledger. A preparing deployment blocks
native queue admission until verified commit or restoration. Settings have a
monotonic revision, and managed release selection, agents and workflow bindings
can only change through this coordinator. Other operator settings remain editable;
a newer revision makes the prepared selection stale. Deployment and rollback keep
integration switches off. The native source-checkout verifier now integrates
daemon restart recovery, installed service-plugin checks and `/ready`.
[Helm deployment controls](deployment.md#helm-sales-deployment-controls) now expose
inspection, preparation, commit, restoration and rollback to verified human
operators. Provider connectivity and real production cutover remain separate gates.

Managed release queues additionally require the native
[schedule generation](deployment.md#managed-workflow-schedule-generations)
context. The scheduler reconciler fences retired callbacks and verifies workflow
and cron state before admission. The native runtime also verifies artifact and
installed-code identity off-loop before workers start. Copying workflow YAML into
a live directory cannot activate a managed release.

Instance workflow YAML calls `sales_process_queue` in a deterministic tool step.
`workflow_bindings` explicitly maps each stage to its authorized native service
workflow. No agent has this execution authority. Stages are `plan`, `scout`,
`research`, `qualify`, `contacts`, `verify`, `promotion`, `draft`, `conversation`,
`activation`, `delivery`, `stop`, `inbox`, `reconcile`, and `business`. Most calls handle one work
item; reconciliation reads up to five pages, and the planner creates a bounded set
of discovery jobs. There is no separate daemon.

Use separate workflows for stop requests, inbound conversations, delivery, and
research. Stop, inbox and reconciliation workflows remain scheduled when sending is paused. Other
workers respect their stage switches. Set `tool_timeout_seconds` in the native
workflow step to cover the worker's allowance and keep the enclosing workflow
timeout larger. Research workers allow 300 seconds; a 330-second tool step inside
a 360-second workflow leaves time for committing work.

Discovery planning runs on weekdays in the configured local window (02:00–07:00
by default). It rotates configured `discovery_segments`, queues batches of at most
20, and checks the daily admission limit, review backlog, and outstanding scout
jobs. Only segments with an active policy version are selected. A persisted daily
plan is not recreated on restart, and unused scout work expires at window end.
Candidate counts are enforced again when the scout commits; direct imports and
other work may have filled the available capacity since planning.

Validated model outputs are checkpointed under the work lease before domain
commit. A replacement worker reuses that output instead of making another paid
model request. Checkpoints preserve the original dossier/conversation context;
stale output still fails the domain version checks. A crash before the checkpoint
may require another request under a new funded work allowance.

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

Open **Sales → Review pilot settings** to edit daily/monthly spending ceilings,
the per-verification allowance, daily discovery and mailbox limits, review backlog,
timezone and discovery hours. The form previews changed values before saving;
amounts are converted to integer micro-USD without rounding the entered decimals.
This editor does not change integration switches, senders, active knowledge,
qualification policies or managed fleet bindings.

`GET /api/sales/settings` returns the current `config` and `revision` in one
snapshot. `POST /api/sales/settings/review` accepts `changes`, `expected_revision`
and a human reason, and allows only the pilot fields above. The domain checks the
revision while holding the same settings lock used for configuration changes.
A stale review returns 409 without mutation. Successful writes audit the verified
operator, reason, revision and before/after values. An uncertain dashboard response
requires reloading current limits and reviewing again; the UI never retries a save
automatically. Existing partial `PATCH /api/sales/settings` clients remain supported.

### Published policies and claims

Open **Sales → Review sales library** to inspect published qualification versions
by buying case and a published claim library. Review displays the exact weights,
required criteria, threshold, evidence age, claim text, supporting details and
publishing operator before selecting the versions. Large catalogs can be paged.
The same panel publishes a new version from a portable JSON review packet.

A packet has `kind` (`qualification` or `knowledge`), `version` and `data`.
Qualification data follows `QualificationPolicy`, with an explicit evidence
definition for every weighted criterion in `criteria_definitions`. The packet and
policy version labels must agree. Knowledge data includes 1–100 named text claims
and can retain dated sources, limitations and other supporting review details.
Packets are bounded to 128 KiB and neither the preview nor the publication path
fetches their source URLs or executes their contents.

`POST /api/sales/library/preview` validates and normalizes the packet, returning its
canonical content and hash without storing it. After inspecting the exact content,
the operator confirms review and supplies a reason. Publication posts that packet,
`expected_hash` and reason to `/api/sales/library/publication`. The server rechecks
the hash, requires a verified human operator and records the reason/hash in the
immutable version's publication audit. It does not change active selections or
integration switches. A changed existing version is refused; use a new label.

After an uncertain response, **Check published version** reads
`GET /api/sales/library/records/{kind}/{version}` and compares its content hash.
It never automatically repeats publication. The legacy human `/policies` and
`/knowledge` endpoints remain available for existing integrations.

`GET /api/sales/library?kind=qualification|knowledge` lists tenant-scoped immutable
records, ordered by version, with `after` and `limit` pagination. Selection uses
`POST /api/sales/library/selection` with the complete `policy_versions` map,
`knowledge_version`, `expected_revision` and a reason. Empty selections deactivate
that category. Missing published records and mismatched buying cases are refused,
including through the ordinary settings endpoint. The settings revision prevents
a stale operator review from replacing a newer selection. Integration switches
remain unchanged.

Active context matches kind, version and buying case explicitly. Reusing a version
label across knowledge and qualification does not activate both records. An
internal `library_revision` increases when the active selection changes; callers
cannot set it. Draft approvals bind to that revision. Changes cancel pending
reviews/approvals, invalidate already claimed send authority and queue pauses for
older campaigns. Restoring an earlier selection does not revive its old approvals.
Pause jobs target older library revisions, preserving campaigns reviewed under a
newer selection. Provider pauses are asynchronous and cannot recall a message
already sent or guarantee interception of an in-flight provider request.

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
| Instantly | `providers/instantly/api_key`, `providers/instantly/workspace_id`, `providers/instantly/webhook_secret` |

Application-specific signup and fulfillment adapters belong in the private
instance deployment. They must pin the authenticated organization and minimize
returned business fields. Signup readiness, order placement, administrative close-out and verified
fulfillment are separate milestones. Outcome import needs an authoritative
business-only projection and a confirmed practice mapping; webhook names alone
do not prove delivery. Patient records and clinical details are not sales inputs.

## Provider event intake

`POST /api/integrations/instantly/{tenant_id}/webhook` authenticates a configured
`Authorization: Bearer <webhook_secret>` header against the selected tenant's
vault. It separately checks the payload workspace ID. The path selects a vault;
body fields and tenant headers do not authorize access. Only this exact POST
bypasses JWT parsing. Other integration paths still require normal authentication.
The route authenticates before reading the body, caps it at 256 KiB, and stores
an allowlisted event without attachments or unrelated contact fields.

[Instantly documents custom webhook headers](https://help.instantly.ai/en/articles/6261906-webhooks),
not an HMAC signature contract. Configure specific supported subscriptions, not
“All events”: sent, replies (including automatic replies), bounce, unsubscribe,
not interested, wrong person, campaign completed and account error. Registration and
subscription-health checks are still activation gates.

A completed campaign-creation receipt binds each event to its original,
single-recipient Genus action. Supplied addresses must agree; missing optional
addresses can be resolved from that unique campaign. Unknown campaigns remain
unprocessed for reconciliation. Replies invalidate pending approvals and queue
campaign pauses within the intake transaction. Negative events also suppress the
recipient immediately. Known mailbox errors revoke its readiness and queue pauses
for only that mailbox. These controls do not depend on a research or sending switch.

The `inbox` worker retrieves the actual email from Instantly before committing a
message and follow-on work atomically. Workspace, campaign, direction, mailbox,
participants and provider message ID must agree. A scheduled email is not sent.
Automatic replies are stored and stop pending work without starting a reply loop.
Canonical sent events record **sent**, not inbox delivery. The message time is
[the provider's database insertion timestamp](https://developer.instantly.ai/api-reference/schemas/email),
not the sender-controlled email date. The real provider UUID and thread ID are
preserved; no reply identifier is fabricated.

Without an email UUID, one campaign-filtered page is checked for exactly one
matching body, subject, participants and nearby timestamp. Ambiguous matches,
additional pages, HTML-only bodies, missing campaign associations and unsupported
identities remain pending and eventually fail visibly after bounded retries.
The periodic scanner recovers messages independently of these event lookup jobs.
Ambiguous event-to-message identity still requires an operator repair interface.
Provider pauses are asynchronous: intake invalidates local authority
immediately, but an already in-flight provider request cannot be recalled.

## Periodic message reconciliation

The independent `reconcile` workflow scans only campaigns with completed Genus
creation receipts. Each tick reads one page of at most 100 records; one native
workflow invocation drains at most five pages. Schedule that workflow every minute
with a 240-second tool timeout and a larger enclosing workflow deadline. Each
individual page has its own fenced 120-second work lease. Sending can remain off.

A scan fixes its lower/upper timestamps and sorts ascending without collapsing
threads. Verified messages, stop/conversation work, provider observations, the
next cursor and job completion commit together. Restarts resume the page chain;
repeated cursors, conflicting identities, scheduled messages and malformed pages
hold the page rather than silently skip it. There is a 1,000-page bound per scan.
A campaign's next scan becomes eligible ten minutes after the prior scan's upper
bound, with a day of overlap. Daily it becomes eligible to revisit the full campaign history
to catch late indexing. This is eligibility, not a maximum recovery latency:
actual lag depends on the campaign/page backlog and provider availability.

The first attempt pins the workspace durably; a configuration change cannot
retarget a resumed cursor or a later scan of that campaign.
Every email-list request checks the configured workspace with the same credential
snapshot used for the page fetch. A database-backed sliding window admits at most
20 list attempts per 60 seconds across this tenant's workers, matching the
[documented email-list limit](https://github.com/Instantly-ai/instantly-starter-kit/blob/main/docs/api/emails.md).
Failed attempts count. Other applications or tenants sharing a provider account
also consume its provider limit; 429 responses defer the job without consuming a
processing attempt. No in-memory counter is treated as a global quota.

Webhook and polling observations deduplicate on the real message ID. Recovered
sent mail updates the matching initial or reply action receipt; unexpected
outbound content hands the conversation to human review. Neither sent mail nor a
completed campaign proves inbox delivery or customer activation.

After correcting a provider-read problem, a tenant owner/admin can call
`POST /api/sales/jobs/{job_id}/retry` with a 10–2,000-character `reason`. Only
inactive `sales.inbound`, `sales.reconcile`, and `sales.business` jobs can be reset. The reason and
actor are audited; delivery and CRM mutation jobs are excluded. This endpoint
retries the same page and does not authorize a changed identity or message.

`GET /api/sales/provider-reads` supplies the owner/admin recovery inventory.
`state=attention` (default) lists failed reads and pending reads with errors;
`state=all` also includes running/completed reads. Optional `kind` is restricted
to the three read queues above. The response contains at most 100 rows, newest
created first, and an `after` cursor for the next page. Cursor lookup is tenant
scoped. Only recovery metadata and allowlisted source/account/practice/campaign
references are returned, without job payloads or lease tokens.

Message backfill does not recover missed provider-only unsubscribe labels,
account-status changes or disabled subscriptions. Reconciliation of those states,
ambiguous thread/HTML handling, uncertain-write repair controls, and lag alerts remain
activation requirements.

## Reviewed business observations

Migration 128 stores allowlisted current practice, signup and order observations,
revision history, and tenant-scoped customer bindings. Source adapters normalize
business-only records before the domain accepts them; there is no agent-facing
raw import endpoint. Source, account, resource and external identity are separate
keys. A platform organization must never stand in for a customer practice.

`GET /api/sales/business-observations` lists at most 100 current records, with
`kind`, optional `source`/`account_id` filters and a UUID `after` cursor. The response
includes current evidence, revision and binding state. Tenant owners/admins can
review those records and use
`POST /api/sales/prospects/{prospect_id}/business-customer` with `observation_id`,
`expected_revision` and a 10–2,000-character `reason`. Tenant and actor come from
verified authentication, never the request body. Agents/service identities cannot
confirm a binding. Name similarity alone is not a confirmed association.

Several practices can belong to one customer, under one authoritative source
account. A practice cannot be attached to two customers. Legacy free-text customer
IDs and current observation bindings cannot be mixed; existing legacy attribution
needs an explicit migration. Provider account/deal candidates are evidence only:
external CRM adoption still needs independent account and entity verification.

A changed practice name or business-unit identity holds its association, transfers
automation to human review and cancels pending message approvals. Reconfirmation
requires the current revision. Account inactivity changes readiness without erasing
historical order evidence. The ordinary binding endpoint cannot move a practice
between customers or migrate its source account.

Migration 129 adds a binding review generation and remembers when a customer has
lost its final practice association. To correct ownership, a human owner/admin
uses `POST /api/sales/business-observations/{observation_id}/reassign` with
`expected_revision`, `expected_prospect_id`, `target_prospect_id`,
`expected_binding_version` and a 10–2,000-character `reason`. The source account
and practice identity stay fixed. The target must be a different customer in the
same tenant, with compatible current attribution and no legacy mapping.

The repair locks the source and both customer records, checks the reviewed match,
then moves the binding and its current order attribution in one transaction.
Both customers receive an audit entry and a new outcome version; their pending
message approvals are cancelled, stop work is queued, and agent-owned
conversations move to human review with status `customer_review`. Existing human
ownership is preserved. The repair does not grant renewed sending authority.
The review generation rejects a stale request even after an association has moved
away and back to the same customer. Partial failure rolls back both sides.

If the former customer has other reviewed practices, their observations remain
attributed to it. If it loses its final match, attribution and retention become
unknown with `requires_review=true` and `coverage_complete=false`; the system
cannot silently fall back to legacy metrics or interpret the missing match as no
orders. A new reviewed binding restores observation attribution. Source-account
migration and source-side order regrouping still require separate reconciliation.

`BusinessObservations.commit_page` is the durable import boundary for a leased
`sales.business` job. It checks the stored scan identity and cursor, commits all
observations/history/customer revisions, and queues the next page in the same
transaction as work completion. Expired leases, mismatched accounts, repeated
cursors and malformed records cannot partly advance a page. Empty pages never
remove previously observed records. A scan is bounded to 1,000 pages.

Repeated identical observations do not generate another outcome transition.
Changed revisions preserve history; a previously revoked fulfillment can be
restored even when the source returns its original content revision again. Older
observations and reused revisions with different content are rejected. Order
reassignment requires reconciliation. Source clocks and stable external identities
are part of the adapter contract; timestamps are not inferred from order names.

Retention uses current verified observations for reviewed customers. Counts and
first dates refer to observed orders, not a certified complete history. These
current-page feeds report `coverage_complete=false`; 30/60/90-day cohort metrics
remain unknown until complete historical coverage and the cohort anchor are
proven. A held identity makes attribution unknown as well. At least one verified
commercial fulfillment can establish activation; a partial empty history instead
leaves the prospect `awaiting_outcome_evidence` and cannot create an onboarding
message in the activation stage.

### Native business source registration and polling

An instance plugin contributes a tenant-bound adapter factory through the existing
`genus.services` group under `sales.business.<source>`. The factory takes the
tenant ID and returns an object with async `business_page(scan)`. It returns a
validated `BusinessPage` (or equivalent dictionary) for that exact `BusinessScan`.
The adapter owns scoped authentication and allowlisted provider payload validation;
Genus owns persistence, review, cursor progress and worker recovery. Plugins retain
the existing manifest, contract-version, lockfile and disabled-plugin checks.

The operator must configure `business_sources`, enable `outcomes_enabled`, and bind
the `business` stage to an installed native workflow. Sources have `source`,
`account_id` and `refresh_seconds` (default six hours; minimum ten minutes). Only
one account per source can be configured. Empty sources and the default disabled
outcome switch perform no reads. Installation/import alone starts no work.

Each workflow invocation plans at most 25 new scan roots and advances one page.
Practice and signup scans cover the configured source account; order scans cover
only currently reviewed practice bindings. The planner serializes concurrent
invocations, resumes existing cursor chains, and waits the refresh interval after
a terminal page. Failed chains require the existing operator read-retry action;
the planner cannot replace them with fresh roots. An empty page is not evidence
of deletion or full historical coverage.

The worker limits admission to 20 page attempts per source/account/minute across
workers (an adapter may make multiple HTTP calls per page). Reads time out after
75 seconds under a 120-second lease. Give the workflow tool step at least 100
seconds and its enclosing workflow more time. Provider rate limits preserve the
page and retry delay without consuming an attempt. Other failures have redacted
diagnostics and bounded retries; a replacement lease owns recovery after expiry.
Account, outcome switch and practice review are checked before reading and again
in the page transaction, so pausing or changing identity during a read cannot
commit its results. Job completion checks wall-clock lease expiry.

Source-account migration, complete-history certification,
atomic instance deployment and production connection checks remain before live
activation. The native workflow and provider tests use synthetic business data;
they do not establish deployed-runtime or customer-pilot success.

### Reviewing and recovering in Helm

In **Sales**, **Review imported practices** opens a paginated source inventory.
Each practice shows its source account, practice/business-group identity, active
state, observation time and current or held match. Select **Review match**, choose
a Genus customer, record the matching evidence, and explicitly confirm ownership.
The submission binds the exact revision displayed during review. A stale revision
or changed association discards the form and requires refresh/review. A held match
can be reconfirmed for its current customer. **Repair customer match** opens the
separate reassignment form: review the existing customer, select the corrected
owner, record the evidence, and acknowledge review of both conversations. The form
explains that pending approvals are cancelled and both conversations held.
Customer choices currently come from the latest 200 prospects in the Sales workspace.

**Inspect provider reads** opens the recovery inventory. Filter by read type or
show all statuses, inspect the failed read's scope, then use **Review read recovery**
to record what was repaired. **Retry this read** resumes only that read. Running
and completed rows have no recovery button; the API enforces the same boundary.
Pagination failures preserve existing rows and the cursor for another attempt.
Changing the practice source ignores late responses from the previous selection.
Both panels load only within the visible owner/admin Sales view.

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

Migrations 126–129 add tenant-scoped work, budgets, event inbox, audit, immutable
actions, external effects and sales records. Work leases are fenced; domain
updates and follow-on jobs commit together. Daily and monthly spending admission
and settlement are atomic across both scopes. Admission locks current settings
and the current work lease; a waiting worker cannot restore an older cap. Budget
periods use UTC and charge the period in which the work allowance was admitted.

Native sales runs activate the engine's shared `request_budget` envelope. Every
main, pooled auxiliary, retry and streaming model request reserves a worst-case
allowance before dispatch. The initial policy supports text-only OpenRouter
requests with current anonymous endpoint metadata: it reserves the full published
input context plus capped output, pins one endpoint, sets provider price ceilings,
and disables hidden SDK retries and provider fallback. Other providers, paid
server tools, multimodal inputs, tiered pricing and explicit cache-write charges
need a supported pricing policy before they can run within this envelope. See
[OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection#max-price).

Confirmed response costs release unused allowance; interrupted or unpriced
responses retain their full request charge. A run that crashes retains its full
durable run reservation. Reported run cost includes these conservative charges,
not just confirmed billing. Provider charges above their declared bound are
recorded as overruns and stop the envelope; the system cannot undo provider billing
or spending through other applications. Production reconciliation is still a
rollout requirement. See [provider usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting).

The scope follows async helper work and closes when the run returns, preventing
detached children from spending later. Paid Brave and Perplexity search have no
pricing policy in this envelope yet; bounded research uses self-hosted search.
Other engine runs retain their existing behavior unless explicitly placed inside
a funded request-budget scope. Native scheduling remains inactive pending the
remaining deployment gates.

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
