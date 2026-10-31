# Event Bus

Redis Streams-based publish-subscribe system with consumer groups, RBAC, and JSON file fallback.

## Streams

These streams carry system events:

| Stream | Key | Events |
|--------|-----|--------|
| Email | `robothor:events:email` | `email.new`, `email.classified`, `email.responded` |
| Calendar | `robothor:events:calendar` | `calendar.new`, `calendar.modified`, `calendar.rescheduled`, `calendar.cancellation`, `calendar.conflict` |
| CRM | `robothor:events:crm` | `crm.create`, `crm.update`, `crm.merge`, `crm.delete` |
| Vision | `robothor:events:vision` | `vision.motion`, `vision.person`, `vision.unknown` |
| Health | `robothor:events:health` | `health.check`, `health.alert`, `health.recovery` |
| Agent | `robothor:events:agent` | `agent.started`, `agent.completed`, `agent.error` |
| System | `robothor:events:system` | `system.boot`, `system.shutdown`, `system.error` |
| Operations | `robothor:events:operations` | `operation_action.approved` — see [Operations ledger](OPERATIONS.md#executing-an-approved-action-from-a-workflow) |

Each stream is a circular buffer (default 10,000 entries, configurable via `EVENT_BUS_MAXLEN`).

## Envelope Format

Every message uses a standard envelope:

```json
{
  "id": "1708123456789-0",
  "timestamp": "2026-02-22T10:30:00+00:00",
  "type": "email.new",
  "source": "email-sync",
  "actor": "robothor",
  "payload": "{\"from\": \"alice@example.com\", \"subject\": \"Meeting\"}",
  "correlation_id": "trace-abc-123"
}
```

The `payload` field is a JSON string (Redis Streams require flat field values).

## Email and calendar event contract

The `email.new` and `calendar.*` payloads are written down as JSON Schemas in
`robothor/events/schemas/`. Load and check them with
`robothor.events.contract` (`schema(name)`, `validate(name, payload)`). The
Google sync scripts and the Microsoft 365 ingest worker
(`robothor/workspace/ingest/`) both publish to this contract, so a consumer
does not need to know which provider sent an event.

**`email.new`** on the `email` stream (`email_new.json`). One event per new
inbound message, published once:

| Field | Type | Meaning |
|-------|------|---------|
| `id` | string, required | Provider message id. The dedup key, and the key of the message's `email-log.json` entry |
| `from` | string or null, required | The From header (`Name <address>` or a bare address) |
| `subject` | string or null, required | |
| `date` | string or null, required | The Date header (RFC 2822) |
| `labels` | string array, required | Gmail label ids. Microsoft 365 builds them: `UNREAD`, `INBOX`, `STARRED`, `IMPORTANT`, then Outlook categories |
| `threadId` | string, optional | Conversation id |
| `provider` | `google` or `microsoft365`, optional | Absent means google |

**`calendar.*`** on the `calendar` stream (`calendar_event.json`). The
meeting fields are the `calendar-log.json` meeting shape:

| Field | Type | Meaning |
|-------|------|---------|
| `id` | string, required | Provider event id |
| `change_type` | required | `created` (`calendar.new`), `updated` (`calendar.modified`), `rescheduled` (`calendar.rescheduled`), `cancelled` (`calendar.cancellation`) |
| `title` | string, required | |
| `start`, `end` | string, required | ISO 8601 UTC, or a date for all-day events. Empty when a deleted event can't be read |
| `attendees` | string array, required | Attendee email addresses |
| `hangoutLink` | string, optional | Online-meeting join URL |
| `provider` | optional | As above |

`calendar.new`, `calendar.modified` and `calendar.rescheduled` trigger the
calendar pipeline. `calendar.cancellation` is what the calendar consumer
handles.

**`email-log.json`** (`email_log.json`) is
`{"lastCheckedAt": ..., "entries": {<id>: entry}}`. An entry carries `id`,
`threadId`, `fetchedAt`, the envelope (`from`, `subject`, `date`, `labels`)
and the triage stages (`readAt`, `categorizedAt`, `urgency`, `category`,
`actionRequired`, `actionCompletedAt`, `pendingReviewAt`, `reviewedAt`), all
null when the entry is new. A writer merges into the file and never truncates
it: an entry is keyed by message id and added once, and the entries it didn't
write keep every field.

**`triage-inbox.json`** (`triage_inbox.json`) is
`{"preparedAt", "counts": {"emails", "calendar", "jira", "total"}, "items",
"activeEscalationIds"}`: what the triage agents still have to look at,
rebuilt after each mail sync. An item has a `source` (`email`, `calendar` or
`jira`) and a `type`. Email items are `new` (not yet categorized: `id`,
`threadId`, `from`, `subject`, `date`, `labels`, `snippet`, `messageCount`)
or `follow-up` (a due `pendingReviewAt`); calendar items are `meeting` or
`change`; jira items are `pending-action`. `activeEscalationIds` lists the
thread ids already escalated, which are left out of `items`. A rebuild with
items is announced as **`triage.refreshed`** on the `email` stream, payload
`{"total": <items>, "emails": <email items>}`.

## Publishing

```python
from robothor.events.bus import publish

# Basic publish
msg_id = publish("email", "email.new", {
    "from": "alice@example.com",
    "subject": "Q3 Planning",
    "urgency": "normal",
}, source="email-sync")

# With RBAC enforcement (checks agent_capabilities.json)
msg_id = publish("crm", "crm.create", {
    "entity": "person",
    "name": "Alice Smith",
}, source="crm-steward", agent_id="crm-steward")

# Returns None on failure (never raises)
```

Publish is fire-and-forget. If Redis is down, the call returns `None` and logs a warning. Services should not depend on publish success for correctness.

## Webhook ingress

External services reach the bus through `POST /api/webhooks/{channel}`
(`robothor/engine/webhooks.py`). Channels are defined in
`$ROBOTHOR_WORKSPACE/docs/webhooks.yaml`, plus every `*.yaml` drop-in in
`docs/webhooks.d/` (instance files, merged in name order after the base file, so a plugin can
ship its own channels). The base file is strict: if it does not parse, or a
number in it is not one, the engine fails to start, as before. Drop-ins are lenient: a
drop-in that does not parse, a bad channel in one, or a channel it declares again is
logged as an error naming the file and skipped, and every other channel is still
served. Each channel names the stream it publishes to, the environment variable that holds its signing secret, and how the event
type is derived. A request is checked in this order: signature, rate limit,
JSON body, delivery id, then published.

| Field | Default | Meaning |
|-------|---------|---------|
| `signature_scheme` | `hmac_hex` | `hmac_hex`: hex HMAC-SHA256 of the raw body (an optional `sha256=` prefix is accepted). `stripe_t_v1`: a header of the form `t=<unix seconds>,v1=<hex>[,v1=<hex>...]`, valid when the timestamp is within the tolerance and some `v1` equals HMAC-SHA256 of `"<t>."` followed by the raw body. `hmac_ts_hex`: the signature header carries a hex HMAC-SHA256 of `"<timestamp>."` followed by the raw body (an optional `sha256=` prefix, in any case, is accepted), and `timestamp_header` carries the timestamp in integer unix seconds, which must be within the tolerance. `google_oidc`: see below. |
| `signature_header` | _(unset)_ | Read the signature from this header only. Unset tries `X-Hub-Signature-256`, `X-Signature-256` and `X-Webhook-Signature`, in that order. |
| `timestamp_tolerance_seconds` | `300` | `stripe_t_v1` and `hmac_ts_hex`: how far the signed timestamp may be from the receiver's clock, either way. |
| `timestamp_header` | `X-Webhook-Timestamp` | `hmac_ts_hex` only: the header carrying the signed timestamp. Seconds only: a millisecond value is refused as stale. |
| `oidc_audience` | _(unset)_ | `google_oidc` only, required: the audience the push's ID token must carry (the push subscription's audience, usually this channel's URL). |
| `oidc_service_account` | _(unset)_ | `google_oidc` only, required: the service account the push subscription signs as. The token's `email` claim must equal it and `email_verified` must be true. |
| `delivery_id_header` | _(unset)_ | When set and present, the id is recorded in Redis for 24 hours (`robothor:webhook:delivery:<channel>:<id>`). A repeat answers `200 {"status": "duplicate"}` and is not published again. If Redis cannot be reached the request is refused with `503`, as the rate limiter refuses it, so the sender retries. |

A bad, stale or malformed signature answers `401 {"error": "Invalid
signature"}` whatever the scheme; the reason is logged, the secret never is.
For `hmac_ts_hex` the logged reason names the check that failed: `missing
timestamp`, `malformed timestamp`, `stale timestamp`, `missing signature`,
`malformed signature` (not 64 hex characters after the optional prefix) or
`bad digest` (well formed, but no match). The signed string uses the timestamp
as a plain integer, so a sender that zero-pads it is refused.

**`google_oidc`** is for Google Cloud Pub/Sub push subscriptions with
authentication on. There is no shared secret, so `secret_env` is not used;
instead the `Authorization: Bearer <ID token>` header is verified with
google-auth (Google's signing certificates, the channel's `oidc_audience`),
then the `email` claim is checked against `oidc_service_account`. A channel
missing either field answers `503`, as an HMAC channel without its secret
does. Because verification is the costly step, a `google_oidc` request is
rate-limited first, and a bearer that is not a three-segment JWT is refused
before any verification. Tokens are verified on a dedicated pool of four
threads, and Google's signing certificates are reused for 30 minutes (fetched
with a 5 second timeout). Since the rate limit counts requests before they
are verified, a flood of forged tokens can spend a channel's budget. Real
pushes are then answered `429`, which Pub/Sub retries with backoff, and the
Gmail ingest works from its history cursor, so the first push that gets
through catches up on everything. The body must be a Pub/Sub push envelope: `message.data` is
base64-decoded and, when it is a JSON object, its keys become the published
payload (otherwise `{"data": "<text>"}`), with a `pubsub` object beside them
(`message_id`, `publish_time`, `attributes`, `subscription`). A body that is
not an envelope, or data that is not base64, answers `400`. The Pub/Sub
`messageId` is the delivery id unless the channel names a
`delivery_id_header`, so Pub/Sub's at-least-once redeliveries are answered as
duplicates. With no event type header or field, `event_type_prefix` is the
event type (`gmail.push` below).

