# Personal autonomous execution

Open **Account → Personal automation** (`/account/autonomy`) to enroll personal
information and grant standing authority. An authorized agent can create an
account, submit an application, log in, or submit a purchase without asking for
another approval when that action fits the grant. Personal resources extend the
existing native vault; no external password manager is required.

## Setup

1. Apply packaged migrations `127_autonomous_execution.sql`,
   `128_autonomy_resource_descriptors.sql`, and `129_autonomy_workflows.sql` through the normal
   upgrade process. Preserve the existing vault master key and encrypted backups.
   Install the `genusos[autonomy]` extra and either a system Chromium or the
   browser installed by `python -m playwright install chromium`.
   For persistent workflows install the `genusos[api,autonomy]` extras, render/install
   the platform units with `scripts/install-units.sh`, and enable
   `robothor-autonomy.service`. It runs independently of the engine and bridge;
   restarting either controller leaves browser pages alive. The default private
   socket is `/run/robothor-autonomy/broker.sock` (override
   `ROBOTHOR_AUTONOMY_SOCKET` consistently in the service and both controllers).
   Readiness: `curl --unix-socket /run/robothor-autonomy/broker.sock http://autonomy/ready`.
2. Link the signed-in dashboard account and messaging identity to the same CRM
   person. Enrollment refuses an ambiguous or unlinked identity. Resources and
   operations are scoped to both tenant and person, including for administrators.
3. Use **Use my saved contact details** to import the linked CRM person’s
   existing name, email, phone, occupation and city into an encrypted profile.
   This reads only the signed-in person’s record and never returns its values.
   Enroll any remaining profile fields, reusable application answers, website
   logins, photos or documents, and website authenticator keys. Values travel
   inward through the authenticated dashboard; responses contain references.
   Application answers use a short name, such as `membership_reason`, so the
   agent can request `answers.membership_reason` without reading its value.
4. Grant named agents access to exact HTTPS website origins or explicitly
   select **Allow any public HTTPS website**, with an expiration and spending
   limits. The general website option retains agent, action, currency and budget
   checks; saved credentials remain bound to their own origins. Embedded payment
   providers on another origin still require separately listed
   frame origins. The UI currently uses USD; the broker also recognizes strict
   decimal EUR and GBP totals. Zero spending limits still permit account and
   application tasks.
5. Enable execution. A valid grant satisfies approval for covered actions;
   agents cannot create grants, enable payments, or raise their own limits.

Payment execution is off by default. An owner/admin must record the deployment's
payment-data assessment reference and enable `payment_processing` through
`PUT /api/autonomy/settings`. This is a deployment setting, separate from the
person's spending grant. The settings object contains `enabled`,
`managed_browser`, `payment_processing`, and `payment_assessment_reference`.
This implementation is not a compliance attestation. It processes encrypted
personal card data and therefore needs a review of the deployed data flow,
access, backups, browser vendor, and logging before real-card use.

## Browser and verification

The broker runs in a short-lived, non-dumpable subprocess with a scrubbed
environment. It owns a fresh browser context that the ordinary browser tool
cannot inspect. It launches installed `chromium` when available, otherwise
Playwright's bundled Chromium, always with Chromium sandboxing enabled. An
operator can select an executable with
`ROBOTHOR_AUTONOMY_CHROMIUM_EXECUTABLE`. A host that blocks the downloaded
binary's user namespace may support the distribution's Chromium policy.

