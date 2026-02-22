# Event Bus

Redis Streams-based publish-subscribe system with consumer groups, RBAC, and JSON file fallback.

## Streams

Seven streams carry all system events:

| Stream | Key | Events |
|--------|-----|--------|
| Email | `robothor:events:email` | `email.new`, `email.classified`, `email.responded` |
| Calendar | `robothor:events:calendar` | `calendar.new`, `calendar.changed`, `calendar.conflict` |
| CRM | `robothor:events:crm` | `crm.create`, `crm.update`, `crm.merge`, `crm.delete` |
| Vision | `robothor:events:vision` | `vision.motion`, `vision.person`, `vision.unknown` |
| Health | `robothor:events:health` | `health.check`, `health.alert`, `health.recovery` |
| Agent | `robothor:events:agent` | `agent.started`, `agent.completed`, `agent.error` |
| System | `robothor:events:system` | `system.boot`, `system.shutdown`, `system.error` |

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
