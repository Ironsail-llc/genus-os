# CRM Data Fabric — Contact 360

The kernel-level layer that guarantees every signal touching any human gets
attached to that human's record, durably and queryably. Every channel write
is mirrored into this fabric at the moment of I/O.

## Reference designs

- **Twenty CRM** — `messaging/`, `calendar/`, `match-participant/`,
  `contact-creation-manager/`, `timeline/`. Per-channel tables with
  participant junctions and a denormalized timeline.
- **HubSpot** — Emails `0-49`, Calls `0-48`, Meetings `0-47`, Notes `0-46`,
  Tasks `0-27`. Separate engagement objects joined by the Associations API.

We follow this pattern.

## Shape

```
crm_people ───┬── contact_identifiers (channel × identifier → person_id)
              │
              ├── message_participant ── message ── message_thread
              │                             │
              │                             └── message_attachment
              │
              ├── calendar_event_participant ── calendar_event
              ├── call_log
              ├── crm_tasks, crm_notes, crm_conversations, crm_messages
              ├── agent_runs           (person_id FK for telegram/chat triggers)
              ├── chat_sessions        (person_id FK stamped at first sight)
              ├── channel_message_map  (person_id FK stamped by channel_bus)
              ├── memory_facts         (person_id FK for unambiguous entities)
              │
              └── timeline_activity    (denormalized feed; indexed read path)
```

`connected_account` sits to the side — each tenant's connected
mailbox / phone number / bot is one row, and every `message` /
`calendar_event` / `call_log` FK's to it so the origin account is always
known.

## Tables

### Identity (pre-existing)
| Table | Purpose |
|---|---|
| `crm_people` | Canonical person record, one UUID per human |
| `crm_companies` | Organizations |
| `contact_identifiers` | `(channel, identifier) → person_id` resolver store |

### Messaging (new, migration 047)
| Table | Purpose |
|---|---|
| `connected_account` | Tenant mailbox / phone / bot — FK target for messages |
| `message_thread` | Conversation thread, unique on `(tenant, channel, external_thread_id)` |
| `message` | One message per row, unique on `(tenant, channel, external_message_id)` |
| `message_participant` | Junction `(message × role × person)` — supports to/cc/bcc/from |
| `message_attachment` | Files attached to a message |

### Voice + calendar (new, migration 048)
| Table | Purpose |
|---|---|
| `call_log` | Voice call (Twilio) — direct `person_id` FK (calls are 1:1) |
| `calendar_event` | Shadow of Google Calendar events |
| `calendar_event_participant` | Junction `(event × role × person)` |

### Existing tables with new FK (migration 046)
| Table | Added |
|---|---|
| `chat_sessions.person_id` | Stamped on first inbound; indexed `(tenant, person, last_active)` |
| `agent_runs.person_id` | Stamped from `trigger_detail` for telegram/chat triggers |
| `channel_message_map.person_id` | Stamped by `channel_bus` write-through |
| `memory_facts.person_id` | Stamped when a single-entity fact unambiguously maps |

### Unified feed (new, migration 049)
`timeline_activity` — the denormalized append-only index. One row per
touch with `(tenant, person, occurred_at, activity_type, source_table,
source_id, channel, direction, title, snippet, agent_id, metadata)`.
Unique on `(tenant, source_table, source_id)` — the same source row can
only produce one feed entry.

## Write-through hooks

Each channel's entry point emits a `timeline_activity` row at the moment
of I/O. No batch reconciler, no nightly sync — the feed is current because
the channel path is current.

| Channel | File | Function |
|---|---|---|
| Telegram in/out | `robothor/engine/channel_bus.py` | `record_inbound`, `record_outbound` |
| Agent runs | `robothor/engine/runner.py` + `robothor/engine/run_person_link.py` | `resolve_run_person_id`, `emit_run_timeline_activity` |
| Email send/reply | `robothor/engine/tools/handlers/gws.py` | `_record_sent_email` |
| Calendar create | `robothor/engine/tools/handlers/gws.py` | `_record_calendar_event` |
| Notes / tasks | `robothor/crm/dal.py` | inline in `create_note` / `create_task` |

All hooks are best-effort: a CRM-side hiccup must not break the primary
channel flow. Errors are logged and swallowed.

## Resolver

`robothor/crm/dal.py:resolve_contact(channel, identifier, name, tenant_id)`
is the one canonical function that maps a channel identifier to a
`person_id`. Owner-priority, tenant-aware. Every write-through goes
through this — no ad-hoc contact matching anywhere.

## Read path — DAL

```python
from robothor.crm.dal import (
    get_person_timeline,      # merged feed, one indexed scan
    get_person_summary,       # counts per activity_type + last touch
    get_person_messages,      # full bodies via participant junction
    get_person_calls,
    get_person_events,
    get_person_tasks,
    get_person_notes,
    get_person_runs,
    get_person_memory,
    get_contact_360,          # holistic one-call view
)
```

## Read path — Bridge API

All at `http://localhost:9100`:

```
GET /api/people/{id}
GET /api/people/{id}/timeline?limit=&channels=email&channels=sms
GET /api/people/{id}/summary
GET /api/people/{id}/messages?channel=email
GET /api/people/{id}/threads
GET /api/people/{id}/calls
GET /api/people/{id}/events
GET /api/people/{id}/tasks
GET /api/people/{id}/notes
GET /api/people/{id}/runs
GET /api/people/{id}/memory
GET /api/people/{id}/contact-360
```

## Agent tools

Delivery agents can call these directly:

- `get_contact_360(id=..., timeline_limit=50)` — one-call holistic view
- `get_contact_360(identifier="jane@example.com", channel="email")` —
  resolve first, then fetch
- `list_contact_messages(id=..., channel=?, limit=?)` — full bodies

## Testing

**Every change is TDD.** Per-channel write-through tests live in
`robothor/engine/tests/`. DAL and schema tests live in `tests/`. Mark
integration tests `@pytest.mark.integration`.

```bash
# Integration (real DB)
ROBOTHOR_TEST_DB_DSN="dbname=robothor_test user=$USER host=/var/run/postgresql" \
  venv/bin/python -m pytest tests/ robothor/engine/tests/ -m integration

# Unit (mocked DB)
venv/bin/python -m pytest tests/ robothor/engine/tests/ -m "not integration"
```

The `mock_get_connection` fixture in `tests/conftest_integration.py`
patches `get_connection` across every module and swallows `commit()` so
per-test rollback stays intact.

## Migration files

| File | Adds |
|---|---|
| `crm/migrations/046_person_linkage.sql` | person_id FKs on chat_sessions, agent_runs, channel_message_map, memory_facts + backfills |
| `crm/migrations/046b_orphan_cleanup.sql` | NULLs stale contact_identifiers.person_id pointers (one-time) |
| `crm/migrations/047_messaging_kernel.sql` | connected_account, message_thread, message, message_participant, message_attachment |
| `crm/migrations/048_voice_calendar.sql` | call_log, calendar_event, calendar_event_participant |
| `crm/migrations/049_timeline_activity.sql` | timeline_activity + initial backfill |

## Open work

- Twilio voice + SMS write-through (tables exist; hooks not yet wired)
- Memory facts extraction write-through (schema supports it; extraction
  path not yet updated)
- Helm Contact 360 detail page (API is ready; frontend not yet built)
- Consistency metric in health dashboard (orphan-row detection)

## Production rollout

Apply migrations to `robothor_memory` in order: 046, 046b, 047, 048, 049.
Each is additive — no service restart required. Backfills filter to
`deleted_at IS NULL` people, so they're safe against historical drift.
