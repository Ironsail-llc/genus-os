# Microsoft 365 workspace

> **Status: in development.** The Microsoft Graph transport and its sign-in
> exist, but no tool uses them yet. Leave `ROBOTHOR_WORKSPACE_PROVIDER` at
> `google`: with `microsoft365` selected, every mail and calendar tool refuses
> (`hint: "unsupported"`) until the Microsoft 365 mail and calendar providers
> ship. This page grows with each part of the work; the connect command,
> the doctor check and the full runbook come later.

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
| Provider (`robothor/workspace/protocols.py`) | `GoogleMail`, `GoogleCalendar` (`robothor/workspace/google/adapter.py`) | not yet built (selecting it refuses) |
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
