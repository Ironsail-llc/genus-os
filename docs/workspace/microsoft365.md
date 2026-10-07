# Microsoft 365 workspace

> **Status: in development.** Calendar works: with
> `ROBOTHOR_WORKSPACE_PROVIDER=microsoft365` and an assistant mailbox set, the
> `gws_calendar_*` tools run against Exchange Online (see [Calendar](#calendar)).
> Mail does not yet: every `gws_gmail_*` tool refuses (`hint: "unsupported"`)
> until the Microsoft 365 mail provider ships. The connect command, the doctor
> check and the full runbook come later.

Genus OS reads and sends mail and manages calendars through the `gws_*` tools.
By default they run against Google Workspace. With
`ROBOTHOR_WORKSPACE_PROVIDER=microsoft365` the same tools are meant to run
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
| Provider (`robothor/workspace/protocols.py`) | `GoogleMail`, `GoogleCalendar` (`robothor/workspace/google/adapter.py`) | `GraphCalendar` (`robothor/workspace/microsoft/calendar.py`); mail not yet built (refuses) |
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
   doctor check (coming later) tries to read it and reports an error if it
   can. That catches a grant that was never scoped.

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