The delivery id is a redelivery filter, not replay protection: it is not
covered by the signature, so a captured request resent under another id is
caught only by the `stripe_t_v1` or `hmac_ts_hex` timestamp window. If the event cannot be
written to the stream the request answers `503` so the sender retries; a
claimed delivery id is released first.

A webhook event's envelope carries `event_type` (the same value as `type`) and,
when the channel has a delivery id header, `delivery_id`, next to `payload`. An
agent hook with `include_payload: true` gets both, plus the payload, appended
to its trigger message, inside an `<untrusted_content>` boundary since the
sender is external. `requeue_if_busy: true` re-adds an event that arrived
while the agent was already running, for that agent only, after a pause that
starts at 5 seconds and backs off to a minute, for up to about 50 minutes
before it is parked in `<stream>:dlq`. Each event is handled off the consumer
loop, up to 100 at a time: the event's workflows start first, its agents run
side by side, and one agent's hook runs take turns. Hook agent runs share the
fleet's `max_concurrent_agents` limit. A long run holds up only later events
for the same agent, unless 100 of them pile up, when the consumer waits. An
event read but not yet acknowledged when the engine stops or crashes stays
pending in Redis and is handled again at the next start if it is under an hour
old and has been tried fewer than three times; otherwise it goes to
`<stream>:dlq`. A handler may therefore see an event twice and should be
idempotent. Under HA (`ROBOTHOR_HA_LEADER_ENABLED`) the replicas share one
consumer name, so nothing is replayed at start, and a crashed replica's
entries stay pending. `include_payload` and `requeue_if_busy` are documented
with the other hook fields in `docs/agents/schema.yaml`.

