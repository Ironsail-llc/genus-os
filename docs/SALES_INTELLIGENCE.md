# Native sales intelligence

The Sales view lets tenant owners and admins inspect evidence-backed prospects,
review exact outreach drafts, pause integration stages, stop outreach to a
contact, and take over a conversation. Agents can research and propose work;
they cannot approve messages or select another tenant.

Structured workers cover discovery, research, independent evidence assessment and deterministic qualification,
contact research, provider verification, initial and follow-up drafting, inbound conversation
classification and activation guidance. Discovery admits at most 20 new domains
per configured local day by default and stops at the review backlog limit.
Candidates already known to Genus do not consume another admission.

### Reviewed follow-ups

The existing draft workflow can plan cold follow-ups when
`followup_delays_business_days` is explicitly configured. The default empty list
disables them. The settings review accepts at most two integer delays of 1–30
business days; each delay starts from the preceding **confirmed send**. For
example, `[3, 4]` waits three business days after the initial send, then four after
the first follow-up is actually sent. Business days mean Monday through Friday,
preserving the local hour across daylight-saving changes; holidays are not excluded.

An activated campaign or provider acceptance is insufficient. Planning verifies
the original human-approved action, owned campaign and activation, canonical
message receipt, participants and any subsequent approved follow-up chain. It
requires current qualification, evidence, verified contact, sender, ownership and
knowledge. A completed campaign scan must cover the last 15 minutes, with a full
scan within a day. Pending events or unfinished scans hold work. Any inbound
message, including an automatic reply, or customer milestone ends the cold
sequence. Rejected, cancelled or uncertain email work prevents automatic retries.

The native SDR receives a code-generated follow-up basis with the exact thread
and participants. It cannot choose the cadence or grant permission to send. The
queue deduplicates each campaign/ordinal, rechecks eligibility before generation
and commit, and produces a new immutable action for individual human review.
Sending rechecks the same basis and the current provider workspace. Follow-ups
share the existing mailbox quota, health checks and weekday sending window with
initial messages and replies. Changing queued cadence values stops that job for
operator review; it does not replace approved content or regenerate failed work.

These conservative holds can require manual handling. For example, a scan that
becomes stale after approval causes delivery to cancel before a provider write;
the cold planner will not recreate that message. Inspect provider reads and the
action receipt before preparing a separately reviewed manual reply. Local tests
use simulated provider responses; live scheduling and delivery remain pilot gates.

Native scouts must complete a real `web_search` before returning a candidate
batch. The required tool turn precedes final-answer JSON formatting; afterward,
the native runner requests and validates the strict `CandidateBatch` schema.
Each source URL and company website domain must occur in that run's captured
search or successful page-read results. The supplied batch allowance and domain
deduplication are validated before completion. A real empty search can return an
empty batch; failed searches cannot fabricate successful discovery. When the
search tool marks results degraded, the scout must refine its query until one
search is not degraded or three distinct queries have completed. Final-answer
formatting remains deferred during those required search turns. The durable
scout receipt retains source observations and the native run identity. When a
search returned URLs but no candidate is selected, `empty_reason` must explain
the exclusion or unresolved requirement; the completion receipt preserves it.
This enables review without requiring invented positive results. These
checks establish observed URLs, not business fit, which still requires research
and independent qualification.

When `agents.qualify` is configured, qualification runs that native agent before
calculating a score. It receives the published policy definitions and captured
passages without the researcher's boolean labels or earlier scores. Its strict
`QualificationAssessment` contract covers every policy criterion with a
supported/disproved/unknown status, source IDs and explanation. Only code
calculates points. Unknown remains distinct from explicit contrary evidence;
expired, future or stale passages cannot earn points. The original dossier is
preserved alongside the assessment and run receipt, and Helm displays both.
Frozen cohort reviews reveal model assessment reasoning only after the original
human assessment, preserving the existing blind-review boundary.

Assessment work uses the existing request budget, bounded correction attempts
and durable job checkpoint. Commit checks the current lease, dossier content and
version, active policy, agent and release against the stored checkpoint. Restarts
can reuse valid paid output. Changed inputs hold the work for fresh research;
they cannot silently rescore from old researcher labels. Configured fleets also
require a current assessment before contact enrichment, verification, human
acceptance, promotion, drafting or delivery. Existing unassessed scores must be
reassessed after an upgrade; they are not grandfathered. Instances without a
qualifier binding retain the existing deterministic evidence evaluation.

