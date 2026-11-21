# Shared threads: reports, agents and tasks as one workspace

Status: approved by the operator, 2026-11-21. This file is the design and the build
contract. Lanes built in parallel code against the **Contract** section; change it there
first, then in code.

## Intent

Genus should be the one place staff work. A **thread** is a company-owned
sub-conversation that sits below each user's main conversation. It has its own history,
a saved dashboard, linked tasks, an agent, an optional schedule, simple settings, and the
users who can access it. Three kinds ship together:

- **Report**: a scheduled run publishes an *edition* (charts, metrics, tables, commentary,
  suggested tasks) for a period. People with access are notified, open the thread, pick a
  period, and continue with the agent.
- **Agent**: extra threads with any agent.
- **Task**: any CRM task opened as a thread.

The main agent creates, edits, shares and runs threads for any staff member in
conversation, and lists and reads the caller's threads, answering with links.

## Goals, non-goals, anti-goals

- Goals: one foundation for all three kinds; visuals that persist; per-thread access;
  definitions nobody can overwrite by accident; every figure traceable; a preview run.
- Non-goals: sharing across companies; saved query adapters; plugin-contributed pages.
- Anti-goals: a new role system; a feature flag or dormant code path; any way for a
  thread to expose data to a user outside its company.

## Decisions

| Topic | Decision |
|---|---|
| Name | Code says `room`; the UI says "Workspaces". The engine already uses "thread" for planner tasks tagged `thread`, and `robothor/workspace/` is the mail and calendar layer. |
| Main conversation | Each user has their own main conversation; workspaces sit below it. |
| Ownership | A room belongs to the tenant. Its creator is *assigned*. |
| Access | `assigned` users manage the definition. `shared` users read, chat, pick a period, run now, accept and work tasks. Tenant owner/admin manage every room. Tenant viewers read only. No per-room role. |
| Roles | Existing roles, seeded in `role_permissions`, tunable per tenant. |
| Overwrite protection | Every definition change writes a numbered revision; restore writes a new one. Shared users get "duplicate". |
| Preview run | Builds an edition for the requester only: no notifications, no tasks, no mirror. A permanent feature. |
| Numbers | The agent fetches with the tenant's tools. Provenance is built by the server from the recorded run. |
| Periods | Each edition has an explicit period. A week is its own edition, not a sum of days. |
| Run-as | A scheduled run uses an assigned user's permissions, re-resolved at fire time. Shown in the UI. |
| Member approval | A member may decide an action only when their own interactive run in a room proposed it and the linked task is assigned to them. |
| Concurrency | One agent turn per room at a time; a second sender is told and queues. |

## Approaches considered

1. A standalone reports feature with its own storage. Agent and task threads would need a
   second system.
2. Title and member columns on `chat_sessions`. Sessions are deleted after seven idle days,
   are keyed by convention, and have no home for editions or tasks.
3. **Chosen:** a rooms layer over an ordinary chat session. The room owns identity, access,
   editions, tasks and schedule; the conversation is a normal session, so history,
   streaming and saving are reused.

## Contract

### Session key

`agent:{agent_id}:room:{room_id}`. Segment 1 stays the agent because chat routing parses
it. `robothor.rooms.access.parse_room_key(key) -> tuple[str, str] | None` is the only
parser; `room_session_key(agent_id, room_id) -> str` the only writer.

### Tables

All carry `tenant_id TEXT NOT NULL`, composite primary keys starting with it, forced
row-level security with the `tenant_isolation` policy, and `created_at`/`updated_at`.

```
rooms(tenant_id, id UUID, kind 'report'|'agent'|'task', title, agent_id, session_key UNIQUE,
      created_by UUID, run_as_user_id UUID, instructions TEXT, settings JSONB,
      task_id UUID NULL, paused BOOL, consecutive_failures INT, rev INT,
      last_activity_at, last_message_id BIGINT, archived_at,
      PK(tenant_id,id), UNIQUE(tenant_id,task_id))
room_members(tenant_id, room_id, user_id UUID, assigned BOOL, added_by UUID,
      last_read_message_id BIGINT, last_read_published_seq INT, notify JSONB,
      PK(tenant_id,room_id,user_id))
room_revisions(tenant_id, room_id, rev INT, changed_by UUID, changed_at, definition JSONB,
      PK(tenant_id,room_id,rev))
room_editions(tenant_id, id UUID, room_id, seq INT, kind 'edition'|'dashboard',
      status 'building'|'published'|'failed', preview BOOL, period_start, period_end,
      grain 'day'|'week'|'month'|'custom'|NULL, revision INT, published_seq INT NULL, title, summary,
      blocks JSONB, suggestions JSONB, provenance JSONB, error TEXT, run_id TEXT,
      chat_message_id BIGINT, requested_by UUID, PK(tenant_id,id), UNIQUE(tenant_id,room_id,seq))
  + UNIQUE(tenant_id,room_id,grain,period_start,period_end) WHERE status='building' AND NOT preview
room_tasks(tenant_id, room_id, task_id UUID, edition_id UUID NULL,
      origin 'subject'|'suggestion'|'linked', suggestion_key TEXT, accepted_by UUID,
      dismissed_by UUID, PK(tenant_id,room_id,task_id))
user_cronjobs + room_id UUID, run_as_user_id UUID
operation_actions + proposed_by_run_id TEXT, proposed_for_user_id UUID, room_id UUID
```

`settings` keys: `tz` (IANA name), `default_grain`, `schedule_text`, `notify_default`.
Nothing in a workspace is capped (operator decision, 2026-11-21): no cost cap, no
on-demand limit, no room-specific schedule floor, no longest custom period. Cost is
shown on each edition's provenance; repeated failures pause a workspace and say why; a
run that dies is recovered. The *definition* stored in a revision is
`{title, instructions, agent_id, settings, schedule}`.

Role seed (`__default__`): `member` and `user` allow `room_*`, deny `room_admin_*`;
`viewer` allow `room_list`, `room_read`; `owner`, `admin` already allow `*`.

### `robothor/rooms/` (sync functions; async callers use `asyncio.to_thread`)

