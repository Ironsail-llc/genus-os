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

## Do-not-contact

`crm_people.do_not_contact` (migration 113, BOOLEAN NOT NULL DEFAULT FALSE
with a partial index on the TRUE rows) is the outreach opt-out. Set it when
someone asks to be removed; clear it if they come back.

**Where it is enforced:** `robothor/engine/tools/handlers/gws.py::_dnc_refusal`,
called at the head of all three branches of `_handle_gws_tool` that put a
message in someone's inbox:

| Tool | Addresses checked |
|---|---|
| `gws_gmail_send` | its `to` and `cc` |
| `gws_gmail_reply` | the reply-all set derived from the thread — the branch that can address someone the agent never typed |
| `gws_calendar_create` | its `attendees`; Google emails an invitation to each on insert and again on every edit |

Nothing reaches the `gws` CLI until the check passes — for a calendar event
that includes the dedup read. The lookup is
`robothor/crm/dal.py::do_not_contact_emails`, which matches an address three
ways — `crm_people.email`, the `additional_emails` JSONB list, and
`contact_identifiers` rows on the `email` channel — so an opt-out cannot be
sidestepped by using a person's secondary address.

**Scope: email and calendar invitations only.** Telegram, SMS and chat are
NOT enforced. A person flagged `do_not_contact` can still be reached on those
channels, because the guard lives in the gws handler and matches on email
addresses; extending it means resolving a person from a chat id or phone
number at each of those senders. Anyone reading this row as "we will not
contact them" is reading more than the control delivers.

**What a block looks like:** the tool returns an error naming the address,
and a row lands in `agent_guardrail_events` with
`guardrail_name='do_not_contact'`, `action='blocked'`, `mode='enforce'` and
the tool name. That row is the evidence the control is live; the guardrail
health surface reads it. A send made outside an agent run still refuses, but
files nothing (`run_id` is NOT NULL and references `agent_runs`).

**Edges, all deliberate:**

- A recipient absent from the CRM is **allowed**. This is an opt-out list,
  not an allow-list.
- A call carrying no tenant **refuses**. The list is per-tenant, and reading
  `default`'s list on a call that could not say whose it is would clear a
  recipient who is flagged in the tenant the send actually belongs to.
- A lookup that fails **refuses** the send, after one bounded retry (0.5s,
  on a connection-level `OperationalError` only — a query error gets the
  same answer the second time). "We could not read the opt-out list" is not
  "nobody opted out". No `agent_guardrail_events` row is written on this
  branch: that write goes to the database that just failed, so it only
  produces a second traceback. The ERROR log line is its evidence.
- The one exception is a missing `do_not_contact` column, which means the
  deploy beat `robothor migrate`. Nobody can have been flagged before 113
  applies, so the send proceeds and the skipped check is logged at ERROR.
  The carve-out is matched on the column NAME, not on the exception class —
  the same SQL reads `deleted_at`, `tenant_id`, `additional_emails` and
  `contact_identifiers`, and any of those going missing is an unreadable
  list, not a pending migration.

**The lever — `ROBOTHOR_DNC_MODE`:** `enforce` (default) or `observe`, read
from the environment at call time. In `observe` every refusal above becomes
a WARNING plus an `agent_guardrail_events` row with action `observed` and
mode `observe`, and the message goes out. It is documented in
`infra/systemd/robothor.env.example`. **`observe` disables a compliance
control** — people who asked not to be contacted will be contacted. It exists
because a fail-closed default with no lever is one nobody can respond to: the
alternative an operator reaches for at 3am is commenting out the call, and a
guard that is watching and logging is strictly better than a guard that has
been deleted. Anything unrecognised enforces, so a typo cannot switch the
control off.

**Setting it:** `PATCH /api/people/{id}` with `{"doNotContact": true}`, the
`update_person` tool (`doNotContact`) on the MCP and engine surfaces, or
`dal.update_person(person_id, do_not_contact=True)`. Reads come back as
`doNotContact` on the person shape. Every write is audit-logged like any
other person edit, and the post-condition checker re-reads the row, so an
opt-out that reported success without taking is caught rather than believed.

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
| `crm/migrations/113_add_do_not_contact.sql` | `crm_people.do_not_contact` + partial index on the opted-out rows |

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