These checks establish provenance and workflow correctness, not semantic model
accuracy. Representative evaluation and the human-reviewed pilot remain required.

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

### Human qualification quality review

Before real dossier sampling, use role-specific native agent benchmarks with
synthetic business sources. Use native `expected.json_assertions` for decisive
fields and `require_all: true` to require every declared check to pass. The source
runbook `docs/runbooks/BENCHMARK_HARNESS_FAIRNESS.md` documents those contracts.
Offline grader fixtures, measured model quality and human customer calibration
are separate evidence. Native sales read/write tools are deliberately excluded
from benchmark children; fixtures must not depend on live prospects or policies.

Open **Sales → Assess qualification quality** after publishing and selecting the
policies to evaluate. Create a named 100-dossier cohort with a review purpose. The
cohort fixes an 85% initial agreement target and captures the active policy map,
full policy contents and settings revision. The authenticated API also supports
other explicit sample sizes and targets; a smaller test cohort does not satisfy
the 100-dossier pilot requirement.

**Enroll next eligible dossiers** fills the remaining sample slots in creation
order. It includes qualified, rejected and needs-research model decisions under
the cohort's exact policy versions. Enrollment freezes each dossier, qualification,
policy and content hash. Later research does not rewrite those snapshots. Sample
membership is fixed; additional enrollment only fills vacant slots. Concurrent
enrollment cannot exceed the sample size or enroll a prospect twice.

Review the business evidence and unknowns, then record a human fit assessment and
reason. The screen reveals the model result after the first assessment to reduce
anchoring. This is a presentation choice, not an API secrecy boundary. An assessment
does not accept a prospect, promote it to an external CRM or approve a message.
Corrections append a new revision and require the currently displayed assessment
ID and frozen snapshot hash. Stale or uncertain responses require reloading; the
dashboard does not automatically retry a judgment.

The report keeps original and latest assessments separate, including agreement,
human-qualified count, unresolved human judgments, false positives, missed fits
(including model abstentions) and precision among model-qualified prospects with
decisive human reviews. Per-buying-case results expose uneven performance. The
initial target requires every planned dossier to have a decisive original human
assessment; missing or uncertain judgments cannot pass it. Corrections remain
visible but cannot rewrite the original target result. Use a separately identified
cohort after changing policies and disclose reused businesses as in-sample evidence.

This is an ordered convenience sample, not a randomized estimate of all prospects.
It excludes unresearched and differently versioned records and can reflect discovery
and website-visibility bias. Human qualification agreement does not establish
customer conversion, commercial fulfillment or complete existing-customer history.

Migration 131 stores tenant-scoped cohorts, frozen items and append-only assessment
revisions. Human operator routes under `/api/sales/calibration` provide cohort
creation/listing, enrollment, paginated items, individual assessment and reports.
All use authenticated tenant/actor identity; agents cannot record reference labels.

### Verification and conversation handling

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

An activation decision's `human_required` identifies a nonstandard issue requiring
human judgment, such as custom terms, clinical questions or disputed records.
Routine waiting for fulfillment remains pending, without becoming an exception.
This flag is separate from the mandatory review of every outbound draft; false
never grants sending permission. The output schema includes this distinction.

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

For object-returning stages, set `model.response_format: json_object` in the
manifest. The shared runner passes this mode to provider requests without
changing other agents. Stage validation remains mandatory: research criteria
must reference at least one evidence item. Omit unknown criteria instead of
emitting an empty reference list; the dossier's generated JSON Schema exposes
this constraint to the model.

Declare `role: sales_agent` and narrow each manifest's `tools_allowed`. The role
grants web research, constrained file writes and the four sales tools; it has
no approval, vault or provider-send capability. File writes additionally need
the `write_path_restrict` guardrail and an explicit status-file allowlist.

### Bounded research delegation

A research parent may use `role: sales_research_agent` and the narrow
`sales_research_parallel` tool after migration 132. It must run from a verified
fleet release, permit exactly one research-worker agent, and declare
`v2.can_spawn_agents: true`, `max_spawn_total: 3`, `max_spawn_batch: 3`, and
`max_nesting_depth: 1`. Its allowed tools are the research broker and optionally
constrained status writes; generic spawn tools are refused at stage admission.
The broker is available only within the matching tenant's active native research
stage. It accepts only a buying case from that stage's approved policy context.