```
access.py
  Actor(user_id, role, tenant_id, is_service=False)
  Access = 'none' | 'viewer' | 'shared' | 'assigned' | 'admin'
  room_access(actor, room, member_row) -> Access           # pure, table-tested
  can(access, action, *, archived=False) -> bool            # archived: read, archive, duplicate, list_all only
  require(actor, room_id, action) -> Room                   # raises RoomNotFound | RoomForbidden
  resolve_room_key(auth, session_key, *, action='read') -> str | None   # None when not a room key
ACTIONS: read, post, run, request_period, accept_task, edit_definition, share,
         archive, restore_revision, duplicate, reassign, list_all
  viewer:   read
  shared:   read, post, run, request_period, accept_task, duplicate
  assigned: shared + edit_definition, share, archive, restore_revision
  admin:    assigned + reassign, list_all
  A tenant role of viewer caps any membership at 'viewer'.
  A service identity, another tenant, or any other role gets 'none'.
dal.py
  create_room(...) -> Room            get_room(tenant_id, room_id) -> Room | None
  list_rooms_for_user(tenant_id, user_id, *, include_archived=False) -> list[RoomSummary]   # with unread
  list_all_rooms(tenant_id) -> list[RoomSummary]
  update_definition(tenant_id, room_id, changes, changed_by, expected_rev) -> Room   # raises RevisionConflict
  list_revisions / restore_revision / duplicate_room / archive_room / unarchive_room
  add_member / remove_member / set_assigned / list_members / mark_read / set_notify
  create_edition / publish_edition / fail_edition / get_edition / list_editions / find_edition_for_period
  link_task / accept_task / dismiss_task / list_room_tasks / get_or_create_task_room
periods.py
  Period(start, end, grain)
  last_complete_period(grain, tz, now) -> Period
  resolve_period(grain, tz, anchor) -> Period
fanout.py
  room_lock(room_id) -> asyncio.Lock
  fan_out_edition(room, edition) -> list[receipt]            # async
```

Errors: `RoomNotFound`, `RoomForbidden`, `RevisionConflict`, `RoomBusy`, `InvalidBlocks`.

#### As built (lane 1)

Exact signatures are in `robothor/rooms/dal.py` and `access.py`. Points later lanes
rely on:

- `create_edition(...) -> tuple[Edition, bool]`: `created` is False when a non-preview
  request named a grain and period that is already building; callers start a run only
  when it is True. `period_start`/`period_end` are `DATE`.
- `get_edition` and `list_editions` take a required keyword `viewer_id`: a preview is
  returned only to its requester; `viewer_id=None` sees no previews.
- `accept_task` / `dismiss_task` return None when the link is missing or already
  decided (a task is decided once). `publish_edition` / `fail_edition` return None
  unless the edition is `building`.
- `update_definition` accepts `title`, `instructions`, `settings` (merged; `None` removes
  a key) and `schedule` (stored as `settings.schedule_text`). `agent_id` is fixed at
  creation (it is part of the session key); duplicate the room to change agent.
- `get_member(tenant_id, room_id, user_id)` exists for `require`. The session-key
  helpers live in `robothor/rooms/keys.py` and are re-exported from `access`.
- `seq` is build order (allocated when the row is created, previews included) and is
  the stable edition number for links. `published_seq` is publish order: set when a
  non-preview edition publishes, never on a preview, unique per room. Read marks and
  unread use `published_seq` only, so a preview never moves a mark and editions that
  publish out of build order are each unread once. The member column is
  `last_read_published_seq`, set by `mark_read(..., published_seq=)` (renamed from
  `last_read_edition_seq` / `edition_seq`); `RoomSummary.latest_edition` is the
  highest `published_seq`, shaped `{id, seq, published_seq, title, period_start,
  period_end}`: pass its `published_seq` to `mark_read` to mark it read (camelCase
  `publishedSeq` on the bridge).
- Unread = messages after the member's message mark + editions whose `published_seq`
  is after their edition mark and that carry no `chat_message_id`.
- Every room table's `tenant_isolation` policy has the platform shape: a connection
  bound to a tenant sees only that tenant's rows, an unbound one (`app.tenant_id`
  unset or empty) sees all. The operator moves and idle cleanup rely on it.
- Child tables reference `rooms(id)` by a single-column key (`rooms.id` is also
  unique): `move-workload` re-tenants rows in place and refuses composite keys.
  A workload move carries an agent's rooms, members, revisions and editions; a room
  with `room_tasks` rows refuses the move. An account move does not carry room
  memberships (rooms belong to the tenant); it removes them.

#### As built (lane 3)

`robothor/rooms/definition.py` holds every definition rule; the `room_*` tools
and the bridge routes are wrappers over it. Besides the two contract functions
(`create_room_with_schedule`, `apply_definition_change`) it exports `list_rooms(actor, *, scope)`, `read_room(actor, room_id, *,
editions, messages)`, `share_room`, `unshare_room`, `archive_room`,
`duplicate_room`, `restore_room_revision`, `reassign_room`, `resolve_user`,
`run_as_actor(tenant_id, room_id)` and `room_schedule(tenant_id, room_id)`.

- A schedule must repeat: a one-off (`2h`, an ISO time) is refused. There is no
  workspace minimum interval (operator decision: nothing in workspaces is capped);
  a schedule may be as frequent as `cron_parse.parse_schedule` allows any user
  cron. `max_cost_usd` is not a definition change the tools or
  `apply_definition_change` accept. A room's
  cron schedule is read in `settings.tz`, UTC when unset; the zone is stored in the
  row's payload. A plain user cron (no `tz` in its payload) keeps the host's zone.
- One `user_cronjobs` row per room: a partial unique index on
  `(tenant_id, room_id) WHERE room_id IS NOT NULL` (migration 170), written by a
  single `INSERT ... ON CONFLICT`. The row follows changes of schedule, time zone,
  pause and instructions (its `prompt`).
- Pausing is not part of the definition: it writes no revision, but still checks
  `expected_rev`.
- A duplicate of a scheduled room starts `paused` (A8: unscheduled until its new
  owner confirms); resuming it creates its schedule row under the new owner.
- `share_room(..., assigned=None) -> ShareOutcome(member, added)`: for an existing
  member an omitted `assigned` keeps their role, `True` promotes, and an explicit
  `False` demotes under unshare's guards; `added` is False for an existing member,
  so callers send no new notice.
- `unshare` refuses the run-as user and the last assigned member; `reassign`
  makes the target assigned and the run-as user. A viewer cannot be assigned or
  reassigned to.
- An unverified identity is refused by every room tool. A run with no person
  behind it may only `room_read`, and only for the room `robothor.rooms.run_context`
  binds the run to (same tenant), with that room's run-as user's access re-resolved
  from `user_accounts`. No carrier module, or no bound room, reads nothing.
