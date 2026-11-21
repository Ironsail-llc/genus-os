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

`settings` keys: `tz` (IANA name), `default_grain`, `schedule_text`, `max_cost_usd`,
`max_on_demand_per_day`, `notify_default`. The *definition* stored in a revision is
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
(`create_room_with_schedule`, `apply_definition_change`, `ROOM_MIN_INTERVAL_SECONDS`
= 900) it exports `list_rooms(actor, *, scope)`, `read_room(actor, room_id, *,
editions, messages)`, `share_room`, `unshare_room`, `archive_room`,
`duplicate_room`, `restore_room_revision`, `reassign_room`, `resolve_user`,
`run_as_actor(tenant_id, room_id)` and `room_schedule(tenant_id, room_id)`.

- A schedule must repeat: a one-off (`in 2 hours`, an ISO time) is refused, as is
  any interval or cron expression whose runs come closer than 15 minutes. A cron
  schedule is read in `settings.tz`, UTC when unset.
- Pausing is not part of the definition: it writes no revision, but still checks
  `expected_rev`.
- A duplicate of a scheduled room starts `paused` (A8: unscheduled until its new
  owner confirms); resuming it creates its schedule row under the new owner.
- `unshare` refuses the run-as user and the last assigned member; `reassign`
  makes the target assigned and the run-as user. A viewer cannot be assigned or
  reassigned to.
- A caller with no verified person behind it may only `room_read`, with the
  access of the room's run-as user re-resolved from `user_accounts`.
- `user_cron.list_due_cronjobs` skips rows with a `room_id`: the generic tick
  must not run a room's instructions as a plain cron prompt. Lane 7 fires them.
- `room_read` takes message authors from `message.author_user_id` (or
  `authorUserId`) on the stored chat message.
- The tools are an opt-in family always given to the main agent; see
  `docs/TOOLS.md`.

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
  `runner.execute`, read with `current_room_run()`. A `ContextVar`, so task-local.
  Chat on a room session binds `RoomRun.for_session(tenant_id, session_key)` (None
  for any other key, which binds nothing). A scheduled or on-demand run binds the
  same plus `preview`, `requested_by` (the preview's requester), `edition_id` (the
  `building` row it created) and that row's period, and passes the run-as user as
  `user_id`/`user_role` to `runner.execute`. Tools never take a room, preview or
  provenance from arguments. The publish is refused unless the carrier names the
  same room and tenant, the room's session key matches, `ctx.agent_id` is the
  room's agent (a sub-agent that inherits the carrier cannot publish), and the
  caller holds `post`.
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
  in the worker thread, exceptions logged); and the edition is noted for the turn,
  so the assistant message `chat_store.save_exchange` / `save_message(role=
  "assistant")` saves next on that session carries `edition_id` (the last one
  published in the turn) and every noted edition gets that `chat_message_id`. A
  preview does none of these.
- Summary at most 4000 characters, title one line of at most 200; escape sequences,
  control and format characters, tag-shaped boundaries (`<untrusted_content>`,
  `<|im_start|>`) and `[RENDER:` / `[DASHBOARD:` openers are removed.

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
  written by `dal.remove_member`, cleared only by `dal.share_member` from an explicit share, never by `add_member`) or the room is
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
  `{error}` (opted out after approval, changed after approval, or approved
but expired before delivery); a lost lease becomes `unknown`. One engine per
tenant: the listener's consumer group is `room-outbound:<event_group>`, it
drops another tenant's approvals, and `sweep(tenant_id)` takes the engine's
own tenant; `deliver`, `sweep` and the receipt writer run inside
`tenant_scope(tenant_id)`. A receipt is recorded as written
(`room.email.noted`) only after it was posted.
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
save and at fire; edition text re-entering prompts (capped, wrapped as data); mirror text
limited to summary and link; minimum interval, cost caps, auto-pause; the session key
never in notification metadata; preview editions never visible to others.

## Left to the builder

How room tools reach agents other than the main one; where the per-run cost cap is
enforced; the session cache under more than one engine replica; whether the scheduler
tick must enumerate tenants.