Until the broker starts, the native stage requires that exact function in the
provider's `tool_choice` contract. This is a scoped runtime requirement, not an
instruction supplied by a web page or a permission grant. It can select only a
tool already available to the parent. Auxiliary requests without tools are
unchanged; later parent calls use normal selection. Each fresh research child has
its own scoped first-read requirement: `web_fetch`, or `web_render` for a worker
that permits only rendered reads. The reviewed child manifest must contain at
least one page retrieval tool. After an actual native read attempt, even a failed
one, that child returns to ordinary selection so it can try another URL or the
renderer. Search snippets do not satisfy this requirement. Siblings cannot clear
each other's requirement, and the internal child-scope hook is not a model tool
argument. A provider that ignores the requirement still fails source attestation.
The requirement
closes with the stage, including for tasks that inherited its context. Endpoint
quotes must support both tools and forced tool selection: the endpoint's specific
`supports_tool_choice.function` flag must be true, not merely a generic
`tool_choice` entry in its supported parameters. A provider that ignores
the request still cannot bypass the validated-child merge required for success.

Genus supplies the company context and dispatches three native children for
services, providers/locations, and ownership/business signals. The parent and
children share one funded request envelope and the research job's 300-second
deadline. Child manifests come from the same verified release and are checked
before the parent starts: read tools, no spawning/continuous work/downstream
agents, a hard cost cap of at most $1, timeout at most 180 seconds, and safety cap
at most 20 iterations. The dedicated role grants research reads and the broker,
but no generic spawn, CRM mutation, approval, credential, or send permissions.
Child tool allowlists exclude the broker. Repeated broker calls return an already
completed bundle or refuse; they never start a second bundle in that stage.

All three children must complete with distinct native run IDs and valid Dossiers
for the selected buying case. Code namespaces their evidence IDs, combines their
criterion references, and preserves contradictions and unknowns in a fixed topic
order. Conflicting parent domains become an explicit unresolved ownership issue.
The aggregate must fit the existing 200-evidence dossier limit. Only this merge
becomes the stage output; facts added in the parent's final narrative do not enter
the committed dossier. The original parent and child outputs remain in native run
records. Child run IDs, output hashes, and the merged hash survive checkpointing
and final job completion. An incomplete bundle is not qualified; completed whole
stage checkpoints can be reused after a domain-commit failure.

Native research also requires retrieval evidence from each child. A scoped engine
observer captures successful `web_fetch` and `web_render` results after the permission gate and
native handler, before verification annotations. It does not treat search snippets,
model output, another child's fetches, or cached-repeat responses as new sources.
The observer adds engine-issued passage references under `_workflow_context` in
the tool result. Each passage is a continuous slice of at most 800 characters.
Native workers return `ResearchDossier` selections containing `source_ref` and
`passage_ref`, with the evidence field, value and criterion references. They do
not author quotation text, URLs or capture dates. Genus resolves the selection
against that child's own captures and creates the normal CRM `Dossier`, including
the exact returned URL, passage text and engine-observed `retrieved_at`. Invented
or foreign references fail. Both fetched and rendered content, including passage
metadata, remain wrapped as untrusted external data in model context.
Missing retrievals or unmatched citations refuse the
child before its output is saved. Even a dossier with no citations requires at
least one successful retrieval; an inaccessible site remains unresolved.

The immutable child fragment retains up to 32 returned source texts of at most
8,000 characters each, their hashes and retrieval times, and the tenant/agent/run
identity. These business-source texts stay in the tenant-scoped operations store;
the merged provenance carries only a source-proof hash. Recovery rechecks the
texts, citations, timestamps and output hash. Version-2 proofs also retain the
worker's selections and resolve them again during recovery; a changed selection
cannot silently change the saved dossier. Existing version-1 proofs with full
attested quotations remain valid. Legacy fragments without this proof
cannot enter a native research stage. The input contract version is changed, so
old fragments require review/new research rather than silent reuse. Raw failed
agent output remains diagnostic material, not accepted sales evidence.