- `user_cron.list_due_cronjobs` skips rows with a `room_id`: the generic tick
  must not run a room's instructions as a plain cron prompt. Lane 7 fires them.
- `room_read` takes message authors from `message.author_user_id` (or
  `authorUserId`) on the stored chat message.
- The tools are an opt-in family always given to the main agent; see
  `docs/TOOLS.md`.
#### As built (lane 8, periods)

- `periods.py` also holds the request: `async request_period(actor, room_id, grain,
  anchor=None, *, now=None, run_room=None) -> (Edition, started)`. It is async because
  the winner starts a background run; its data access runs in a thread.
- Periods are whole local days in the room's `settings.tz` (UTC when unset), end
  inclusive; weeks run Monday to Sunday; custom ranges may be any length. Only a
  period whose last day is before today (local) can be requested. `Period.bounds(tz)`
  gives the UTC instants of its local midnights.
- `anchor`: a date (`YYYY-MM-DD`; `YYYY-MM` for a month), `start/end` (or a pair or
  `{start, end}`) for custom, or None/`latest` for `last_complete_period`.
- The run is `robothor.rooms.runs.run_room(room, edition, *, period, context)`
  (lane 7), resolved before any row is written; when it is missing the request fails
  `RoomRunUnavailable` (503). A run that raises fails its edition.
- No limits (operator decision): no on-demand cap, no `max_on_demand_per_day`
  setting (removed from `SETTINGS_KEYS`), no maximum custom range. A period must
  still have ended, and one run still serves each period.
- Recovery (`robothor/rooms/recovery.py`): `reap_stale_editions(tenant_id, *,
  room_id=None, booted_at=None, now=None) -> list[Edition]` fails each `building`
  edition whose run is not alive: not held by this process (`hold(edition_id)` /
  `live(edition_id)`), and either its `run_id`'s `agent_runs` row has ended, or (no
  run row) it predates `booted_at` (default `ROBOTHOR_DAEMON_START_TS`) or has been
  building for 2 hours. `request_period` reaps the room first; call it at engine
  start after `_cleanup_stale_runs`. A cancelled period run fails its edition; runs are
  spawned on the engine's task registry, so the shutdown drain waits for them; the
  context is read before the row is inserted; a caller cancelled mid-claim still gets
  its run started.
  Errors: `PeriodError` (a `ValueError`, 422).
- `context_for_period(tenant_id, room_id, period)`: the newest published summary of
  each `day` edition inside the period, 500 characters each, 6,000 in all, wrapped
  as data.
- `POST /api/rooms/{id}/periods` (engine) body `{grain, anchor?, requested_by?}`. A
  human token (`engine:write`) is its own identity (`requested_by` ignored). A service
  token needs `engine:control` on this route and `/run` (`engine/auth.py`), which the
  bridge's engine credential carries, and must be the bridge's own identity
  (`genus-bridge`, else 403). It must name `requested_by`; the engine reads that account's role from `user_accounts`
  (never the body), requires it active and living in the token's tenant (else 404),
  and re-checks `request_period`. 200 existing, 202 started, 400 no `requested_by`,
  403, 404, 422 (`reason`: one of `PERIOD_ERROR_REASONS`), 503 no room
  runner, 500 `internal_error`. Every response message is the route's own fixed text,
  never an exception's.
- Added: `dal.list_editions_in_period`,
  `dal.list_building_editions`,
  `models.edition_json` (the camelCase `Edition`, with `publishedSeq`). Engine route module
  `robothor/engine/rooms_api.py`; tool `room_request_period`.

### Edition blocks

`blocks` is a list of `{"component": <name>, "props": {...}}`. Allowed components:
`render_metric_grid`, `render_metric_card`, `render_bar_chart`, `render_line_chart`,
`render_pie_chart`, `render_data_table`, `render_markdown`, `render_timeline`. A test keeps
this list a subset of the dashboard's component registry. `suggestions` is a list of
`{"key", "title", "detail", "action_hint"}`. `provenance` is
`{"run_id", "ran_as", "started_at", "tool_calls": [{"tool", "at", "ok"}]}`.

#### As built (lane 4)

- `robothor/rooms/editions.py` holds validation (`validate_blocks`,
  `validate_suggestions`, built on `models.ALLOWED_COMPONENTS`; one shape check per
  component in `COMPONENT_CHECKS`), plain-text cleaning (`clean_summary`,
  `clean_title`), provenance and `publish_from_run`, the whole tool, sync.
  `handlers/room_editions.py` runs it in one worker thread.
- **Run carrier.** `robothor/rooms/run_context.py`: `RoomRun(tenant_id, room_id,
  session_key, preview=False, requested_by=None, edition_id=None, grain=None,
  period_start=None, period_end=None)`, bound with `room_run_scope(run)` around
  `runner.execute` AND the save of the turn's reply, read with `current_room_run()`. A `ContextVar`, so task-local.
  Chat on a room session binds `RoomRun.for_session(tenant_id, session_key)` (None
  for any other key, which binds nothing). A scheduled or on-demand run binds the
  same plus `preview`, `requested_by` (the preview's requester), `edition_id` (the
  `building` row it created) and that row's period, and passes the run-as user as
  `user_id`/`user_role` to `runner.execute`. Tools never take a room, preview or
  provenance from arguments. The publish is refused unless the carrier names the
  same room and tenant, the room's session key matches, `ctx.agent_id` is the
  room's agent, the caller holds `post`, and the run has an `agent_runs` row that is
  not a sub-agent run (`parent_run_id` set or `nesting_depth` above 0). A sub-agent
  runs inline and inherits the carrier, even one with the room's own agent id, so
  the run record is what refuses it; a run with no row cannot have provenance.
- **Rows.** With a carrier `edition_id`, that `building` row is completed (a
  different `period` argument is refused). Otherwise `create_edition` either
  returns the period's existing `building` row or allocates a new seq. A dashboard
  is always a new row with no period.
- **`revision`** is the edition's revision within its period, set at publish: 1, or
  one more than the non-preview editions already published for that grain and
  period; 1 for an edition with no period. While building, the row carries the
  room's definition revision, which provenance records as `room_rev`.