A workflow with a `hook` trigger gets the event as its run's `event` context
(`type`, `stream`, `delivery_id`, the parsed `payload`, and `id` for webhook
events), so its steps
can template it, and its trigger takes the same `requeue_if_busy` option. See
[Agent Builder](AGENT_BUILDER.md#triggered-by-an-event).

## Gmail push

A Google instance can ingest the assistant's Gmail on push instead of from a
sync script (`robothor/workspace/ingest/gmail_push.py`,
`robothor/workspace/ingest/google.py`). It is off by default.

1. Gmail's `users.watch` sends a notification to a Pub/Sub topic whenever
   the inbox changes. With `gmail_push_topic` set, the engine calls it at
   start and daily (a watch lapses after seven days) and logs the expiration.
2. A push subscription POSTs each notification to a `google_oidc` webhook
   channel, which publishes `{emailAddress, historyId, pubsub}` to its stream
   (`gmail_push_stream`, default `gmail`).
3. The engine reads that stream as consumer group `gmail-push`. A push for
   `gmail_push_mailbox` runs one round of the Gmail history ingestor: it
   reads `users.history.list` (inbox additions) from the saved `historyId`
   and publishes `email.new` (`provider: google`) for each new unread inbox
   message, after merging it into `email-log.json` and logging it to the CRM.
   A burst of pushes costs at most two rounds, the one in flight and one
   more. The push's own `historyId` is not used; the saved cursor is.

The first round, and a round whose cursor Gmail has expired (`404` from
`history.list`), publish nothing and start again at the mailbox's current
`historyId`, so old mail is never replayed. A message id is published once,
whatever the rounds re-read; seen ids are purged after 45 days, hourly, as
on Microsoft 365. With HA leader election on, only the leader watches and
ingests.

Setup, for the operator:

1. In the Google Cloud project, create a Pub/Sub topic and grant
   `gmail-api-push@system.gserviceaccount.com` the Pub/Sub Publisher role on
   it.
2. Create a service account for the push (it needs no roles of its own), and
   a push subscription on the topic to
   `https://<engine host>/api/webhooks/<channel>`, with authentication on:
   that service account, and the endpoint URL as the audience.
3. Add the channel to `$ROBOTHOR_WORKSPACE/docs/webhooks.yaml`
   (`signature_scheme: google_oidc`, `oidc_audience`, `oidc_service_account`,
   `stream`, `event_type_prefix: "gmail.push"`; see the commented example in
   `docs/webhooks.yaml`).
4. Set `ROBOTHOR_GMAIL_PUSH_ENABLED=true`, `ROBOTHOR_GMAIL_PUSH_TOPIC` to the
   topic's full name (`projects/<project>/topics/<topic>`),
   `ROBOTHOR_GMAIL_PUSH_MAILBOX` to the address gws is signed in as, and
   `ROBOTHOR_GMAIL_PUSH_STREAM` if the channel's stream is not `gmail`. The
   gws account needs a Gmail read scope. Restart the engine.
5. Turn off the instance sync script's mail half (its `email.new`, email log
   and CRM logging), or every message is published twice.