Retrieval matching proves that quoted text was retrieved, not that a model's
interpretation, summary, or criterion assignment is correct. Human qualification
review is still required. JavaScript-only pages may return an unusable shell;
workers can use the separately permissioned `web_render` reader after migration
134. Its sandboxed GET-only retrieval has the same citation checks and bounded
source retention. See [rendered public reads](TOOLS.md#reading-javascript-pages)
for installation and limits. A failed renderer leaves the facts unresolved;
it does not authorize invented quotations or a browser-permission bypass.

Migration 133 adds immutable, tenant-scoped `operation_fragments`. Native research
saves its selected buying case before spawning, then checkpoints each validated
child as it finishes, without waiting for its siblings. A new attempt of the same
job loads those topics and dispatches only the missing ones. All topics must still
validate and merge before the dossier is committed; the parent model may run again
to finish orchestration. Each attempt retains the ordinary job-attempt, spawn and
funded-request limits. Previously uncertain charges remain reserved.

Reuse binds to the exact company/context, policy content, agent ID, fleet release
and output schema. Changed inputs, duplicate child receipts, altered stored bytes,
or a different native stage identity refuse reuse; fragments are never replaced
or transferred to another job. Writes require the current work lease and deadline,
rechecked after acquiring the database lock and immediately before insertion.
Cancellation preserves already committed fragments. A crash before a fragment is
saved can still require paid re-research under a new allowance; this is not an
exactly-once guarantee for remote requests. Changed-input holds require review or
newly authorized research work rather than silently discarding paid evidence.

This tool is withheld from offline benchmarks. Synthetic inline-evidence grades
do not validate live tool use, delegated research quality, or the human pilot.

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
and disables hidden SDK retries and provider fallback. JSON-mode calls filter out
endpoints that do not advertise `response_format` before reserving or dispatching;
an incompatible cheaper endpoint cannot displace a compatible one.

A funded run remembers endpoints that return HTTP 429/500/502/503/504 or a
`TimeoutError`. Its next quote for that model excludes those endpoints while
preserving the configured provider allowlist, exclusions, privacy constraints and
price ceilings. An alternative needs its own full reservation; the failed
request's unknown charge is not refunded. No extra retry loop or automatic
provider-side fallback is introduced. Authentication errors and caller cancellation
do not exclude endpoints, and health exclusions do not persist into a new run.
Already in-flight concurrent requests cannot be recalled. Streaming failures apply
the same exclusions while retaining uncertain costs.

If a model has no eligible endpoint, the engine advances through the agent's
declared model fallback chain. This also covers a route excluded by another
worker sharing the run's budget. Each fallback needs a fresh compatible endpoint
quote and spending reservation; provider restrictions are not relaxed. Exhausted
funding, unverified pricing and unpriced request features still stop the run.
The same distinction applies to streaming and auxiliary model calls.

Mandatory named-tool turns omit final-answer `response_format` and its JSON-only
instruction. The tool argument schema still applies. Once the required tool has
run, normal final-answer formatting resumes, including the research dossier
schema after retrieval. Source attestation and local output validation remain
mandatory; JSON text that resembles tool arguments is never executed as a tool.
Citation correction feedback identifies up to eight invalid evidence field
positions and distinguishes an unread URL from a non-verbatim excerpt. It includes
no model-supplied quotation, identifier or URL as an instruction. Corrections
still share the existing two-attempt, time and spending limits.

Other providers, paid
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
detached children from spending later. Bounded native web search can use an
existing Brave key. It reads the current standard Search rate from
[Brave's public pricing](https://brave.com/search/api/), caches the quote for at
most 60 seconds and reserves each HTTP attempt before dispatch, including retries.
Tool receipts retain the rate document hash, retrieval time, response status and
micro-USD estimate. Credits are not deducted; failed or unknown attempts retain
their reservation. These estimates are not verified invoices or custom enterprise
rates. Missing/ambiguous pricing or insufficient allowance skips the paid request
and names the reason alongside self-hosted fallback results. Perplexity remains
unpriced in this envelope. Explicit self-hosted searches never select Brave.
Other engine runs retain their existing behavior unless explicitly placed inside
a funded request-budget scope. Native scheduling remains inactive pending the
remaining deployment gates.

The automated tests exercise real isolated PostgreSQL transactions and synthetic
HTTP transports. They cover tenant boundaries, concurrent budgets, stale reviews,
suppression races, exact-message authorization, uncertain writes, provider
contracts, native stage output and mature retention windows. They do not establish
provider production connectivity, model research quality or live sales results.

The native research integration test also runs the full AgentRunner and real
tool registry/RBAC path for one parent and three children. It checks stored run
lineage, source-tool calls, the deterministic merged dossier, and shared accounting
including auxiliary model requests. Only the remote model and web responses are
fixtures; this is stronger wiring evidence than a stubbed stage runner, but still
does not establish live research quality.

`test_pipeline_rehearsal.py` runs the workers from discovery through a reviewed
initial message, reviewed reply, opt-out and verified business outcome in the
isolated test database. Its model answers, operator decisions, external receipts
and customer milestones are synthetic. This is component integration evidence;
it is not a native deployed-runtime test or a successful customer pilot.

An end-to-end pilot additionally needs calibrated buying cases, reviewed dossiers,
healthy authenticated mailboxes, individually approved test and prospect messages,
provider send/reply/opt-out evidence, restart recovery, and independently verified
customer fulfillment. Report 30/60/90-day retention only for mature cohorts.

### Research provider selection

Research manifests may set `model.provider_order` for an exact OpenRouter model
when backend latency or function support differs. Both parent and worker manifests
own their preferences; the worker does not inherit the parent's model routing.
Bounded attempts honor provider order after capability and cost filtering, pin
one endpoint and retain normal spending admission. This configuration does not
relax child deadlines or source attestation. See the model configuration guidance
in [Agent Builder](AGENT_BUILDER.md).

Optional planning and verification calls use the same owning agent's provider
preferences before budget quotation. Those calls keep their configured model
fallback chain and cannot silently use another provider for a pinned model.
Concurrent agents retain separate routing scopes. A qualification timeout stays
pending with its cost-reconciliation diagnostic; it is not reported as malformed
assessment output. Uncertain spending remains reserved until reconciled.

Native scouts and research children keep the trusted output schema in their task
and validate every final response in code. While tools remain available, they
defer provider final-answer formatting: some backends otherwise stop collecting
evidence after the first search or page read. This opt-in scope does not change
ordinary agents or the evidence-only qualifier. When a scoped final call has no
tools and its schema is ready, it still requests strict `json_schema`; those
requests require endpoint JSON/structured-output support. Genus always parses the
final dossier and attests every citation against that child's successful
retrievals. Invalid JSON, missing evidence and invented citations fail normally.
If final output fails schema validation, the bounded correction requests strict
provider formatting. A citation/source failure reopens tool collection instead,
so the agent can read missing evidence. Neither transition adds repair attempts,
time or spending allowance; malformed final output cannot complete the stage.

New captures use `captured_passages_v2`: each paragraph has its own reference,
with long paragraphs split into continuous slices of at most 800 characters.
This avoids including a neighboring review when citing a business heading.
Source proofs store the passage version and bind it to new source references;
older unversioned captures keep their original boundaries and references during
recovery. Paragraph separation is not a semantic privacy filter: private agent
instructions must exclude personal reviews and mixed passages, and source-quality
review remains necessary before accepting dossiers.

Research workers validate their proposed final output before ending the native
run. Invalid schemas, invented passage references, unexplained empty dossiers and
non-boolean scored evidence produce bounded correction feedback: at most two
additional ordinary iterations, subject to the existing time, iteration and
spending ceilings. Exhaustion fails the child. Completion after a budget or
finalizer exit rechecks the same validator. Only locally accepted output can
become a durable fragment; recovery reapplies source and criterion checks.
Research fleets should keep passage-bearing tool output in context; disable
tool offloading and eager compression for workers that cannot read offloaded
files. Provider context limits and the existing source/run limits still apply.
An evidence-free result must explain its unknowns and cannot establish fit.
Provider JSON conformance and successful correction do not establish semantic
research quality; human qualification calibration remains required.

When the research broker finishes, the owning parent ends from the deterministic
merged result or explicit bundle failure. It does not buy another model call to
restate the broker result. Native run history marks successful final content as
workflow-authored, and retains all child IDs and provenance. A broker rejection
before any bundle starts can still receive normal agent correction. Failed or
cancelled children never become a successful bundle; partial validated fragments
remain available for an authorized recovery attempt.

Mandatory first retrieval supports both named-function endpoints and endpoints
that require a tool call when offered only that one retrieval function. The latter
uses the same authorized schema, native dispatch and successful-fetch attestation;
it does not admit an automatic-tool-choice fallback. This permits choosing a
research model independently of a backend's named-function syntax support.
