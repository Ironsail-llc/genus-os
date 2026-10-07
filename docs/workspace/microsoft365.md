# Microsoft 365 workspace

> **Status: mail available, calendar in development.** With
> `ROBOTHOR_WORKSPACE_PROVIDER=microsoft365`, the `gws_gmail_*` tools read and
> send mail from the assistant's Exchange Online mailbox. The
> `gws_calendar_*` tools still refuse (`hint: "unsupported"`) until the
> Microsoft 365 calendar ships. The connect command, the doctor check and the
> full runbook come later.

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
| Provider (`robothor/workspace/protocols.py`) | `GoogleMail`, `GoogleCalendar` (`robothor/workspace/google/adapter.py`) | `GraphMail` (`robothor/workspace/microsoft/mail.py`); calendar not yet built (refuses) |
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
`ROBOTHOR_M365_ASSISTANT_MAILBOX`. Without that setting every mail tool
refuses. The Graph client is built from the vault of the platform tenant
that made the tool call.

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