The settings are in [Configuration](reference/configuration.md).

## Subscribing

```python
from robothor.events.bus import subscribe

def handle_email(event):
    payload = event["payload"]  # Already parsed from JSON
    print(f"New email from {payload['from']}: {payload['subject']}")

# Blocking consumer loop with auto-ack
subscribe(
    "email",                    # Stream name
    "classifier-group",         # Consumer group
    "classifier-worker-1",      # Consumer name
    handler=handle_email,
    batch_size=10,              # Messages per read
    block_ms=5000,              # Block timeout
)

# With RBAC check
subscribe("email", "my-group", "worker-1",
    handler=handle_email,
    agent_id="email-classifier",  # Must have read access to "email" stream
)
```

Consumer groups are created automatically. Messages are auto-acknowledged after successful handler execution. Failed messages remain pending for retry.

## Reading Without Groups

For dashboards and monitoring, read recent events without a consumer group:

```python
from robothor.events.bus import read_recent, stream_info, stream_length

# Last 10 events
events = read_recent("system", count=10)

# Stream metadata
info = stream_info("email")
# {"length": 1523, "first_entry": ..., "last_entry": ..., "groups": 2}

length = stream_length("email")
```

## Agent RBAC

Define per-agent permissions in `agent_capabilities.json`:

The bridge refuses a request carrying an agent's service token (for example
`log_interaction`) with 403 `agent not authorized` unless that agent is listed
under `agents`; `default_policy` does not apply to it.