- **`provenance`** = `{run_id, ran_as, started_at, room_rev, tool_calls: [{tool, at,
  ok}]}` (plus `tool_calls_truncated` past 200 calls), from `agent_runs` and the
  run's `tool_call` steps in `agent_run_steps` (flushed after every iteration). A
  `tool_call` wrapper is listed as the tool it ran; the publish call itself is left
  out.
- **After a non-preview publish:** `room.edition` on stream `agent` with payload
  exactly `{room_id, tenant_id, edition_id}`; every hook registered with
  `editions.register_edition_published(hook)` runs as `hook(room, edition)` (sync,
  in the worker thread, exceptions logged); and the edition is noted on the turn's
  `RoomRun`, so the assistant message `chat_store.save_exchange` /
  `save_message(role="assistant")` (or their async wrappers, which carry the
  caller's context into the worker) saves inside that scope for that tenant and
  session carries `edition_id` (the last one published in the turn) and every
  noted edition gets that `chat_message_id`. Notes live and die with the carrier:
  a turn that never saves leaves nothing for a later turn, and a failed save puts
  them back for the retry. A preview does none of these.
- Summary at most 4000 characters, title one line of at most 200; suggestion text
  is cleaned the same way. These fields are plain text, so cleaning is structural,
  not a list of known tags: hidden characters (control, format, surrogate,
  private-use, escape sequences) are removed, HTML entities decoded and NFKC
  look-alikes folded, together until nothing changes; then every `<`, `>`, `[` and
  `]` is replaced (`‹`, `›`, `(`, `)`), so no tag, prompt boundary or dashboard
  marker can be written in them in any spelling. Cleaning is idempotent. A
  generated corpus of obfuscations is tested against the downstream detectors.
- This text is not wrapped as data when it is read back: a tool result that
  carries it is not in `EXTERNAL_DATA_TOOLS` (`robothor/engine/session.py`), so the
  structural cleaning is what keeps it inert. Tools that return edition text to a
  model (`room_read`, `room_list`) should join that set.

#### As built (lane 7)

`robothor/rooms/runs.py` is the one path that runs a room's agent outside
interactive chat. Its entry points (the contract's single `run_room` split in
two, so the periods lane calls the shape it needs):

- `execute_room_run(tenant_id, room_id, *, trigger: 'schedule'|'manual'|'period',
  requested_by=None, preview=False, period=None) -> Edition | None` (async):
  creates the period's `building` edition (`period` a `Period(start, end, grain)`,
  a `(start, end, grain)` tuple or `{grain, start, end}`; default the last
  complete period of `default_grain`) and runs. None when nothing ran: no such
  room in the tenant, archived, paused (schedule), the period already building,
  or a run-as user who can no longer run it.
- `run_room(room, edition, *, period, context) -> Edition | None` (async): the
  periods lane's entry point for the `building` edition it created; runs as the
  run-as user, tells `edition.requested_by` only, raises `RunRefused` when the
  room cannot run.
- `start_room_run(actor, room_id, *, preview=False, period=None) ->
  tuple[Edition, bool]` (async): "run now" for a person, `(edition, joined)`,
  the run in the background (`pending_runs()`). Needs `run`; a paused room only
  for assigned or admin; refuses non-report rooms (`ValueError`). A named
  period goes through `periods.resolve_period` (over, a whole day/week/month;
  `PeriodError`). Joins only a run for the same period and preview-ness (for a
  preview, the same requester); a run for another period waits for the lock and
  builds its own edition, returned `building` (202). Raises
  `RoomNotFound`, `RoomForbidden`, `RunRefused(reason='paused'|'archived')`.
  No on-demand cap (operator decision, no caps in workspaces): any number of
  runs may start.
- `Period` and `last_complete_period` are `robothor.rooms.periods`'.
- Liveness (`robothor.rooms.recovery`): a run holds its edition live
  (`recovery.hold`) from the moment it is scheduled until it ends, records the
  `agent_runs` id on the edition at the run's first status event
  (`dal.set_edition_run`, added), and reaps the room's dead `building` rows
  before creating one. The engine reaps every dead one at start, after
  `_cleanup_stale_runs` (`daemon._reap_room_editions`). Background runs start
  through the engine's task registry (`runs.spawn`), so the shutdown drain
  waits for them.

Run rules: the room's turn lock (`fanout.room_lock`) is awaited, never the
per-agent dedup lock; the room and the run-as user (`definition.run_as_actor`
plus `post` access) are re-read inside the lock; the assembled prompt is
re-scanned; the carrier (`RoomRun.for_session(..., preview, requested_by,
edition_id, period)`) wraps `runner.execute` and the save of the reply; the run
goes through the normal runner (run tracking on), `trigger_type` CRON for a
schedule and MANUAL otherwise, `trigger_detail=f"room:{id}"`, with no
`agent_config`: the runner loads the agent's own manifest, so a workspace adds
no budget (no cost cap). What the run cost is a flag: the publish writes the
run row's `total_cost_usd`, `input_tokens` and `output_tokens` into provenance
(`cost_usd`, `input_tokens`, `output_tokens`; `editions.run_cost`), and when the
run ends its final totals are merged in (`dal.record_edition_cost`, added). The
edition ends `published` or `failed` on every exit (cancel, exception, timeout,
no publish). A non-preview failure increments `consecutive_failures`
(`dal.record_run_result`, added), tells assigned members (inbox
`agent_error`, `metadata.room_id`), and at `AUTO_PAUSE_AFTER_FAILURES` (3) in a
row pauses the room (`definition.sync_room_schedule`, added) and says why; a
success resets it. A run-as user who is inactive or cannot post pauses the room
and tells the tenant's owners and admins. A non-preview run saves its exchange
in the room (user turn `room_trigger`, reply `author_agent_id`); a preview saves
nothing in the shared conversation.

`fanout.fan_out_edition(room, edition, *, recipients=None, emit_event=True) ->
list[SendReceipt]` (async): never for a preview. Recipients: members with an
active account who have not muted (`notify.muted`, `notify.enabled: false`, or
`settings.notify_default: false` without an opt-in), or the named ones who have
access. Each gets a webchat note in their main conversation (`[title](/?v=chat&room=<id>)`,
summary cut to 280 characters; inbox row type `info` to the user id, `metadata`
`{room_id, edition_id, ...}` without the session key; `WebchatChannel.send` gained
`tenant_id` and `metadata`), plus their paired `notify.channel`. A run's publish
already emitted `room.edition`, so runs pass `emit_event=False`.
`fan_out_note(room, text, *, metadata, recipients=None)` is the shared sender.