Validate Chromium under the actual service restrictions, not only in a login
shell. A Snap launcher can require capabilities that `NoNewPrivileges=yes`
correctly denies. One supported deployment option is a root-owned copy of the
matching Playwright Chromium headless shell, selected with the executable
override in the engine, bridge and workflow service. On Ubuntu hosts that restrict
unprivileged user namespaces, give that exact executable a dedicated AppArmor
profile with `userns,`, following
[Ubuntu's per-application namespace policy](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/).
Keep Chromium sandboxing and the service restrictions enabled. Update that
browser alongside Playwright, and verify a real protected open/inspect after
deployment; an HTTP readiness response alone does not prove browser launch.

Optional Browserbase fallback uses the tenant's native-vault credential
`providers/browserbase/api_key`. Enable managed browsing only after configuring
that provider. Sessions request `recordSession=false`, `logSession=false`, and
`solveCaptchas=true`. Local failures can fall back automatically only while the
operation is still reserved, before credential entry or submission. This
integration does not guarantee that every CAPTCHA can be solved. Bank approval,
biometrics, hardware keys, and unsupported challenges remain external actions.
Managed browsing has mocked contract coverage, not live-provider validation.

Existing owner Gmail access can provide a fresh six-digit code or same-origin
verification link without passing it to the model. The agent must already have
both Gmail search and read tools, and the connector must belong to the same
primary-tenant owner. Extraction checks recipient, time, sender domain and the
Gmail authentication result. The owner can configure `verification_senders` in a
standing grant or use **Additional verification senders** on the Personal
automation page: each HTTPS website maps to exact additional mail sender domains.
These domains are bound to that destination, do not include their subdomains,
and still require authenticated mail addressed to the enrolled recipient.
Agent arguments and page content cannot add trusted senders. Additional senders
do not authorize off-origin verification links. Authority is checked again after
mailbox I/O; revoked grants cannot enroll the retrieved factor. Existing grants
retain same-domain behavior. The operation cutoff floors fractional seconds so
an immediate verification message is not excluded by timestamp rounding.
Other mailboxes, SMS and push approvals need adapters; no account authority
implies mailbox authority.
TOTP generation uses an origin-bound enrolled authenticator resource.

For a card verification code or other supported transient numeric challenge,
the operation pauses before credential entry. The dashboard accepts the code
and resumes the stored execution plan. Codes are never sent through chat or
written into the operation journal. The broker does not save browser storage
after a payment or transient-code entry: merchant scripts may have copied a
code into cookies or local storage. Account/login sessions can be saved as
encrypted references and restored in a later browser process.

## Agent workflow

Use the existing `browser` tool with `action="autonomy"` and `request`:

| `request.kind` | Purpose |
| --- | --- |
| `status` | Discover setup state, resource references, grants and recent operations. |
| `procedures` | Find recent successful plan templates by `origin` and `action`, scoped to the current owner and agent. |
| `prepare` | Reserve a proposal under `grant_id`; returns a durable operation ID. |
| `workflow_open` | Open one protected persistent page for `{operation_id,url,session_resource_id?}`; returns workflow ID, revision and inspection. |
| `workflow_inspect` | Inspect the same page using `{workflow_id}` without reloading it. |
| `workflow_execute` | Execute `{workflow_id,command_id,revision,plan,advance?}` on that page. Reuse the exact command ID and payload after a transport failure. |
| `workflow_status` | Read durable workflow and operation state with `{workflow_id}`. |
| `workflow_reconcile` | Match a newly observed confirmation on an uncertain retained page with `{workflow_id,command_id,revision,selector,text}`; no navigation, input or submit. |
| `workflow_close` | Close `{workflow_id}`; unfinished effects retain their budget reservation and require reconciliation. |
| `inspect` | Discover field selectors, labels, option labels, billing terms and authorized frame fields, never input values. |
| `generate_credential` | Create an origin-bound username/password reference using an enrolled profile. |
| `email_verification` | Obtain a short-lived code/link reference from the authorized owner's Gmail. |
| `execute` | Fill resource references, upload documents, check required boxes and submit. |
| `operation` | Read the durable state and confirmation evidence. |
| `reconcile` | Check a receipt-specific confirmation without filling or clicking. |
| `cancel` | Cancel a reserved operation or one waiting before submission. |

A proposal names `origin`, `action` (`account`, `login`, `application`, `purchase`,
`subscription`), `purpose`, `idempotency_key`, `amount_minor`, `currency`,
`recurring_minor`, `annual_commitment_minor` and optional `recurrence`. Money is integer minor units.
Recurring charges require `recurrence` with `interval_months` (1, 2, 3, 6 or 12),
`next_charge_on` (ISO date), and optional `ends_on`. New first renewals must be
within a year. Month-end billing keeps its original day, clamped to shorter months.
A subscription requires its annual commitment. Per-purchase, monthly total,
per-recurring-charge and per-membership annual caps are distinct. Monthly
accounting includes committed purchases and unresolved reservations across the
person's grants plus recorded renewals due in that UTC calendar month. Future
commitments are projected across 25 calendar months, covering a new subscription's
first full billing cycle; free trials cannot overbook a later month. Completed
memberships remain commitments after the initial purchase rolls out of the current
month. Missing legacy renewal dates block new spending until resolved. Status
returns projections separately from actual settlement; this is not an issuer-side
card limit.

An execution plan identifies the URL, field selectors with resource IDs and
field names, checkboxes and submit selector. Supply both `success_selector` and
`success_text` for a known confirmation, or omit both to discover a new affirmative
English completion message for the requested action. Examples include “Your account
has been created” and “Application submitted successfully”; welcome pages, pending
states and failure messages are insufficient. Discovery checks the main document
and supports a bounded set of completion phrases, not arbitrary language or page
layouts. Unsupported or ambiguous results remain reconciling. Verification links
and reconciliation still require a specific confirmation selector and text.

An already-visible confirmation prevents submission. If a confirmation appears
during filling, the broker stops before clicking and preserves the operation for
reconciliation. Discovered confirmations return a fixed rule name and a text hash;
page text is not returned to the model. Confirmation means the merchant's observed
message, not settlement or admission.

Persistent workflows use a separate non-dumpable broker service with a private
0700 runtime directory, 0600 socket, exclusive process lease, and signed
owner/tenant/agent-bound service tokens. Browser processes receive an environment
allowlist without service credentials or tracing flags. The RPC has no arbitrary
JavaScript, screenshot, HTML or download operation. Entered values and restored
cookie/storage values are masked from inspection metadata before results are
returned or journaled, including common URL/HTML/base64 representations. Once
protected values are present, inspection returns structural CSS selectors, so
secret-bearing element IDs are not exposed or turned into unusable masked
selectors. Masking happens before label shortening. Confirmation digests from
explicit selectors normally hash masked text; after transient-code entry they
hash only the previously declared matched phrase, so even a transformed code
cannot be retained in an arbitrary confirmation-text digest. Browser storage
is not saved after transient code or TOTP entry. This does not constitute verification
against every possible site-specific encoding; adversarial leakage testing
remains part of deployment validation.

Each workflow owns one page and one immutable proposal. `advance=true` permits
zero-money account/login/application steps only: at least one previous field must
disappear and a new field appear, or the broker must observe a final confirmation.
A confirmed intermediate step clears the old plan and increments the revision.
Invalid native constraints can be corrected in place. For zero-money account,
login and application forms, a `server_validation_required` result permits
correcting bindings and retrying at its new revision. Recovery requires exactly
one POST to the submitted form's declared same-origin action, an HTTP 422
response, and a newly invalid visible bound field with an associated visible
error inside that form. It supports AJAX and full-document submissions. Only
fixed error categories and existing selectors are returned; error text, response
bodies and entered values stay private. Wrong endpoints, multiple requests,
stale/hidden errors, unknown outcomes, payments and transient-code challenges
cannot authorize a retry through this path. Commands are journaled before
execution; duplicates return the original result and changed payloads are refused.
Secure code entry resumes the same page without retaining the code in the journal.
Up to 16 contexts are retained, for 15 idle minutes and at most one hour total.
Completion, expiry and shutdown close the browser. Uncertain submissions retain
their page with network requests blocked for read-only reconciliation. Restarting
the browser service loses page state and requires reconciliation; a controller
restart does not. Closing a workflow does not assert cancellation or release money.

Before updating the broker service, send its main process `SIGUSR1` to stop
admitting new workflows. Existing pages remain inspectable and executable.
The private `/ready` response reports `accepting`, `active_workflows` and
`opening_workflow`; restart only when admission is off and both counts/activity
are zero. `SIGUSR2` resumes admission if the rollout is deferred. These controls
are process-management signals, not agent tools. Engine and bridge updates
need not restart this service.

Persistent workflows currently use local Chromium. Managed-browser CAPTCHA
sessions, authentication redirects, verification-link navigation, cumulative-only
wizard transitions, field rejection without the explicit request/error evidence above,
and multi-operation checkout workflows remain separate work.
The one-shot browser path remains available for its supported tasks.

Successful execution plans persist in the operation journal. `procedures` returns
up to five distinct templates from confirmed operations in the last 90 days.
One-time verification links and expiring or revoked resource bindings are excluded;
saved session references, URL query strings and fragments are removed. Old
receipt-specific success text is replaced with discovery rather than asserted
about a new submission. Templates retain active personal-resource references, not
their values. Inspect the current page, choose the correct resources and fresh
session for the new task, then prepare a new proposal with a new idempotency key
and current grant. Existing budgets, price checks, validation and revocation checks
still apply. A template does not resume or repeat its source operation.
Payment plans also identify visible current, recurring and annual totals as
applicable. Recurring plans also require `recurrence_interval_selector` and
`next_charge_selector`, plus `recurrence_end_selector` when an end date is declared.
The broker compares those visible terms to the proposal before filling and again
before submitting. Date selectors must identify an ISO date or an unambiguous
English month-name date; interval selectors identify monthly, quarterly, yearly
or an explicit “every N months” label. Even a zero-charge checkout must show a
matching zero total.
For terms inside a direct child frame, set `terms_frame_selector` and
`terms_frame_origin`; all price and renewal selectors then use that frame.
Inspection returns these bindings with each frame field or term. Same-origin
frames inherit website authority; foreign frames require a separately granted
origin. Credentials must match the actual frame origin. Nested and originless
frames are reported as unsupported. Protected frames cannot navigate to another
origin during filling or submission. Inspection omits page HTML and general body
text; returned terms are restricted to recognized prices, intervals and dates.
The broker validates origin and totals again immediately before clicking.
Before entering protected values, the broker checks native form constraints in
a separate offline browser context. Required fields, email formats, patterns,
lengths, numeric bounds and unfillable controls return `validation_required`
with field selectors and fixed constraint flags; values and browser error messages
are omitted. The operation remains reserved and its plan is not bound yet, so the
agent can correct field bindings or required checkboxes and execute the same
operation. These checks do not run merchant scripts and do not replace server-side
validation. Once actual protected filling starts, failures still require
reconciliation rather than a blind retry.

Multi-step applications use a separate operation for each meaningful step,
with the saved account session carried forward. This currently works only when
the website persists progress outside the page: each broker call closes its
browser, and cookie/local-storage restoration does not preserve client-only
wizard state. Persistent workflow sessions remain an implementation gap.
File upload accepts an enrolled
document reference; ordinary nonsecret workspace uploads also work through
`browser(action="act", request={kind:"upload", selector, path})`.

The journal is authoritative:

- `reserved`: no protected fill has begun. Native validation failures can be
  corrected within the same operation before its plan is bound. Other bad plans
  can be cancelled and prepared again with a new idempotency key.
- `awaiting_input`: the bound plan is waiting for a code before submission.
- `submitting`: atomically claimed before the first protected fill; scripts
  can initiate requests during input, so even a later failure is uncertain.
- `reconciling`: an external outcome is unknown. Do not repeat the submission.
- `completed`: the expected merchant confirmation was observed and hashed.
- `failed` / `cancelled`: terminal outcomes; only valid journal transitions apply.

Identical preparation requests return the same operation. A different proposal
with the same idempotency key is refused. Concurrent reservations cannot exceed
the monthly cap. Grant revocation, expiry and execution/payment settings are
rechecked before submission. Disabling execution does not prevent read-only
reconciliation of a pending result.

**Completion means observed website confirmation, not bank settlement or club
admission.** Reconciliation needs a receipt-specific URL/selector/text tied to
the operation; a generic welcome page is insufficient evidence. No issuer
webhook, refund/dispute automation, settlement feed or virtual-card issuance is
provided by this browser adapter. Recurring commitments are recorded and capped
at enrollment; future merchant-initiated renewals are not intercepted by it.

## Storage and operation

`vault_resources` holds versioned AES-GCM envelopes authenticated to the tenant,
person, record and key version. These resources are excluded from generic
service-secret exports. `autonomy_grants`, `autonomy_operations`,
`autonomy_events` and `autonomy_settings` provide authority, reservations,
state transitions and owner-scoped status. Tenant RLS matches the platform's
backstop; the DAL also requires tenant and person on every lookup.

Resource descriptors expose available field names, enrollment source and timestamp
without decrypting values during ordinary status reads. The dashboard shows saved
fields and provenance. For preexisting resources, the owner can choose **Check saved
information** (`POST /api/autonomy/resources/refresh-descriptions`, empty JSON body)
to derive metadata inside the vault boundary. This retains the original enrollment
timestamp and marks unknown historical provenance explicitly; it does not invent
missing personal information.

`genus vault rotate-resources` reencrypts personal resources transactionally
under a new resource key wrapped by the existing vault master key. Old versions
remain readable for in-flight workers. This does not rotate the master key.
Back up the database and master key together; deleting old key versions can
break recovery. Event records contain event names and references, never values;
this is an application append-only journal, not an external tamper-proof ledger.

The feature does not expose screenshots, page HTML, arbitrary JavaScript,
console logs or recordings from credential-bearing browser contexts. Recognized
card numbers and labeled verification codes pasted into text are redacted
before the normal runner/Telegram transcript path. This is a backstop, not a
way to enroll payment data. Arbitrary card photos or attachments are not
classified automatically; use the secure enrollment page.

The broker blocks nonpublic request destinations, including redirects, but
application DNS checks do not close DNS rebinding. Deployed brokers also need
network egress enforcement. Agent shell execution must remain in its configured
sandbox; a privileged host process can bypass an application-level vault.
Disable execution or revoke a grant to stop new submissions; preserve uncertain
operations and reconcile them rather than deleting their reservations.

## Validation

Use a disposable PostgreSQL database whose name ends in `_test`:

```bash
export AUTONOMY_TEST_DSN='host=127.0.0.1 port=5432 user=test dbname=autonomy_test'
export ROBOTHOR_WORKSPACE="$PWD"
pytest robothor/autonomy/tests/ robothor/engine/tests/test_browser_resources.py
```

The suite covers real database transactions, concurrent budgets, owner/origin
isolation, key rotation, safe HTTP validation, code extraction, redirects,
revocation, process errors, and controlled Chromium account/checkout flows.
The subprocess acceptance test requires a sandbox-capable Chromium executable.
Managed browser tests mock the provider. No live account creation, real payment,
production migration or service deployment is performed by this test suite.

Host browser paths are declared under `settings.autonomy` in the normal settings
registry. `chromium_executable` and `socket` accept the corresponding documented
environment overrides and appear in the generated configuration reference.
Browser tests carry the `e2e` marker where they do not require the database fixture;
the required `test-autonomy` CI lane installs Chromium and runs the entire autonomy
suite, including these tests. Generic Python matrix jobs do not install browsers.


Engine-created operations retain a `request_context` with the authenticated actor
identifier and originating run UUID. It is supplied from tool execution context,
never from page content or model arguments. Operation lookup exposes this reference
alongside the grant version and proposal, so an audit can follow it to the recorded
request. Exact idempotent retries from another run preserve the original context.
Older or direct administrative reservations remain explicitly unattributed; a retry
does not invent a historical request. Apply migration 130 before using this version.
This is attribution, not additional spending authority or a digital signature.

## Private input enrollment

Apply migration 131 for expiring enrollment intents. The existing browser tool
accepts `action=autonomy`, `request.kind=enrollment_link`, and an `enrollment`
object containing `kind` and optional HTTPS `origin`. Website logins and
authenticator keys require an origin. It returns a 15-minute link to Account →
Personal automation; set `autonomy.dashboard_origin` to the dashboard's public
HTTPS origin to make links clickable outside the dashboard.

Links require a normally authenticated personal account linked to the same CRM
person as the requesting channel identity. The token is in the URL fragment,
removed on page load, and submitted only in a private request body. The database
stores its hash. The link fixes the resource type and destination; expiration,
foreign owners and foreign tenants cannot enroll through it. Completion writes
the encrypted resource and its receipt in one transaction. Concurrent submissions
and retries return the original reference without replacing its value. Revoked
resources cannot be recovered through an old enrollment receipt.

In a linked private Telegram chat, `/secure profile` or `/secure document` requests
a link; `/secure credential https://example.com` requests a website-specific link.
For an explicitly supplied structured input, append a JSON object: a credential
uses `username` and `password`, a profile uses the documented profile fields, and
`/secure totp https://example.com` accepts an object containing `secret`. Such
inputs are intercepted before message logging, pending questions, history and
model processing. Only a resource reference is queued for the assistant. Storage,
validation or identity errors consume the marked message and return a generic
error; they never fall back to normal chat. Cards must use the secure page.

A single file with caption `/secure document` is downloaded into bounded memory
and encrypted directly, up to 5 MB. Its contents and original filename do not
enter the normal attachment inbox, OCR, vision or agent arguments. Albums are
held for the normal collection window before any download; a private caption
refuses that entire batch. Send private documents individually: an item arriving
after the collection window is a separate batch, and Telegram itself retains the
original upload. Ordinary document-analysis requests retain their normal flow.

The shared redaction boundary also withholds explicit `/secure` text if it reaches
history or the runner through another path. This is a backstop, not secure capture
for other chat surfaces, nor automatic detection of arbitrary unlabeled secrets.
The dashboard enrollment form includes legal name, second address line and
nationality as well as the existing profile fields; missing values are not guessed.

Enrollment deployments must also apply migration 132 through the canonical migrator.
It adds the established tenant-isolation database backstop to enrollment intents
and any other tenant table missing it, without changing existing policies or
the checksum of an already-applied migration 131. Scoped database reads and
writes are covered by a non-superuser PostgreSQL regression test.

## Purposes and shared spending decisions

Standing grants can set `allowed_purposes` on Account → Personal automation.
Each line is an allowed proposal purpose; matching ignores leading/trailing
whitespace and letter case but does not use substring or semantic matching.
An empty list retains broad authority for any task otherwise covered by the
grant, including existing grants created before this field was added. Select a
matching granted purpose when preparing a task. The originating request remains
linked through `request_context`; a purpose label is not proof that arbitrary
page content describes the user's intent.

The operation's purpose is part of its immutable proposal and idempotency
fingerprint. The broker checks it on reservation and again before execution and
submission, alongside grant revocation, version, destination and amounts. A page
cannot edit the grant or replace that purpose. A covered task proceeds without an
additional approval; an unmatched purpose returns `purpose_not_allowed`.

Personal and organizational spending now call the same exact-amount limit
functions in `robothor.entity.spend_limits`. Personal amounts remain integer minor
units; treasury amounts remain Decimal values. Both paths enforce per-transaction
and monthly boundaries and reject unavailable or invalid usage. Organizational
daily limits and approval thresholds remain part of treasury policy; personal
standing grants do not acquire a new approval threshold. Future personal renewal
projections use the same bounded arithmetic. Ownership checks, resource access,
reservations and ledger records remain scoped to their respective domains, so
personal funds are never represented as company-owned virtual cards.
e

## Private submission observations

The exclusive broker records visible page text before filling an operation and
again before its submit click. These immutable snapshots are encrypted separately
from model-readable resources, bound to tenant, owner, operation and record ID.
Each version references the operation's grant version; the operation retains its
originating request, purpose, amounts and outcome. They are authorization audit
observations, not digital signatures or independent evidence of acceptance,
settlement, or complete review of a contract.

On Account → Personal automation, open **Submission record** under a recent task
and choose a snapshot. Only the authenticated linked owner can retrieve the
private text; agent service tokens cannot. Listing metadata and ordinary agent
responses contain no snapshot text or link values. The page renders text without
executing markup, clears it when closed, and uses uncached authenticated requests.

Rendered-page coverage is `visible_text_only`: the top-level page and authorized
direct child frames. Hidden text, nested or unauthorized frames, images and PDFs
are outside that coverage. Bounded text/link extraction reports truncation and
omitted frames. Unselected link references remain encrypted and their contents
are labeled as uncaptured.

Plans may select up to five linked material documents with `material_terms`, each
containing a `selector` and optional `frame_selector`/`frame_origin`. Inspection
returns candidate `terms_links` as labels and selectors, without exposing private
URLs. For each selected link, the broker creates a fresh browser context with no
applicant cookies or storage, scripts disabled, and only document GET requests
allowed. The selected origin and every redirect must be covered by the standing
grant (including broad website authority where granted); private network checks
still apply. Public HTML and plain text are supported, up to 200,000 characters
per document and 30 seconds total per capture phase. Selected content is never
silently shortened.

Successful captures use `visible_text_and_selected_documents` and preserve both
the requested and final document URLs inside encryption. The viewer identifies
these documents and does not mislabel their links as uncaptured. This is a record
of selected content, not a claim that every relevant contract has been discovered
or understood. Login-required documents, PDFs, missing/oversized responses and
known code/credential-bearing URLs return `material_terms_unavailable` before
filling. Correct a failed selection on the same operation; no execution plan is
bound until the initial capture succeeds. Existing plans without selections keep
their original serialized shape and retry fingerprints.

Known protected values are masked before storage. No page snapshot is taken after
any transient verification code or TOTP entry in that browser, including a code
reflected with an arbitrary encoding. The earlier pre-input observation remains
available; do not describe it as a later pre-submit snapshot. Failed or rejected
attempts retain their own observations, and a snapshot alone never changes an
operation's state or authorizes another submission.

Apply migration 133 through the canonical migrator before deploying the broker
and bridge changes. The new table applies tenant row-level security inline and
uses the native versioned encryption keyring; retained historical keys can still
read existing snapshots. Ordinary credential exports do not include this table.

Selected material-document support requires migration 134 after migration 133.
It extends metadata constraints without rewriting existing encrypted records.
If an earlier workflow step has already entered a transient code, new material
selections cannot be collected from that page; use a supported fresh source.
The normal post-code submit phase retains its earlier pre-input observations.

### Retained submission recovery

When a persistent submission is uncertain, `workflow_inspect` returns bounded,
private-masked candidate messages from its frozen page. These are untrusted
merchant observations, not automatically verified success. The agent must identify
an affirmative confirmation for the requested operation, then pass that exact
selector and text to `workflow_reconcile`. The broker matches the current
observation and records its masked-text hash; it does not infer bank settlement.
Unknown or negative messages must remain uncertain, not be selected as success.

The broker captures text digests immediately before the click, excludes unchanged
messages and hidden/editable/control content, uses structural selectors, masks
known form/session values, and applies existing audit redaction. Reconciliation
cannot navigate, fill, upload, click, or change authority. Network is blocked on
the retained context. A mistaken execute retry returns the pending state without
another submission. Command replay, owner/agent binding and revision checks still
apply. Revoked grants or disabled execution do not prevent read-only completion
of an already pending operation.

The observation inventory is bounded to 3,000 candidate elements and 80 messages
of at most 300 characters. Oversized/incomplete inventories offer no candidate
rather than treating an omitted old message as new. Transient-code/TOTP entry
also suppresses recovery text. Missing evidence, unsupported protected challenges,
expiry or service loss still require external/status-page reconciliation; this
feature does not make uncertain effects safe to repeat. The existing 15-minute
idle and one-hour absolute browser limits still apply.

Private workflow errors expose only fixed retry reasons, never arbitrary browser
or database exception text. `command_changed` means an ID was reused for different
arguments: a new logical command (such as reconciliation) needs its own ID, while
an identical retry keeps the original ID. On `workflow_revision_changed`, read
`workflow_status` and use its revision. `command_in_progress` requires waiting;
`workflow_lost` requires external/status-page reconciliation, not a blind repeat.

Malformed workflow commands return `invalid_workflow_request` before token issuance or browser RPC. Fixed `invalid_operation_id`, `invalid_workflow_id` and `invalid_command_id` reasons identify references that must come from prior results (or a fresh UUID for a new command). `confirmation_selector_and_text_required_together` requires both confirmation fields or neither. Other validation failures remain generic; submitted values and unknown field names are never echoed.


### Payment lifecycle integration in progress

The internal `payment_lifecycle` projection distinguishes a merchant submission
confirmation from issuer authorization, charge, refund and authorization reversal.
Repeated identical events are idempotent; a reused event key with changed content
is rejected. Gross charged and refunded amounts remain separate, and verified
amounts above the reservation or authorization are recorded with discrepancy flags.
They are not hidden by execution-policy limits. Refunds do not by themselves free
a spending reservation or end a recurring commitment.

Migration 138 adds encrypted, owner-scoped payment facts. Broker payment completion
records a submission fact atomically for `purchase` and initial `subscription` enrollment; concurrent identical deliveries are deduplicated,
and conflicting deliveries cannot overwrite evidence. A fresh process can read the
position using the native resource keyring. Unsupported or inconsistent facts remain
stored with a reconciliation-required result rather than an invented balance.
Delivery order does not determine financial dependency order: known authorizations
are evaluated before reversals, and known captures before refunds. A refund or
reversal that arrives first stays unresolved until its prerequisite evidence
arrives, then the read projection can recover without modifying earlier journal
entries or releasing budget. Each initial or renewal payment is projected
separately, as described below; corrections remain unfinished. Conflicting
captures/reversals, excess refunds and multiple authorizations still require
reconciliation.

The journal is not yet an issuer integration. Its provenance field is descriptive, not authentication: only trusted adapters may
supply facts after validating their evidence. Agent claims and unauthenticated
callbacks must never become issuer facts. The Personal automation operation list includes a private Payment status view.
The owner-only GET payment endpoint and agent-bound `payment_status` command
return balances and discrepancy flags without issuer references. Neither accepts
payment writes. A submission is explicitly labeled as not yet a verified charge.

Rendered-page receipt observations are described below. Validated issuer evidence
ingestion and budget reconciliation remain integration work before this capability
can be considered complete.


### Private receipt observations

Migration 139 extends the existing encrypted observation archive with an
`after_confirmation` phase. Receipt observations are limited to completed
`purchase` or `subscription` operations, the assigned agent, the original origin,
and the exact confirmation digest already recorded on the operation. Revocation
does not discard evidence for an already completed payment. These observations
remain merchant page evidence, not proof of issuer settlement.

An encrypted `capture_status` distinguishes captured content from content withheld
after verification-code entry or unavailable content. Non-captured records cannot
contain page text or links. The broker captures bounded rendered main-page text
only after durable confirmation, including read-only reconciliation. It checks
the original origin and confirmation witness before and after extraction, masks
known private values, and returns only archive metadata to the agent. Capture or
storage failure leaves the completed operation intact and never retries submission.
After transient-code entry it does not access the page or browser storage.

The owner can open “Submission and receipts” for a purchase or subscription and
view “Receipt after confirmation” observations. Withheld or unavailable text is
explicitly identified. These are rendered page observations, not complete merchant
PDF receipts or issuer settlement records; embedded pages are not captured. Receipt
retrieval from other sources and issuer reconciliation remain separate work.

### Task prerequisite preview

The protected browser tool accepts `kind=readiness` with a `grant_id`, the same
`proposal` used by `prepare`, and optional `requirements` entries containing a
`resource_id`, `kind`, and required `fields`. It previews execution/payment flags,
current scoped grant and budget, and active destination-compatible resource
metadata. It returns missing field names and actionable blockers without
reserving money, decrypting private values, or contacting a merchant.

`ready_to_prepare` covers only these declared prerequisites. The response lists
resource decryption, actual browser execution, merchant requirements, funding
acceptance and verification challenges as unchecked. Missing requirements must
be declared from the task and subsequent page inspection; an empty requirements
list does not prove that a form needs no private data. A preview is not an
execution authorization or a promise of completion. Preparation and submission
continue to enforce current authority and concurrent spending limits. A matching
already-prepared request returns its existing operation for status/reconciliation;
it does not count the same reservation again or suggest another submission.

### Renewal payment evidence

A trusted issuer adapter can attach `renewal_id` (its stable transaction identity)
and `renewal_on` (the billing-period due date) to an issuer fact. Both fields are
required together and remain inside the encrypted journal. Merchant submissions
cannot supply renewal facts. Existing facts without these fields continue to
belong to the initial purchase or enrollment payment.

Each renewal has independent charge/refund accounting; a refund cannot consume
another renewal's charge. The initial `position` remains separate, and the
owner/agent read result adds `renewals` with opaque ordinal references, due dates,
balances and reconciliation flags. Private issuer transaction IDs are not returned.
The owner Payment status view displays these separate balances and clears them
when closed. A fresh process can recover them from the encrypted journal.

Billing dates are checked against the saved recurrence, including month-end
clamping and an optional end date. Unexpected dates and charges above the saved
recurring allowance are recorded and flagged. Multiple transaction IDs in one
billing period are checked against that period's allowance using gross charges;
refunds do not authorize another purchase. Facts with inconsistent billing dates
for one transaction remain unresolved. Revocation does not prevent recording
money that has already moved, and evidence never releases the existing budget
reservation or changes a membership. This adds no issuer connection or automatic
renewal execution: authenticated ingestion, corrections and verified membership
changes remain integration work.

### Durable external verification handoffs

For an observed SMS/device, push, passkey, biometric, issuer or unsupported website
challenge, `browser` autonomy supports `handoff {operation_id, handoff}`. The
handoff contains a fresh UUID `request_id`, a `kind` (`sms`, `push`, `passkey`,
`biometric`, `issuer` or `captcha`), and a `confirmation` with a same-origin status
`url`, `selector` and task-specific confirmation `text`. An existing origin-bound
browser session can be supplied as `confirmation.session_resource_id`. The
optional `lifetime_seconds` is 60–86400, defaulting to 900. Reuse the same request
ID and content after a transport failure; a different active request cannot
replace a pending handoff. `handoffs {operation_id}` reads its public state.

Migration 140 stores the confirmation plan encrypted and binds it to the owner,
operation and handoff. Public results expose the handoff ID, kind and deadline,
never its private URL or browser session. Starting a handoff rechecks current
execution settings, grant and budget. The operation then remains `reconciling`
because a person may complete a commitment outside the broker; handoff expiry
cannot free money or enable resubmission. A replay cannot extend the deadline.

The Personal automation page displays **External verification** and a **Check
status after verification** button. The person completes the challenge on their
trusted device or merchant/issuer website; this page does not collect their SMS
code or biometric data. The authenticated owner endpoint loads the stored plan
and requests a read-only browser check. Acknowledgment is not proof of completion:
only observed website confirmation resolves the operation and its handoff.
Existing uncertain work can still be checked after revocation or disabling
execution. The encrypted handoff survives process restart; if a background check
is interrupted, the same button can safely request another status check.

Status recovery uses a fresh context with service workers disabled. It permits
GET/HEAD through existing destination network checks and blocks other HTTP
methods and WebSockets, including script-initiated attempts to repeat checkout.
It does not click or fill. Sites requiring a mutating status API need a dedicated
validated adapter; a GET endpoint must still have read-only server semantics.
Missing confirmation, authentication or an expired handoff leaves the task
uncertain. A preexisting broker session is needed for authenticated status pages;
no post-payment browser storage or verification code is retained for this purpose.

Use the existing secure numeric-code path or authorized mailbox/TOTP integration
when available, and configured managed challenge handling where supported.
Handoffs do not add a phone connection, passkey signer, biometric capability or
CAPTCHA solver, and do not replace standing authority with routine final approval.
Automatic recovery scheduling and real issuer/device-provider acceptance remain
part of the broader integration work.
