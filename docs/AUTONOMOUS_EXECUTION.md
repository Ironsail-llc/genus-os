# Personal autonomous execution

Open **Account → Personal automation** (`/account/autonomy`) to enroll personal
information and grant standing authority. An authorized agent can create an
account, submit an application, log in, or submit a purchase without asking for
another approval when that action fits the grant. Personal resources extend the
existing native vault; no external password manager is required.

## Setup

1. Apply packaged migration `127_autonomous_execution.sql` through the normal
   upgrade process. Preserve the existing vault master key and encrypted backups.
   Install the `genusos[autonomy]` extra and either a system Chromium or the
   browser installed by `python -m playwright install chromium`.
2. Link the signed-in dashboard account and messaging identity to the same CRM
   person. Enrollment refuses an ambiguous or unlinked identity. Resources and
   operations are scoped to both tenant and person, including for administrators.
3. Enroll the profile, reusable application answers, website logins, photos or
   documents, and website authenticator keys needed for the task. Values travel
   inward through the authenticated dashboard; responses contain references.
   Application answers use a short name, such as `membership_reason`, so the
   agent can request `answers.membership_reason` without reading its value.
4. Grant named agents access to exact HTTPS website origins, an expiration,
   and spending limits. Embedded payment providers require separately listed
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
Gmail authentication result. Other mailboxes, delegated sender domains, SMS and
push approvals need adapters; no account authority implies mailbox authority.
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
| `prepare` | Reserve a proposal under `grant_id`; returns a durable operation ID. |
| `inspect` | Read field labels and attributes, never field values, from an operation's website. |
| `generate_credential` | Create an origin-bound username/password reference using an enrolled profile. |
| `email_verification` | Obtain a short-lived code/link reference from the authorized owner's Gmail. |
| `execute` | Fill resource references, upload documents, check required boxes and submit. |
| `operation` | Read the durable state and confirmation evidence. |
| `reconcile` | Check a receipt-specific confirmation without filling or clicking. |
| `cancel` | Cancel a reserved operation or one waiting before submission. |

A proposal names `origin`, `action` (`account`, `login`, `application`, `purchase`,
`subscription`), `purpose`, `idempotency_key`, `amount_minor`, `currency`,
`recurring_minor` and `annual_commitment_minor`. Money is integer minor units.
A subscription requires its annual commitment. Per-purchase, monthly total,
per-recurring-charge and per-membership annual caps are distinct. Monthly
accounting includes committed purchases and unresolved reservations across the
person's grants; it is not an issuer-side card limit.

An execution plan identifies the URL, field selectors with resource IDs and
field names, checkboxes, submit selector, and a new, specific success marker.
Payment plans also identify visible current, recurring and annual totals as
applicable. Even a zero-charge checkout must show a matching zero total.
The broker validates origin and totals again immediately before clicking.
Multi-step applications use a separate operation for each meaningful step,
with the saved account session carried forward. File upload accepts an enrolled
document reference; ordinary nonsecret workspace uploads also work through
`browser(action="act", request={kind:"upload", selector, path})`.

The journal is authoritative:

- `reserved`: no protected fill has begun. A bad plan can be cancelled and
  prepared again with a new idempotency key.
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