The scheduler's tick (`robothor/engine/room_schedule.py`, called from `_tick_user_cronjobs`) reads `user_cron.list_due_room_cronjobs`
for `config.tenant_id` (one engine serves one tenant), claims each row with
`user_cron.claim_room_cronjob(job_id, due_at=, next_run_at=)` (an update that
holds only if `next_run_at` is unchanged: one winner across ticks and replicas)
and fires `execute_room_run(..., trigger='schedule')`.

`POST /api/rooms/{id}/run` (engine, `robothor/engine/rooms_api.py`): body
`{preview?, period?, requested_by?}`; caller rules as the periods route (human
token is its own identity; the service token names `requested_by`, whose role is
read from `user_accounts`; only the bridge's service identity may name one, and
a service needs `engine:control`). 202 started, 200 joined, 400
`requested_by_required`, 403 `forbidden`, 404 `room_not_found`, 409 `paused` /
`archived`, 422 `invalid_run`, 500 `internal_error` (no 429: nothing is capped); every message is the route's own fixed text, never an
exception's. The body is `edition_json`.
`room_run(room_id, preview?, period?)` is the same for the main agent (in
`ROOM_TOOLS`, excluded from benchmarks) and returns `{edition, started, joined, link}`.

The `room` delivery channel (`channels/room.py`, built in): `delivery.to` is the
room id; the output is an assistant turn attributed to the agent, then a note to
members. Failures: `failed:room_unknown_target`, `failed:room_archived`,
`failed:room_no_session_write`.

No run starts runs: a workspace run's carrier is `RoomRun(..., unattended=True)`
(added); inside it `room_run` and `room_request_period` refuse
(`handlers.room_runs.nested_refusal`, also for a sub-agent run, read from its
`agent_runs` row), and every room tool but `room_read` refuses
(`handlers.rooms._actor` returns None), so a run cannot create scheduled
workspaces either. `max_cost_usd` is no longer a room setting (refused as
unknown). Stopping a run: a run now, period run or preview by its requester or
an assigned member/admin; a scheduled run by an assigned member/admin.

Interactive chat binds `chat_rooms.room_scope(target)` in `chat_rooms.start_turn`,
so every room turn's task (runner and save) carries the room; the interruption
path (`chat_delivery.deliver_interruption`) finds the room from the session key
and saves the turn with its author and moves its activity mark; `/chat/abort` stops a running room run, a scheduled one only for assigned
or admin. `chat_store.search_chat_turns(..., room_ids=None)` leaves out every
room session not in `room_ids`. The bridge's notification read and ack accept
only the recipient or an operator. No schema change.

#### As built (lane 9)

`robothor/rooms/tasks.py` holds the task rules and `robothor/rooms/approvals.py`
the member approval rule; the tools (`handlers/room_tasks.py`) and the bridge are
wrappers. Schema changes are migration 172.

- **Functions.** `list_tasks(actor, room_id) -> list[RoomTaskView]`,
  `accept_task(actor, room_id, task_id) -> RoomTaskView`, `dismiss_task(...)`,
  `open_task_room(actor, task_id) -> Room`, `on_edition_published(room, edition)`
  (registered on the publish hook when the handler module loads) and
  `publish_task_changed(tenant_id, task_id)`. `RoomTaskView(task_id, title, status,
  origin, edition_id, assignee: TaskPerson | None, accepted_by: TaskPerson | None,
  dismissed)`, `TaskPerson(user_id, display_name)`. Errors carry `code`:
  `TaskNotFound` and `RoomNotFound` `not_found`, `RoomForbidden` `forbidden`,
  `TaskAlreadyAccepted` `already_accepted` (with `.by`, who has it),
  `TaskAlreadyDismissed` `already_dismissed`.
- **Assignee.** Accept writes `crm_tasks.assignee_id` (a `user_accounts.id`;
  nothing wrote it before, the bridge task view already reads it as
  `assigneeId`) and `room_tasks.accepted_by`. A task whose assignee is someone
  else is refused naming them; the same person accepting again is a no-op.
  A task workspace's own `subject` task is never accepted from inside it
  (`RoomForbidden`), so opening a task never makes the opener its assignee.
- **Dismiss** applies to `origin='suggestion'` only; it also resolves the CRM
  task (`resolution='dismissed'`) so it leaves open task lists. A task is
  decided once: accept after dismiss and dismiss after accept are refused.
  `list_tasks` leaves dismissed suggestions out.
- **Suggestions** become tasks in one transaction under the room row lock
  (`dal.create_suggested_tasks`), with a history row; a partial unique index on
  `(tenant_id, room_id, suggestion_key)` backs the once-per-key rule.
- **Opening a task.** The first opener creates the room (agent: the task's
  `assigned_to_agent` when this instance has it, else the default chat agent)
  and is assigned; the room is seeded with one user-role message labelled as data
  (title, status, objective, the last 10 history rows, all cleaned as edition
  text is). After that, anyone with access gets the room; the task's human
  assignee joins as assigned unless they were removed (`room_member_removals`,
  written by `dal.remove_member`, cleared by `dal.add_member`) or the room is
  archived; anyone else gets `RoomNotFound`. A viewer cannot create one.
- **Tools in sub-agents.** `room_task_accept`, `room_task_dismiss` and
  `room_open_task` refuse a run whose `agent_runs` row is a `sub_agent` trigger
  or has a parent run, and a run the database does not know
  (`tasks.run_is_top_level`). Nothing in tasks or member approval is capped
  (the member list has no row limit; recipients are not capped).
- **Status events.** `room.task` (exactly `{room_id, tenant_id, task_id}`) for
  every linked room on accept, dismiss, suggestion creation, task room
  creation, and from `robothor.crm.dal.update_task` (a status change),
  `resolve_task` and `approve_task`. The bridge's own legacy task DAL does not
  publish them.
- **Proposer columns.** `Operations.propose` writes them from
  `robothor.operations.proposer.current_proposer()`, bound by the runner around
  its tool loop only (set just before it, restored in its `finally`, so no exit
  or cancellation leaves it behind): `proposed_by_run_id` always,
  `proposed_for_user_id` only for an interactive trigger with a user-account id,
  `room_id` from the room run carrier, else the room key in an interactive
  turn's `trigger_detail`. An explicit `proposer=` argument overrides it.
