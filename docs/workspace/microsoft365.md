# Microsoft 365 workspace

> **Status: in development.** The Microsoft 365 mail and calendar providers,
> the Graph transport and its sign-in, the connect command and the doctor
> checks exist: with `ROBOTHOR_WORKSPACE_PROVIDER=microsoft365` and an assistant
> mailbox set, the `gws_gmail_*` tools read and send mail from the assistant's
> Exchange Online mailbox (see [Mail](#mail)) and the `gws_calendar_*` tools run
> against Exchange Online calendars (see [Calendar](#calendar)). New inbox mail
> and calendar changes are published by the platform's
> [ingest worker](#ingestion). The `gws_chat_*` tools stay Google-only: on a
> Microsoft 365 workspace they refuse up front (Teams chat is a later phase).
> Do not pass `--enable` on a production instance until the release notes say
> it is ready. [Connect a tenant](#connect-a-tenant) is the step-by-step runbook.

Genus OS reads and sends mail and manages calendars through the `gws_*` tools.
By default they run against Google Workspace. With
`ROBOTHOR_WORKSPACE_PROVIDER=microsoft365` the same tools run
against Exchange Online (Outlook mail and calendar) through Microsoft Graph.

## Architecture

The tool names stay the same on purpose. The platform's mail and calendar
safety rules (do-not-contact, no automatic scheduling, event dedup,
duplicate-reply and inbound-only checks, verification read-backs, CRM
write-through and benchmark refusal) are keyed on the `gws_*` names, so keeping
the names keeps every rule. Only the transport underneath changes:

| Layer | Google | Microsoft 365 |
|-------|--------|---------------|
| Tools and guards | `gws_*` tools, shared rules | same tools, same rules |
| Provider (`robothor/workspace/protocols.py`) | `GoogleMail`, `GoogleCalendar` (`robothor/workspace/google/adapter.py`) | `GraphMail` (`robothor/workspace/microsoft/mail.py`) and `GraphCalendar` (`robothor/workspace/microsoft/calendar.py`), sharing one Graph client |
| Transport | `gws` CLI, plus conditional Calendar HTTP for edits | `robothor/workspace/microsoft/graph.py` (`GraphClient`) |
| Sign-in | Google OAuth | Entra app-only (`robothor/workspace/microsoft/auth.py`) |

The tool handlers (`robothor/engine/tools/handlers/gws.py`) get their providers
from `robothor.workspace.get_workspace()`, which reads `workspace_provider`.
Every guard runs in the handler before a provider is called. The provider only
moves data:

- **Mail** (`MailProvider`): `search`, `get_message`, `get_thread`, `send`,
  `reply` and `modify`, plus `shape_envelope` and `shape_message`, which turn a
  raw message into the tool result. Sends take the RFC 5322 message the handler
  built, so the recipients the do-not-contact screen approved are exactly the
  ones that get sent. A thread read with `fmt="metadata"` returns Gmail-style
  `payload.headers`, because the shared duplicate-reply and reply-all logic
  reads those headers.
- **Calendar** (`CalendarProvider`): `resolve` (the assistant's own calendar
  or the operator's, as a `CalendarRef`), `list`, `get`, `create`,
  `conditional_patch` (one write, only if the event is still at the version
  that was read), `respond` and `delete`. The read, merge, conditional write
  and read-back for an edit live in `robothor/engine/calendar_attendees.py` and
  call the provider.
- **Shapes** (`robothor/workspace/types.py`) use Google's field names
  (`threadId`, `responseStatus`, `attendeesOmitted`, and so on), so the Google
  adapter passes data through unchanged and a Microsoft adapter translates
  into them.
- **Search** (`robothor/workspace/query.py`) parses Gmail syntax into a
  `MailQuery`. Google still receives the original string. Any operator the
  parser does not understand is kept in `raw`, and a provider that cannot
  express it must refuse the search, never widen it.
- **Threads and the event loop.** The handlers run in a worker thread.
  `robothor.workspace.bridge.blocking()` calls the Google adapter's synchronous
  side directly, and sends an async-only provider's calls (Microsoft 365) to
  the engine's event loop.

`GraphClient` applies Graph's rules in one place:

- **Stable ids and UTC.** Every request sends
  `Prefer: IdType="ImmutableId", outlook.timezone="UTC"`. A message id stays
  valid when the message moves folders, so the id recorded at send time still
  works for the read-back. A caller's own `Prefer` values are merged into that
  one header, and a value with the same name replaces the default.
- **Reads retry, writes don't.** A read that gets 429, 503 or 504 waits for
  `Retry-After`, up to 3 attempts and 60 seconds of waiting in total. A write
  is sent once. If the connection drops after sending, or Graph answers 5xx,
  the result is *unknown effect*: the caller has to read back before trying
  again, so a contact never gets the same email twice.
- **Paging stays on Graph.** `@odata.nextLink` is only followed when it points
  at the same origin as the Graph base URL.
- **Four requests per mailbox at once**, which matches Exchange's per-mailbox
  throttling limit.
- **Errors carry Graph's error code and message, nothing more.** They never
  include a token or a message body. Each request has its own
  `client-request-id` for Microsoft support.

## Mail

`GraphMail` serves the mail tools from the mailbox in
`ROBOTHOR_M365_ASSISTANT_MAILBOX`. Without that setting every Microsoft 365
tool (mail and calendar) refuses as not configured; nothing falls back to
Google. The Graph client is built from the vault of the platform tenant that
made the tool call, and mail and calendar share it.

Every guard in the tool handler runs before Graph is called: do-not-contact,
the duplicate-reply check, benchmark refusal and reply-all assembly. A refused
send makes no Graph write.

### Results look the same as Google's

The provider turns each Graph message into Gmail's raw shape, and the Google
shaper formats it. So a tool result has the same keys whichever provider
served it:

| Tool result field | From Graph |
|-------------------|-----------|
| `id` | the message's immutable id |
| `thread_id` | `conversationId` |
| `from`, `to`, `cc`, `subject`, `message_id` | `from`, `toRecipients`, `ccRecipients`, `subject`, `internetMessageId` |
| `date` | `sentDateTime` as an RFC 5322 date |
| `snippet` | `bodyPreview`, HTML-escaped like Gmail's |
| `body_text` | the body, requested as text (`Prefer: outlook.body-content-type="text"`); an HTML body is converted to text the same way as a Gmail one |
| `attachments` | name, type and size of each attachment (never the bytes) |

### Search syntax

Agents keep writing Gmail queries. Each one is translated to Graph. If a
query can't be translated exactly, the tool refuses it and lists what is
supported. It never drops a term and returns more mail than was asked for.

| Gmail query | Graph |
|-------------|-------|
| `from:addr`, `to:addr` (a full address) | `$filter` on the address, exact |
| `from:name`, `to:name`, `cc:`, `bcc:`, `subject:`, free text, `"a phrase"` | KQL `$search` |
| `is:unread` / `is:read`, `is:starred`, `is:important` | `isRead`, `flag/flagStatus`, `importance` |
| `has:attachment` | `hasAttachments` |
| `label:<name>` | the Outlook category `<name>` |
| `in:inbox`, `in:sent`, `in:trash`, `in:spam`, `in:drafts` | that folder (Inbox, Sent Items, Deleted Items, Junk Email, Drafts) |
| `in:anywhere` | every folder |
| `after:` / `before:` (`YYYY/MM/DD` or epoch seconds, UTC) | `receivedDateTime ge` / `lt` |
| `newer_than:` / `older_than:` (`Nh`, `Nd`, `Nm` = 30 days, `Ny` = 365 days) | `receivedDateTime ge` / `lt` |
| `a OR b`, `{a b}` | an OR group, when every part is the same kind |
| `-is:…`, `-has:attachment` | the opposite value |

These are refused: any other operator (`filename:`, `larger:`, `category:`
and so on), other `is:`/`has:`/`in:` values, other negations (`-from:`,
`-subject:`), more than one `in:`, and a group that mixes text terms with
flag, date or label terms.

How the query runs:

- **Only structured terms** go into a `$filter`, sorted newest first on the
  server. Graph needs the sort property to be filtered first, so a
  `receivedDateTime` condition always leads the filter.
- **Any text term** switches the query to KQL `$search`. Graph doesn't allow
  `$search` together with `$filter` or `$orderby`, so every structured term is
  also checked on each result, and the provider sorts the results itself.
- **Deleted Items and Junk Email are left out** unless the query names a
  folder, the same way Gmail leaves out trash and spam.

### Labels

Graph has no labels, so the provider builds Gmail's label list from the
message:

| Label | Means |
|-------|-------|
| `UNREAD` | `isRead` is false |
| `INBOX`, `SENT`, `TRASH`, `SPAM`, `DRAFT` | the message is in Inbox, Sent Items, Deleted Items, Junk Email or Drafts |
| `STARRED` | the message is flagged |
| `IMPORTANT` | `importance` is high |
| anything else | an Outlook category |

`gws_gmail_modify` maps them back:

| Change | Graph |
|--------|-------|
| add / remove `UNREAD` | `isRead` false / true |
| add / remove `STARRED` | flag `flagged` / `notFlagged` |
| add / remove `IMPORTANT` | importance `high` / `normal` |
| remove `INBOX` | move to Archive |
| add `INBOX`, or remove `TRASH` | move to Inbox |
| add `TRASH` | move to Deleted Items |
| any other label | add or remove that category |

`SENT`, `DRAFT`, `SPAM`, `CHAT` and Gmail's `CATEGORY_*` tabs are refused, as
is a label that is both added and removed. A refused change writes nothing.
Immutable ids survive a move, so the id stays the same after archiving.

### Sending and threading

- **A new message** is a draft created from the MIME message the handler
  built, then sent. The draft's immutable id is returned as the sent message's
  `id`, with `threadId` set to its `conversationId`, so a read-back by that
  id finds the sent message.
- **A reply** (`gws_gmail_reply`, or `gws_gmail_send` with a `thread_id`) uses
  `createReplyAll` on the newest message in the conversation that isn't a
  draft, so Exchange keeps it in the same conversation. The draft's To, Cc and
  Bcc are then replaced with exactly the recipients the handler approved,
  and its body with the handler's body. Exchange's own reply-all list is
  never sent. The subject Exchange gave the reply is kept, because changing it
  can split the conversation. If the draft can't be prepared, it is deleted
  without being sent.
- **The duplicate-reply guard** reads the conversation oldest first. If the
  last message is from the assistant (drafts included), the reply is skipped
  before any write. The guard compares senders with `ROBOTHOR_AI_EMAIL`, so
  set that to the same address as `ROBOTHOR_M365_ASSISTANT_MAILBOX`.
- **Writes are sent once.** A send whose outcome is unknown comes back with
  `outcome_unknown: true` and is not retried.

## Ingestion

On Google, the instance's own sync scripts poll Gmail and publish `email.new`
on the event bus, which starts the email pipeline. On Microsoft 365 the
platform does this itself: with `ROBOTHOR_WORKSPACE_PROVIDER=microsoft365` the
engine runs an ingest worker (`robothor/workspace/ingest/`). On a Google
instance the worker starts no task at all, so nothing is published twice.
With HA leader election on, only the leader replica ingests.

Every `ROBOTHOR_M365_INGEST_INTERVAL_SECONDS` (default 60) one round runs:

- **Assistant inbox** (`GET /users/{assistant}/mailFolders/inbox/messages/delta`,
  envelope fields only, 50 per page). Each new unread message is merged into
  `email-log.json` and published once as `email.new`. The payload and the log
  entry use the same translation as the mail tools, so `from`, `date` and the
  labels match what `gws_gmail_get` shows.
- **Owner calendar** (`GET /users/{owner}/calendarView/delta` over one day
  back to 30 days ahead; the window is fixed when the delta starts and renewed
  daily). The first sync is a baseline and publishes nothing. After that each
  change is published as `calendar.new`, `calendar.modified`,
  `calendar.rescheduled` or `calendar.cancellation`. A delta removal is read
  back: a deleted or cancelled event is a cancellation, an event moved out of
  the window is a reschedule.

The payloads follow the contract in [Event Bus](../event-bus.md#email-and-calendar-event-contract).

**A lost delta never replays old mail.** Exchange can drop a delta
(`410 syncStateNotFound`). The worker then starts a new one, and publishes
from it only messages newer than the high-water mark (the newest message it
had already processed) that it has not seen before. Both live in the database
(migration 149: `workspace_sync_state` and `workspace_seen`). The first sync
ever publishes at most 20 unread messages from the last week.

The email log is written atomically (a temporary file, then a rename, under
the same `.email-log.lock` the Google script uses). Existing entries are kept
as they are, and a reply resets its conversation's first entry for re-triage.
The log is capped at 2,000 entries. `ROBOTHOR_EMAIL_LOG_PATH` overrides where
it goes; by default it is `<workspace>/brain/memory/email-log.json`, the file
the dashboards read.

**Each new email is logged to the CRM once.** For every message it
publishes, the worker records an incoming email interaction with the sender:
the same body the Google script posts to the bridge's `/log-interaction`
(the sender's name and address, `channel=email`, `direction=incoming`, and
`From: 'Subject'` as the summary). It calls the bridge's own core,
`robothor.crm.interactions.log_interaction`, in process: the contact is
resolved by email address (and created if new), and the message is appended to
their newest conversation. A logged message is marked `crm:<message id>` in
`workspace_seen` and its log entry gets `crmLoggedAt`, so a retried round or a
410 resync never logs it twice. The interaction is logged before `email.new`
is published, so a round that dies in between re-publishes without logging
again. If the CRM is down, the email is still published and the failure is
logged; that message is not logged later.

**The triage inbox is rebuilt.** `triage-inbox.json` is the small file the
email classifier and the calendar monitor read instead of the full logs. The
worker rebuilds it right after a round that brought new mail, and otherwise
every five minutes (the Google script's cadence). It holds the email-log
entries not yet categorized (`type: "new"`) and the follow-ups that are due
(`type: "follow-up"`), plus pending items from `calendar-log.json` and
`jira-log.json` when those files sit next to the email log. Items whose
message or conversation id appears as `threadId:` in an open (or recently
resolved, within 72 hours) `escalation` task are left out and listed in
`activeEscalationIds`. Mail items also carry `threadId`, the conversation id
the classifier puts in the tasks it creates. The file is written atomically
(a temporary file, then a rename), and a rebuild with items publishes
`triage.refreshed` on the `email` stream, which the email classifier hooks.
The shape is the `triage_inbox` schema in
[Event Bus](../event-bus.md#email-and-calendar-event-contract).
`ROBOTHOR_TRIAGE_INBOX_PATH` overrides where it goes; by default it is
`triage-inbox.json` beside the email log.

On a Google instance none of this runs: the instance's own sync script logs
to the CRM and writes the triage inbox.

## Auth model

The assistant signs in as an **application**, not as a user:

1. An Entra admin in the client's tenant registers an app and consents to the
   Graph application permissions for mail and calendar.
2. The app authenticates with a **certificate**. Its private key signs a
   10-minute client assertion for each token request, so no shared secret is
   sent. A client secret works as a fallback, but the certificate is
   preferred.
3. **Exchange RBAC for Applications** limits the app to exactly two mailboxes:
   the assistant's and the owner's. An app with tenant-wide access would let
   the assistant read every mailbox in the company. This scope is required.
4. A **canary mailbox** in the same tenant must be unreadable to the app. The
   doctor check `workspace.m365_scope` tries to read it and reports an error
   if it can. That catches a grant that was never scoped.

The credential lives in the instance vault, one row per field, and is re-read
on every token refresh, so a rotated certificate applies without a restart:

| Vault key | Holds |
|-----------|-------|
| `workspace/microsoft365/tenant_id` | Directory id (GUID) or verified domain |
| `workspace/microsoft365/client_id` | Application (client) id |
| `workspace/microsoft365/client_certificate_pem` | The app's certificate (PEM) |
| `workspace/microsoft365/client_private_key_pem` | Its RSA private key (PEM) |
| `workspace/microsoft365/client_secret` | Fallback only, when there is no certificate |

Tokens are cached in memory and refreshed 5 minutes before they expire.
Errors from Entra are reported by their `AADSTS` code only. Entra's own
description can quote a rejected secret back, so it is never logged.

## Settings

| Variable | Meaning |
|----------|---------|
| `ROBOTHOR_WORKSPACE_PROVIDER` | `google` (default) or `microsoft365` |
| `ROBOTHOR_M365_ASSISTANT_MAILBOX` | The assistant's mailbox |
| `ROBOTHOR_M365_OWNER_MAILBOX` | The operator's mailbox, whose calendar the assistant manages |
| `ROBOTHOR_M365_SCOPE_CANARY_MAILBOX` | A mailbox the app must *not* be able to read |
| `ROBOTHOR_M365_INGEST_INTERVAL_SECONDS` | Seconds between ingest rounds (default 60) |
| `ROBOTHOR_EMAIL_LOG_PATH` | Where the ingest merges new mail; empty means `<workspace>/brain/memory/email-log.json` |

See [Settings](../reference/configuration.md) for the generated reference.

## Calendar

`GraphCalendar` (`robothor/workspace/microsoft/calendar.py`) serves every
`gws_calendar_*` tool. Each guard still runs in the handler first, before any
Graph request: do-not-contact on every invitee, the `no_auto` scheduling
policy, duplicate-event detection, the calendar-identity check, and the
cancellation check right before a write. A blocked call sends nothing to
Graph.

**Which calendar.** `calendar="own"` is the assistant mailbox's default
calendar (`/users/{assistant}/calendar`). `calendar="operator"` (the default)
is the owner mailbox's (`/users/{owner}/calendar`), using
`ROBOTHOR_M365_OWNER_MAILBOX` when set and the owner.yaml address otherwise.
An explicit `calendar_id` that is an address is that mailbox's default
calendar; any other id is a calendar in the assistant's mailbox
(`/users/{assistant}/calendars/{id}`).

**Events look like Google's.** The provider translates Graph events into the
Google Calendar v3 shape the tools, dedup and CRM already read:

| Graph | Tool result |
|-------|-------------|
| `subject`, `body` (read as text), `location.displayName` | `summary`, `description`, `location` |
| `start`/`end` (UTC) + `originalStartTimeZone` | `start.dateTime` with an offset, in the event's own zone, plus `timeZone` (IANA) |
| `isAllDay` | `start.date` / `end.date` (end exclusive, as Google) |
| attendee `status.response` | `responseStatus`: `none`/`notResponded` → `needsAction`, `tentativelyAccepted` → `tentative`, `accepted`, `declined` |
| `isOrganizer`, `organizer` | `organizer.self`, `organizer.email`; the mailbox's own entry is `self: true` |
| `isCancelled` | `status: "cancelled"` |
| `webLink` | `htmlLink` |
| `onlineMeeting.joinUrl` | `conferenceData` (solution `teamsForBusiness`) |
| `recurrence` / `seriesMasterId` | `recurrence: ["RRULE:…"]` / `recurringEventId` |
| `@odata.etag` | `etag` |

Listing uses `calendarView`, which expands a series into its occurrences (as
Google's `singleEvents=true`). Ids are Graph **immutable ids**, so the id
recorded at create time stays valid, and the CRM write-through files the
event under `provider = 'microsoft365'` with that id as `external_event_id`.

**Teams meetings.** `with_meet=true` (the default) creates a Microsoft Teams
meeting (`isOnlineMeeting`, `onlineMeetingProvider: teamsForBusiness`); the
join link comes back in `conferenceData`. A Google Meet link cannot be
attached to an Exchange event and is refused.

**Exchange always notifies.** When the organiser creates, changes or cancels a
meeting with attendees, Exchange mails them; there is no quiet mode. So with
attendees involved the `calendar_send_updates` flag must be `all`: under
`none` or `externalOnly` a create, edit or delete of such a meeting is refused
before anything is written (`hint: "unsupported"`). Events without attendees
are unaffected. An RSVP honours the flag exactly (`sendResponse` is false
under `none`).

**Edits.** `gws_calendar_update` and `gws_calendar_add_attendees` use the same
read / merge / conditional write / read-back loop as Google. The write is a
PATCH with `If-Match: <etag>`; if the event changed in between, Graph answers
412 and the loop re-reads, merges and tries once more. `gws_calendar_respond`
reads the event the same way, then sends Graph's `accept`, `decline` or
`tentativelyAccept` action, and reads it back to verify.

**Delete.** On a meeting this mailbox organises, with attendees,
`gws_calendar_delete` calls `/cancel`, which removes the event and sends the
attendees a cancellation (the result says `method: "cancel"`). Anything else
(no attendees, or the mailbox's copy of someone else's meeting) is a plain
`DELETE` (`method: "delete"`).

**Recurrence.** A create may carry one `RRULE` with `FREQ` `DAILY`,
`WEEKLY`, `MONTHLY` or `YEARLY` and only these parts: `INTERVAL`, `BYDAY`
(weekdays; for monthly or yearly, one numbered weekday such as `2TU` or
`-1FR`), `BYMONTHDAY` (one day, 1–31), and `COUNT` or `UNTIL`. Anything else
(`BYSETPOS`, `BYMONTH`, hourly rules, `EXDATE`/`RDATE`, several rules) is
refused before any write rather than approximated. Reading maps every Graph
pattern back to an `RRULE`.

**Time zones.** Graph answers in UTC (`Prefer: outlook.timezone="UTC"`); each
event is shown in its own zone, with Windows zone names (`Eastern Standard
Time`) mapped to IANA (`America/New_York`) by the CLDR table in
`robothor/workspace/microsoft/timezones.py`. Writes send IANA names. A time
with an offset is converted into the event's zone; a time without one is read
in that zone, or the instance's `ROBOTHOR_TIMEZONE`. If a tenant rejects an
IANA name, the create is retried once with the Windows name (or in UTC when
CLDR has none).

`genus workspace connect microsoft365` writes the three mailbox settings to
`config.yaml` (the same writer as `genus config set`) and sets
`ROBOTHOR_WORKSPACE_PROVIDER` only when you pass `--enable` and the probe
passes. It never writes an environment file.

## Read-backs, the email channel and prompts

- **Verification read-backs.** After `gws_gmail_send` or `gws_gmail_reply`
  the post-condition check reads the message back through Graph by the
  immutable id the send returned. After `gws_calendar_create` it reads the
  event back with `calendar.get` on the calendar the create reported (the
  provider resolves `own` to the assistant mailbox and `operator` to
  `m365_owner_mailbox`). A cancelled event does not count. The gws CLI is
  never asked. See [Tool post-conditions](../runbooks/TOOL_POSTCONDITIONS.md).
- **Email channel.** `delivery.channel: email` sends through Graph as the
  assistant mailbox, never through the gws CLI or SMTP. `genus doctor --only
  email.transport` names the Graph transport, and fails when no assistant
  mailbox is set. See [Email](../channels/email.md).
- **Prompts.** The engine tells the model it has "your own Microsoft 365
  account" (with `ROBOTHOR_AI_EMAIL`, or the assistant mailbox when that is
  unset). The tool descriptions are provider-neutral.

### Autonomy mailbox verification is disabled

The autonomy feature can read a website's one-time verification code from the
mailbox. Its anti-spoofing rule trusts an `Authentication-Results` header only
when it starts `mx.google.com;`, which Gmail's receiving server adds. On
Exchange Online a sender can write that header into the message themselves, so
it proves nothing. Until an Exchange equivalent has its own security review,
mailbox verification is **off** on Microsoft 365. The request fails before
any mailbox query, authority check or profile use, with
`error: mailbox_verification_unsupported` and the reason "mailbox verification
is not supported on Microsoft 365 yet", and no code is ever extracted. Google
instances are unchanged.

## Connect a tenant

This runbook connects one Microsoft 365 tenant to one Genus OS instance. It
takes about 30 minutes, plus however long Exchange takes to apply the scope.
An admin in the client's tenant does steps 2, 4 and 5; the instance operator
does the rest.

### Prerequisites

- **An admin in the client's tenant.** Registering the app needs an
  Application Administrator (or Global Administrator) in Entra ID. Scoping it
  needs an Exchange Administrator (or Global Administrator) with the Exchange
  Online PowerShell module (`Install-Module ExchangeOnlineManagement`).
- **The assistant's mailbox.** A licensed user mailbox (any Microsoft 365
  plan that includes Exchange Online). Mail is sent from it and its inbox is
  read.
- **The owner's mailbox.** The operator's own mailbox. The assistant reads and
  edits its calendar.
- **A canary mailbox.** Any third mailbox in the same tenant, for example an
  admin or test user. The app must *not* be able to read it. The doctor tries,
  and an app that can is reported as an error.
- **Shell access to the Genus OS instance** as the user the engine runs as,
  with the vault reachable (`genus vault list` works).

### 1. Preview the plan

A dry run reads the vault and settings and writes nothing. It needs the
directory and application ids from step 2, so run it after step 2 if you
don't have them yet:

```bash
genus workspace connect microsoft365 --tenant-id <directory-id> --client-id <application-id> --assistant-mailbox assistant@example.com --owner-mailbox owner@example.com --canary-mailbox canary@example.com --dry-run
```

### 2. Register the app in Entra ID

In the [Microsoft Entra admin center](https://entra.microsoft.com):

1. **Identity > Applications > App registrations > New registration.**
   Name it, for example, `Genus OS assistant`. Supported account types:
   **Accounts in this organizational directory only (single tenant)**. No
   redirect URI.
2. From the app's **Overview**, copy the **Application (client) ID** and the
   **Directory (tenant) ID**.
3. Don't create a client secret. The connect command makes a certificate.

### 3. Create the certificate and store the credential

On the instance:

```bash
genus workspace connect microsoft365 --tenant-id <directory-id> --client-id <application-id> --assistant-mailbox assistant@example.com --owner-mailbox owner@example.com --canary-mailbox canary@example.com
```

The command:

- generates an RSA 3072 key and a self-signed certificate valid for one year,
  unless one is already stored (then it reuses it);
- stores the directory id, client id, certificate and private key in the vault
  under `workspace/microsoft365/`, for this instance's platform tenant;
- writes the three mailbox settings to `config.yaml`;
- prints the certificate (PEM), its SHA-1 and SHA-256 thumbprints, the Graph
  permissions and the PowerShell for step 5, with your values filled in.

The private key is never printed, not even with `--json`. Re-running the
command is safe: it reuses the stored certificate unless you pass `--rotate`.

### 4. Upload the certificate

Copy the printed block from `-----BEGIN CERTIFICATE-----` to
`-----END CERTIFICATE-----` into a file named `genus-os.cer`. In the app
registration, go to **Certificates & secrets > Certificates > Upload
certificate** and choose the file. Check that the **Thumbprint** Entra shows
matches the printed SHA-1 thumbprint.

### 5. Grant the permissions and scope them to two mailboxes

The app needs three Microsoft Graph **application** permissions:
`Mail.ReadWrite`, `Mail.Send` and `Calendars.ReadWrite`. `MailboxSettings.Read`
is optional; the doctor uses it only to compare timezones.

On their own, application permissions cover **every mailbox in the tenant**.
Use one of the two options below to limit them to the assistant's and the
owner's mailboxes. Don't use both.

#### Option A (recommended): RBAC for Applications

With RBAC for Applications the permissions are granted **in Exchange**, by
role assignments limited to a management scope. **Don't also add and
admin-consent the same permissions under API permissions in Entra.** An Entra
consent is tenant-wide, and Exchange adds it to the scoped grant, so the app
could read every mailbox again. The canary check catches this.

```powershell
Connect-ExchangeOnline -UserPrincipalName admin@example.com

# ObjectId is the ENTERPRISE APPLICATION (service principal) object id:
# Entra > Enterprise applications > Genus OS assistant > Object ID.
# It is not the object id on the app registration's Overview page.
New-ServicePrincipal -AppId <application-id> -ObjectId <enterprise-app-object-id> -DisplayName "Genus OS assistant"

New-ManagementScope -Name "Genus OS assistant mailboxes" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'assistant@example.com' -or PrimarySmtpAddress -eq 'owner@example.com'"

New-ManagementRoleAssignment -App <application-id> -Role "Application Mail.ReadWrite" -CustomResourceScope "Genus OS assistant mailboxes"
New-ManagementRoleAssignment -App <application-id> -Role "Application Mail.Send" -CustomResourceScope "Genus OS assistant mailboxes"
New-ManagementRoleAssignment -App <application-id> -Role "Application Calendars.ReadWrite" -CustomResourceScope "Genus OS assistant mailboxes"
New-ManagementRoleAssignment -App <application-id> -Role "Application MailboxSettings.Read" -CustomResourceScope "Genus OS assistant mailboxes"

Test-ServicePrincipalAuthorization -Identity <application-id> -Resource assistant@example.com   # InScope True
Test-ServicePrincipalAuthorization -Identity <application-id> -Resource canary@example.com      # InScope False
```

Exchange can take from 30 minutes to two hours to apply new role assignments
to Graph (verify against Microsoft docs). If the doctor still reports a 403
for the assistant or the owner, wait and run it again.

#### Option B (legacy): ApplicationAccessPolicy

Microsoft is replacing application access policies with RBAC for
Applications. Use this only if a tenant already relies on them.

1. In Entra, go to **API permissions > Add a permission > Microsoft Graph >
   Application permissions**. Add `Mail.ReadWrite`, `Mail.Send` and
   `Calendars.ReadWrite` (and optionally `MailboxSettings.Read`), then select
   **Grant admin consent**.
2. Restrict the grant to a mail-enabled security group holding the two
   mailboxes:

```powershell
Connect-ExchangeOnline -UserPrincipalName admin@example.com
New-DistributionGroup -Name "Genus OS assistant mailboxes" -Alias genus-os-assistant-mailboxes -Type Security -Members assistant@example.com,owner@example.com
New-ApplicationAccessPolicy -AppId <application-id> -PolicyScopeGroupId genus-os-assistant-mailboxes -AccessRight RestrictAccess -Description "Genus OS: assistant and owner only"
Test-ApplicationAccessPolicy -Identity assistant@example.com -AppId <application-id>   # Granted
Test-ApplicationAccessPolicy -Identity canary@example.com -AppId <application-id>      # Denied
```

### 6. Run the doctor

```bash
genus doctor --category workspace
```

Every check must pass:

| Check | Severity | Passes when |
|-------|----------|-------------|
| `workspace.m365_connection` | required | The mailboxes are set, the vault holds the credential, Entra issues a token, and the assistant's inbox and the owner's calendar can be read |
| `workspace.m365_scope` | required | Reading the canary mailbox is **denied**. If the app can read it, its scope is not restricted: it can read mailboxes beyond the assistant and the owner. That is an error |
| `workspace.m365_assistant_identity` | required | `ROBOTHOR_AI_EMAIL` is the assistant mailbox. The mail guards recognise the assistant by `ROBOTHOR_AI_EMAIL` (the duplicate-reply guard and reply-all), while on Microsoft 365 it sends from the assistant mailbox, so a different address makes the guard miss the assistant's own replies |
| `workspace.m365_canary_configured` | recommended | A canary mailbox is set. Without one the scope is unproven |
| `workspace.m365_timezone` | recommended | The owner's Exchange timezone matches `ROBOTHOR_TIMEZONE`. Windows zone names such as `Eastern Standard Time` are mapped to IANA names. A zone that can't be mapped, or settings the app may not read, are reported but don't fail |
| `workspace.m365_ingest_freshness` | recommended | Only once `ROBOTHOR_WORKSPACE_PROVIDER=microsoft365`: the [ingest](#ingestion) finished a round for the inbox (and the calendar, when an owner mailbox is set) within three ingest intervals. It reads the database, not Graph |

The checks run only on an instance with `ROBOTHOR_WORKSPACE_PROVIDER=microsoft365`
or with a Microsoft 365 credential in the vault. Everywhere else they skip.
With `--offline`, only the local configuration step runs. Results name the
configured mailboxes and Graph status codes, never a token or mail content.

On a Microsoft 365 instance, two Google-only checks pass with a note instead
of asking the `gws` CLI: `calendar.operator_calendar_writable` and the `gws`
half of `email.transport`. SMTP settings, if any, are still checked.

### 7. Enable

```bash
genus workspace connect microsoft365 --enable
```

The ids and mailboxes from step 3 are reused, so you only need flags to
change them. `--enable` runs the connection, scope and assistant-identity probes and sets
`ROBOTHOR_WORKSPACE_PROVIDER=microsoft365` in `config.yaml` only if all pass
(set `ROBOTHOR_AI_EMAIL` to the assistant mailbox first).
It needs a canary: an unproven scope is never enabled. Restart the engine
afterwards so it reads the new setting.

### Rotating the certificate

The certificate is valid for one year. Before it expires:

1. Run `genus workspace connect microsoft365 --rotate` to store a new one.
2. Upload the printed certificate to the app registration (step 4) straight
   away. Delete the old one there once `genus doctor --category workspace`
   passes.

The transport re-reads the vault on every token refresh, so no restart is
needed. Token requests fail between steps 1 and 2, so do both together.

### Troubleshooting

| The doctor says | Cause | Fix |
|-----------------|-------|-----|
| `microsoft365 not connected` | No credential in the vault for this platform tenant | Run step 3 as the engine's user |
| `invalid_client: AADSTS700027` | The stored certificate isn't uploaded to the app, or a different one is | Step 4; compare thumbprints |
| `AADSTS700016` | Wrong client id, or the app is in another directory | Check both ids from step 2 |
| `cannot read the assistant inbox ... graph HTTP 403` | Role assignment missing, mailbox not in the scope, or not applied yet | Step 5; wait for Exchange to apply it |
| `app scope is not restricted` | Tenant-wide Entra consent next to RBAC, or no scope at all | Remove the permissions under API permissions in Entra (option A), or finish option B |
| `canary mailbox ... was not found` | Typo, or the canary is not a mailbox | Set `ROBOTHOR_M365_SCOPE_CANARY_MAILBOX` to a real mailbox |
| `Microsoft 365 ingest is behind` | The worker is failing each round, or the engine is down | Look for `microsoft365 ... ingest deferred` in the engine journal; run the connection check |
| `could not read the ingest state` | Migration 149 not applied | `genus migrate` |

## Verification

Two suites under `robothor/workspace/tests/contract/` hold the providers to
one contract. Both run in CI with no network, against in-memory fakes.

- **Provider contract** (`test_contract.py`). Every scenario runs twice, on
  Google and on Microsoft 365, through the real `gws_*` handlers and their
  guards. The scenarios: search, read and reply-all in the same conversation;
  a sent id read back by the engine's verification; a label round trip; the
  duplicate-reply skip; do-not-contact on send, reply, create, add-attendees
  and RSVP; a `no_auto` scheduling policy; dedup and `force`; a create, a
  conditional edit that retries once after a 412, an attendee add, an RSVP and
  a delete, each read back; cancellation before the write; benchmark refusal of
  every tool; and the CRM rows' `provider` and external id. Every refusal is
  asserted to make **zero** write requests to the provider. A difference
  between the providers is read from `Workspace.capabilities`, not from the
  provider's name. `test_m365_full_flow.py` runs the same steps as one day on
  Microsoft 365: mail arrives, is triaged and answered in its conversation, a
  meeting is made, edited, accepted and cancelled, an opted-out recipient is
  blocked and a duplicate is caught. Delta ingestion has its own suite.
- **No Google** (`test_no_google.py`). With `workspace_provider=microsoft365`,
  `PATH` empty, no Google credential, and any attempt to resolve or run `gws`
  failing the test: every mail and calendar tool, the engine's post-write
  read-backs, the doctor's channel, workspace, calendar and tools checks, and
  the email delivery channel all run. The test asserts zero `gws` calls, no
  logged error or traceback, and no failing doctor check. The Google-only
  checks pass with a note naming `microsoft365`.

The email delivery channel follows the setting too. On Microsoft 365 it sends
through Microsoft Graph as the assistant mailbox. It never probes for the `gws`
CLI and never falls back to SMTP. See [Email](../channels/email.md).

### Verify against Microsoft docs

These details come from Microsoft's documentation and change from time to
time. Check them against the current pages before a client rollout:

- The RBAC for Applications role names (`Application Mail.ReadWrite`,
  `Application Mail.Send`, `Application Calendars.ReadWrite`,
  `Application MailboxSettings.Read`), and that an Entra consent adds to the
  scoped grant rather than being replaced by it.
- That `New-ServicePrincipal -ObjectId` takes the enterprise application's
  object id, and how long role assignments take to reach Graph.
- That an out-of-scope mailbox answers HTTP 403 (`ErrorAccessDenied`) rather
  than 404.
- That `GET /users/{id}/mailboxSettings/timeZone` needs `MailboxSettings.Read`
  and is not covered by `Mail.ReadWrite`.