```json
{
  "default_policy": "allow",
  "agents": {
    "email-classifier": {
      "tools": ["search_memory", "log_interaction"],
      "streams_read": ["email", "agent"],
      "streams_write": ["email", "agent"],
      "bridge_endpoints": ["GET /api/*"]
    },
    "vision-monitor": {
      "tools": ["look", "who_is_here"],
      "streams_read": ["vision"],
      "streams_write": ["vision"],
      "bridge_endpoints": []
    }
  }
}
```

Check access programmatically:

```python
from robothor.events.capabilities import (
    check_tool_access,
    check_stream_access,
    check_endpoint_access,
    get_agent_tools,
    list_agents,
)

# Tool access
check_tool_access("vision-monitor", "list_people")    # False
check_tool_access("email-classifier", "search_memory") # True

# Stream access
check_stream_access("vision-monitor", "email", "read")  # False
check_stream_access("vision-monitor", "vision", "write") # True

# Endpoint access (supports wildcards)
check_endpoint_access("email-classifier", "GET", "/api/people/123")  # True
check_endpoint_access("vision-monitor", "POST", "/api/people")       # False
```

Unknown agents get full access when `default_policy` is `"allow"` (backward compatible).

## Building Consumers

Use the base consumer class for event-driven workers:

```python
from robothor.events.consumers.base import BaseConsumer

class MyConsumer(BaseConsumer):
    stream = "email"
    group = "my-processor"
    consumer_name = "worker-1"

    async def handle(self, event: dict) -> None:
        payload = event["payload"]
        # Process the event
        print(f"Processing: {event['type']}")

# Run the consumer
consumer = MyConsumer()
consumer.run()  # Blocks, handles signals for graceful shutdown
```

## Connection

The bus connects with the same Redis settings as the rest of the engine:
`ROBOTHOR_REDIS_HOST`, `ROBOTHOR_REDIS_PORT`, `ROBOTHOR_REDIS_DB` and
`ROBOTHOR_REDIS_PASSWORD` (the password is percent-encoded into the URL, so any
character is safe). `REDIS_URL`, when set, is an explicit override that wins
for the bus alone; leave it unset unless the bus must reach a different server
from everything else. Under pytest the resolved destination, whichever way it
was reached, must be the test namespace (`ROBOTHOR_TEST_REDIS_URL`) or listed
in `ROBOTHOR_EVENT_BUS_ALLOW`.

The dashboard reads the same streams (its live event views) and resolves its
connection in the same order: `REDIS_URL` when set, otherwise
`ROBOTHOR_REDIS_HOST`, `ROBOTHOR_REDIS_PORT` and `ROBOTHOR_REDIS_PASSWORD`,
otherwise `redis://localhost:6379`; in both of the latter it selects the DB
from `ROBOTHOR_REDIS_DB` (default 0), as the Python services do. On Kubernetes
the Helm chart supplies the host, port and password variables to the dashboard
when the in-chart cache is enabled.

## Feature Flag

Disable the event bus entirely by setting `EVENT_BUS_ENABLED=false`. All publish calls return `None`, subscribe calls return immediately. Useful for testing or minimal deployments without Redis.

## Testing

```python
from robothor.events.bus import set_redis_client, reset_client, cleanup_stream

# Inject a mock Redis client
set_redis_client(mock_redis)

# Clean up after tests
cleanup_stream("email")
reset_client()
```