- **Member approval.** Beyond the contract's four conditions the rule also
  requires: the caller's `user_accounts` row active with role `member`/`user`
  now; `proposed_by` NULL (a plugin's draft is its policy's or an operator's);
  the kind on the allow-list `MEMBER_DECIDABLE_KINDS` (only `room.email`;
  every plugin, sales or future kind stays operator-only); the run top level
  (no parent run); the task not done, resolved or deleted. One predicate
  (`approvals.member_turn` / `_TURN_FROM` + `_TURN_WHERE`) serves proposing and
  deciding.
- **Approved email** (`robothor/rooms/outbound.py`, tool `room_propose_email`).
  Sender rule: an approved workspace email is sent from the company's assistant
  mailbox (the one the `gws_*` tools send from) with Reply-To the member who
  approved it, decided only in `outbound.sender_for(tenant_id, user_id)`.
  Payload (`kind: "room.email"`): `{version: 1, sender, reply_to, recipient:
  [..], cc: [..], subject, body, on_behalf_of: {user_id, display_name},
  room_id, task_id}`. The executor (`deliver`, `sweep`; engine
  `room_outbound.py`) claims by id, re-checks hash, sender and do-not-contact,
  sends via the workspace mail provider, and `finish_action`s `completed`
  `{id, thread_id, sent_at}`, `failed` `{error}` (never retried), `cancelled`
  `{error}` (opted out after approval); a lost lease becomes `unknown`.
  Receipt: an assistant chat message with `kind: "room_email_receipt"` and
  `receipt: {action_id, status (sent|failed|uncertain|not_sent|rejected|expired),
  from, reply_to, to, cc, subject, approved_by, decided_by, decided_at, sent_at,
  message_id, error, room_id, task_id}`, written once per action (audit event
  `room.email.noted`), then `room.message`. Accept never takes a task that has
  its own task room through a `linked` link either.
  The action's task is the task workspace's own task
  (`rooms.task_id`): `operation_actions` has no task column, and a report or agent
  workspace's actions stay with operators. A viewer's `GET /api/approvals` stays
  403 (a viewer can never post, so nothing could be theirs); a `member` or `user`
  gets `scope: "own"`. `Operations.decide` accepts `member:<id>` only with a
  `member_check`, re-run under the row lock, and only for a row proposed for that
  id (`NotPermitted`, a `Conflict` subclass, otherwise).

### Tools

`room_create(kind, title, instructions, agent_id?, schedule?, tz?, default_grain?, share_with?)`,
`room_update(room_id, expected_rev, changes)`, `room_list(scope?)`, `room_read(room_id, editions?, messages?)`,
`room_share(room_id, user, assigned?)`, `room_unshare(room_id, user)`,
`room_run(room_id, preview?, period?)`, `room_archive(room_id)`, `room_duplicate(room_id)`,
`room_restore_revision(room_id, rev)`, `room_admin_reassign(room_id, user)`,
`room_publish_edition(room_id, period?, kind?, title, summary, blocks, suggestions?)`,
`room_request_period(room_id, grain, anchor)`, `room_task_accept(room_id, task_id)`,
`room_task_dismiss(room_id, task_id)`, `room_open_task(task_id)`.
Every result that names a room carries `link` = `/?v=chat&room={room_id}`.

### Bridge API (`/api/rooms`)

```
GET    /api/rooms                      -> {rooms: RoomSummary[]}        ?scope=mine|all
POST   /api/rooms                      -> Room
GET    /api/rooms/{id}                 -> Room (+ access, members, runs_as)
PATCH  /api/rooms/{id}                 -> Room           body {expected_rev, changes}; 409 on conflict
POST   /api/rooms/{id}/archive | /unarchive | /duplicate | /read
GET    /api/rooms/{id}/revisions       POST /api/rooms/{id}/revisions/{rev}/restore
GET    /api/rooms/{id}/members         PUT/DELETE /api/rooms/{id}/members/{user_id}
POST   /api/rooms/{id}/reassign
GET    /api/rooms/{id}/editions        GET /api/rooms/{id}/editions/{edition_id}
POST   /api/rooms/{id}/run             body {preview?, period?}          (engine)
POST   /api/rooms/{id}/periods         body {grain, anchor} -> Edition   (engine)
GET    /api/rooms/{id}/tasks           POST /api/rooms/{id}/tasks/{task_id}/accept | /dismiss
POST   /api/rooms/from-task/{task_id}  -> Room
```

No access → 404. Access without the action → 403. JSON is camelCase, as the rest of the bridge.

#### As built (lane 5)

- Top-level fields are camelCase; stored JSON documents (`settings`, `blocks`,
  `suggestions`, `provenance`, a revision's `definition`) pass through as stored, so
  `settings` keys stay `tz`, `default_grain`, `schedule_text`, …
- Bodies accept camelCase or snake_case. `POST /api/rooms` takes `{kind: "agent" |
  "report", title, agentId?, instructions?, scheduleText?, tz?, defaultGrain?,
  shareWith?}` (user ids, each an active account of the caller's company). `PATCH`
  takes `{expectedRev, changes}`; change keys `title, instructions, scheduleText, tz,
  defaultGrain, paused, notifyDefault` (snake_case too) map to the
  `apply_definition_change` names; any other key is 422 `invalid_request` with
  `field: "changes"`. Nothing in the bridge caps cost or frequency: there is no cost
  setting, no run cap and no minimum schedule interval of its own.
- Every refusal is `{"detail": {"error": <code>, "message", "field": <camelCase or
  null>, ...}}`: `not_found` 404, `forbidden` 403 (names the duplicate on a shared
  user's edit), `revision_conflict` 409 + `currentRev`, `invalid_schedule` /
  `unsafe_instructions` 422 with `field`, `invalid_request` 422 (malformed bodies
  included), `ineligible_account` 422, `already_accepted` 409 + `acceptedBy
  {userId, displayName}`, `already_dismissed` 409, `channel_not_linked` 422,
  `not_a_member` 409, `room_busy` 409, `run_as_user` / `last_assigned` 422,
  `unauthenticated` 401, `internal_error` 500. The router's own route class applies
  it. Messages are fixed text per code (or per refusal the router decides); an
  exception's own text never reaches a body, and an unexpected exception is logged
  with its traceback and answered with the generic 500. The engine's answers to `/run` and `/periods`
  pass through unchanged, whatever their status.
- `GET /api/rooms?scope=all` (admin) includes archived rooms; each row adds `members:
  {userId, displayName, assigned}[]` and `assigned: {userId, displayName}[]`. Bounded
  queries: `scope=mine` one, `scope=all` three.
- `GET /api/rooms/directory` → `{users: [{userId, displayName}]}`: the active accounts
  of the acting tenant, by name; no email, no role. Any role that can create a
  workspace (not a viewer).
- `GET /api/rooms/{id}` → `Room`; `unread` is the caller's (0 for a non-member
  admin); `nextRunAt` is the earliest enabled `user_cronjobs.next_run_at` of the
  room, null when paused or archived. `Edition` and `latestEdition` add
  `publishedSeq`; `Edition` adds `error`. `GET /editions` lists building and failed
  editions too (previews still only to their requester).
- `GET /{id}/notify` → `{inApp, muted, channels: {name: bool}, linkedChannels:
  [{name, label}]}`; `PUT` with `{inApp, muted, channels}`. Stored in
  `room_members.notify` as `{in_app, muted, channels}`; `linkedChannels` are the
  caller's unrevoked `user_channel_identities`, and only those may be turned on.
  Read access is enough (it touches only the caller's row; `bridge:read` suffices, as
  for `/read`); a non-member admin gets `not_a_member`.
- `POST /archive` (assigned/admin) and `/unarchive` (admin only; the room returns
  paused) → `Room`. `/duplicate` → 201 `Room`. `/read` → `{read}`, marks only the
  caller and needs only `bridge:read`.
- `GET /revisions` → `{revisions: {rev, changedBy, changedByName, changedAt,
  definition}[]}`; `POST /revisions/{rev}/restore` takes no body (an optional
  `{expected_rev}` is honoured), 404 for a missing revision.
- `GET /members` → `{members}`; `PUT /members/{user_id}` body `{assigned?}` → the
  member; the account must be active and in the caller's company. A new member joins
  shared unless `assigned: true`; for an existing member an omitted `assigned` keeps
  their role. A new member gets an `info` notification addressed to their user id with
  metadata `{kind: "room_shared", room_id, link}`. `DELETE` → `{removed}`, 404 for a
  non-member. Neither may take the run-as user out of the assigned set (422
  `run_as_user`: reassign first) or leave no assigned member (422 `last_assigned`).
- `POST /reassign` body `{userId}` (admin): the user (active, not a viewer) becomes
  the only assigned member and run-as, the room's cron rows included; previously
  assigned members stay as shared (`dal.reassign_room`).
- `POST /run` body `{preview?, period?}` and `POST /periods` body `{grain, anchor,
  end?}` proxy to the engine as `POST /api/rooms/{id}/run|periods` with
  `{...body, requested_by: <user id>}` under the bridge's service token, after the
  bridge has checked `run` / `request_period`.
- `GET /tasks` → `{tasks: [{taskId, title, status, origin, editionId, assignee,
  acceptedBy, dismissed}]}`: title and status from the CRM task; `assignee` is the
  task's `assignee_id` (else `assigned_to_agent` when it is a person's account id)
  when it names an account of the company, else null. Accept and dismiss return the
  task in that shape. `POST /from-task/{task_id}` → `Room` (200), made on first use
  under the main agent with the opener assigned. Once it exists, only the task's
  assignee joins it this way (as shared); anyone else gets 404 until shared in, so a
  removed member cannot rejoin.
- Every mutation writes an audit row with the caller as actor (`room.create`,
  `room.update`, `room.share`, `room.unshare`, `room.restore`, `room.reassign`,
  `room.archive`, `room.unarchive`, `room.duplicate`, `room.task_accept`,
  `room.task_dismiss`, `room.open_task`, `room.run`, `room.request_period`,
  `room.notify`) and, except `notify`, a `room.updated` event with `{room_id,
  tenant_id}` (plus `task_id`).
- Data-layer additions: `get_accounts`, `room_summary`, `members_of_rooms`,
  `reassign_room`, `next_run_at`, `list_directory`, `linked_channels`,
  `task_details`; `unarchive_room` now returns the room paused.

```ts
type Access = "viewer" | "shared" | "assigned" | "admin";
interface RoomSummary { id; kind: "report"|"agent"|"task"; title; agentId; paused; archived;
  unread: number; lastActivityAt; latestEdition?: {id; seq; title; periodStart; periodEnd} }
interface Room extends RoomSummary { instructions; settings; rev; access: Access;
  runsAs?: {userId; displayName}; members: {userId; displayName; assigned}[]; nextRunAt? }
interface Edition { id; seq; kind; status; preview; periodStart; periodEnd; grain; revision;
  title; summary; blocks: {component; props}[]; suggestions; provenance; createdAt }
```

### Chat

Chat stays on the existing routes. The dashboard and the Next.js routes send `room_id`,
never a session key; the engine checks access and resolves the key. History items gain `authorUserId`, `authorName`, `editionId`.
A send while the room is busy returns 409 `{error: "room_busy", answering: <name>}`.

#### As built (lane 2)

`robothor/engine/chat_rooms.py`; the behaviour is in `docs/channels/webchat.md`.

- `chat_rooms.resolve(auth, body_or_query, action) -> ChatTarget | JSONResponse` is
  every chat route's first step (`chat._target`); `ChatTarget(key, room)`.
  `chat._effective_session_key` now refuses a room-shaped key from a human (404), so a
  route that skipped `resolve` fails closed.
- Errors are plain JSON: 404 `{error: "room_not_found"}`, 403 `{error: "room_forbidden"}`,
  400 when `room_id` comes with a `session_key` or `agent_id`.
- Actions: reads `read`; send, abort, steer, interrupt, plan and deep `post`;
  **clear and inject `edit_definition`** (they change what everyone in the room sees).
- Echo: `/chat/send` streams `event: room` `{roomId}` first; history, outcome, abort,
  steer and interrupt add `roomId`; a room's history and export never carry its key.
- `dal.record_activity(tenant_id, room_id, message_id) -> Room | None` (added) moves
  `last_activity_at` and, never backwards, `last_message_id`. `room.message` payload is
  exactly `{room_id, tenant_id, message_id}`.
- Stored user turns carry `author_user_id`, `author_display_name`; an edition's chat
  message is read as `edition_id` (surfaced as `editionId`).

### Events

Stream `agent`, types `room.edition`, `room.message`, `room.updated`, `room.task`, payload
`{room_id, tenant_id, ...}` with ids only (`room.edition` is exactly `{room_id, tenant_id,
edition_id}`); content is fetched through the API. The dashboard filters by acting tenant, then by the rooms the
user can access.

#### As built (lane 12, integration)

The seams between lanes, each crossed by a test:

- **Golden replies.** The bridge and the engine record their real workspace replies
  (real routes, Postgres, only the model stood in) into
  `app/src/lib/rooms/__tests__/contract/{bridge,engine}-replies.json` and fail when a
  reply changes (`robothor/rooms/tests/contract.py`; regenerate with
  `ROOMS_CONTRACT_UPDATE=1`). The dashboard's `contract.test.ts` feeds them to its API
  client, and `engine-contract.test.ts` to the real chat routes and engine client.
- **Bridge over the real definition module.** Archive, unarchive, duplicate and restore
  go through `definition` (`archive_room`, the new `unarchive_room`, `duplicate_room`,
  `restore_room_revision(..., expected_rev=)`), so the schedule row follows them: an
  archived room stops, a duplicate of a scheduled room starts paused,
  a restore moves the schedule back. A missing revision is `RevisionNotFound` (404).
- **Agents.** `create_room_with_schedule(..., agent_known=)` takes the bridge's answer:
  the acting company's engine manifest scan (`routers._manifest_backend`, as the fleet
  views use), not a manifest directory in the bridge process. An engine that cannot
  answer is 502 `agents_unavailable` (field `agentId`). An unknown agent or time zone
  is `definition.InvalidField`, so the 422 names `agentId` / `tz`.
- **Viewers** may be shared with, never assigned (422 `ineligible_account`).
- **One edition shape.** `models.edition_json` adds `error`; the bridge answers editions
  through it, so the engine's `/periods` and the bridge's `/editions` are the same keys.
- **Chat refusals.** The dashboard's engine client keeps a room's 404/403 on a history
  read as `EngineRefusal`; the history route answers it with its status, and a send to a
  room the caller cannot see is 404 (it was 502 "not confirmed").
- **Periods.** The picker refuses any period whose last day is today or later in the
  room's zone (the engine's rule), says so while it is picked, offers no date after
  yesterday, and shows the engine's own words for any refusal it still makes. The
  engine's other refusals pass through the bridge unchanged.
- **Schedules.** The only grammar is `robothor.engine.cron_parse`: `every N
  minutes|hours|days`, a five-field cron expression, or an ISO time (refused for a
  room). The settings drawer's example is checked against it
  (`tests/test_rooms_ui_schedule_examples.py`).
- **Middleware first.** A viewer's `POST /api/rooms` is refused by the bridge
  middleware's scope check before the router, in the middleware's own
  `{error: "insufficient scope"}` body. The dashboard's `createRoom` reads that as
  `cannotCreate` and says the role cannot create workspaces, with no create offered.
- **Default agent.** `definition.default_agent()` is `manifest_store.main_agent_id()`,
  the agent a chat with no key reaches; creating with no agent and opening a task as a
  workspace both use it (`DEFAULT_AGENT` is gone).
- **Tasks through one module.** The bridge's `/tasks`, accept, dismiss and `/from-task`
  routes call `robothor.rooms.tasks` (accept writes the CRM task's assignee; only a
  suggestion is dismissed and a dismissed one leaves the list; the task's assignee joins
  its workspace assigned). Refusals keep the bridge shape with fixed words
  (`not_found`, `already_accepted` with `acceptedBy`, `already_dismissed`, `forbidden`).
  `crm.dal.reject_task` and `delete_task` publish `room.task` like every other change.
- **Shares through one module.** `PUT /members/{id}` calls `definition.share_room` and
  notifies only when `ShareOutcome.added`.
- **Workspace email.** A `room.email` approvals row adds `email` (`from`, `replyTo`, `to`,
  `cc`, `subject`, `body`, `onBehalfOf`) from the payload it authorizes; the task
  workspace's card shows it field by field. `room_propose_email`'s `room_id` is optional
  and defaults to the workspace the chat turn is bound to (`room_run_scope`); nothing else
  gives the agent the id. A receipt turn (`kind: "room_email_receipt"`, `receipt`)
  reaches the dashboard through history unchanged and is drawn as a receipt. Approvals
  appear only in a task workspace; a report workspace's accepted task offers "Open task
  workspace".
- **Provenance cost.** The drawer reads `cost_usd` and the `input_tokens` /
  `output_tokens` the engine writes beside it; the bridge golden carries a provenance
  built by `editions.build_provenance`.
- **History paging** reads one session's page by `(session_id, id)` (migration 175,
  `chat_store.HISTORY_PAGE_SQL`): a quiet workspace's page no longer walks every newer
  message of every session.
- **Strict test database.** `ROOMS_TESTS_REQUIRE_DB=1` turns the rooms fixtures' skip
  into a failure; CI's integration job sets it.
- **The dry run.** `scripts/dev/rooms-stack.sh` runs the real engine, bridge and
  dashboard over their own `*_test` database with row-level security enforcing, local
  sign-in and tenant switching; only the model (a script on litellm's mock path), one
  fixture data tool and the mail transport (an outbox file) are stand-ins.
  `app/playwright.stack.config.ts` runs `app/e2e-stack/` against it.

## Build lanes

1. Foundation: schema, data access, access rules, retention.
2. Chat on a room session.
3. Tools, revisions, duplicate, role seed.
4. Editions and publish.
5. Bridge API.
6. Dashboard shell: rail, room view, edition rendering, live updates.
7. Scheduled runs, run-as, fan-out, preview, room delivery.
8. Periods.
9. Tasks and member approval.
10. Dashboard: edition pane, period selector, settings, share.
11. Dashboard: hub, inbox, tasks tab, open as thread.
12. Integration: end-to-end suite, docs, release notes, performance.

Lane 1 is serial. Lanes 2 to 6 run in parallel on it, then 7 to 11, then 12.

## Security review points

Run-as exposure to shared users; the member-approval widening; instruction injection at
save and at fire; edition text re-entering prompts (bounded, wrapped as data); mirror text
limited to summary and link; run cost visible on each edition, auto-pause after repeated
failures, recovery of a run that dies; the session key
never in notification metadata; preview editions never visible to others.

## Left to the builder

How room tools reach agents other than the main one; the session cache under more than
one engine replica; whether the scheduler tick must enumerate tenants.
