# Workspaces

A workspace is a conversation that belongs to the company, not to one person.
It sits below each person's main conversation and has its own history, an
agent, saved editions or dashboards, linked tasks, an optional schedule, a few
settings, and the colleagues it is shared with. In code, routes and tools a
workspace is a *room* (`robothor/rooms/`, `/api/rooms`, `room_*`).

| Kind | What it is |
|---|---|
| **Report** | A workspace with instructions and, usually, a schedule. Each run publishes an *edition* (charts, metrics, tables, commentary and suggested tasks) for one period. People with access open it, pick another period and carry on with the agent. |
| **Agent** | Another conversation with any agent of the company, kept apart from the main one. |
| **Task** | A CRM task opened as a workspace ("Open as workspace"). There is one per task. |

A workspace never crosses companies: everyone in it, and everything it shows,
belongs to the company that owns it.

## Who can do what

Access comes from two things only: your role in the company and whether the
workspace is shared with you. There is no per-workspace role.

| You are | You can |
|---|---|
| **Shared** (a member it is shared with) | Read, chat, run it now, pick a period, accept or dismiss its tasks, duplicate it as your own |
| **Assigned** (its creator, or a member made assigned) | Everything shared can, and change its settings, share and unshare it, archive it, restore a revision |
| **Company owner or admin** | Everything assigned can, on every workspace of the company, and reassign a workspace, list all of them, restore an archived one |
| **Company viewer** | Read the workspaces shared with them. A viewer cannot create, chat, run or be assigned |

If you have no access to a workspace, it answers exactly as one that does not
exist: "not found". Access without the action you tried is refused as
forbidden. An archived workspace can still be read and duplicated; nothing else
happens in it until an admin restores it.

## Creating one

- **Ask the main agent.** "Make me a weekly sales report, shared with Bob" is
  enough. Before it creates anything the agent restates the title, the
  period, the schedule, the data sources and who it is shared with, and waits
  for your yes. Its answers carry a link to the workspace.
- **From the dashboard.** "New workspace" asks for an agent and a title and
  makes an agent workspace. To make a report, ask the main agent.
- **From a task.** "Open as workspace" on a CRM task makes its workspace on
  first use, with the main agent and you assigned. After that, only the task's
  assignee can join it this way; anyone else must be shared in, so a person who
  was removed cannot rejoin.

You become assigned, and the workspace runs as you. Every person you share it
with must have an active account in the same company. A viewer can be shared
with but not assigned.

## Settings

Only assigned members and admins change settings. Shared members see them and
can duplicate the workspace to change their own copy.

| Setting | Notes |
|---|---|
| Title, instructions | Both are checked for prompt injection when saved; a refused change saves nothing. |
| Agent | Fixed when the workspace is made. Duplicate it to use another agent. |
| Schedule | See below. Empty means no scheduled runs. |
| Time zone | An IANA name such as `Europe/London`. UTC when unset. Used for cron schedules and for periods. |
| Default period | The period length a report works in (day, week, month). |
| Notify people with access | Whether a published edition notifies people by default. Each person can still mute it for themselves. |

### Schedule

A schedule must repeat. It takes one of two forms:

| Form | Examples |
|---|---|
| `every N <unit>`, unit minutes, hours, days or weeks (short forms such as `m`, `min`, `h`, `hr`, `d`, `w` work too) | `every 30 minutes`, `every 6 hours`, `every 1 day` |
| A five-field cron expression, read in the workspace's time zone | `0 8 * * 1` (08:00 every Monday) |

A one-off time is refused: a bare duration such as `2h` or an ISO time such as
`2026-12-01T09:00:00Z`. For a single run, use run now. A workspace may run as
often as any scheduled job on the platform: at most once a minute.

### Pause, archive, restore

- **Pause** stops scheduled runs. It is not a change to the definition and
  writes no revision. On resume, the next run is counted from that moment.
  While it is paused, only its assigned members and admins can run it now or
  ask it for a period; anyone else is told it is paused.
- **Archive** (assigned or admin) stops the schedule and leaves the workspace
  readable.
- **Restore an archived workspace** is for a company owner or admin, from
  "All company workspaces". It comes back paused; an assigned member resumes
  it.

## Revisions and duplicate

Nobody can overwrite a workspace's definition by accident:

- Every change to the title, instructions, schedule or settings writes a
  numbered revision, recording who changed it and when.
- A change is made against the revision you opened. If someone saved in
  between, yours is refused and nothing is changed: reload to see their
  version, then make your change again.
- **Restore** a revision (assigned or admin) writes a new revision equal to
  the old one, checked again as any change is. History is never rewritten.
- **Duplicate as mine** (shared or more, not a viewer) makes your own copy:
  same definition, you assigned and the one it runs as, no editions,
  conversation or tasks. A copy of a scheduled workspace starts paused, so it
  runs only once you resume it.

## Editions and periods

A report's run publishes an edition for one explicit period. A week is its own
edition, not a sum of seven days.

- Periods are whole local days in the workspace's time zone, end included.
  A day is one date, a week runs Monday to Sunday, a month is a calendar month,
  and a custom range is any run of past days.
- Only a period that has ended can be asked for: its last day must be before
  today in the workspace's time zone. The period picker offers nothing after
  yesterday.
- Asking for a period that already has an edition opens it. Otherwise one run
  builds it; people who ask at the same moment share that run.
- Asking for periods is not rationed: every request for a period with no
  edition starts its run.
- Publishing a period again publishes its next revision; earlier ones stay.
- Each edition says how it was made: the run, who it ran as, when it started,
  the tools it called and what the run cost. The server records this from the
  run; the agent cannot set it. Open "How this was made" on an edition to see it.

## When runs fail

Nothing limits how much a workspace runs; instead, problems are made visible
and stuck work is cleaned up:

- A run that fails marks its edition failed and says why; the last good
  edition stays on show.
- A workspace whose scheduled runs fail several times in a row pauses itself,
  and its assigned people are told why. Resume it once the cause is fixed.
- A workspace whose run-as person is deactivated, or loses access, pauses
  rather than run under stale permissions.
- A run that dies part-way (an engine restart, a crash) is recovered: its
  edition does not stay "building" for ever.

## Preview runs

A preview run builds an edition for you alone. It notifies nobody, creates no
tasks and is not posted to the conversation. Nobody else can see it. Use it to
check a change to the instructions before the next scheduled run.

## Who a run runs as

A scheduled run uses the permissions of one assigned member, the *run-as*
user: the creator, until an admin reassigns the workspace. Their access is
looked up again each time the run fires, so a change to their role applies to
the next run. The workspace header and the share dialog show "Runs as
<name>", and each edition's "How this edition was made" shows who it ran as.

The run-as user cannot be removed from the workspace, and neither can the last
assigned member: an admin reassigns it first. Reassigning makes the new person
assigned and the run-as user; the previously assigned members stay as shared.

## One agent turn at a time

The agent answers one person at a time in a workspace. If you send while it is
answering someone else, your message waits in a queue and the chat says whom
it is answering ("Queued — answering Alice…"). It is sent when the turn ends.

Leaving a workspace while the agent is answering you (closing the tab, or
going back to your main conversation) does not stop the answer: it finishes,
everyone in the workspace sees it, and it is there when you come back. To
stop an answer, use the stop button.

## Tasks

A workspace's tasks tab lists the CRM tasks linked to it: the task it was
opened from, tasks accepted from an edition's suggestions, and tasks linked by
hand.

- Anyone shared or more can accept or dismiss a suggested task. Accepting
  makes the task yours (its assignee). A task is decided once: a second person
  is told who already has it. A dismissed suggestion does not come back.
- **Work a task in its own workspace.** An accepted task shows "Open task
  workspace": the task's own workspace, seeded with the task, where you work
  it with the agent. Its assignee can open it; anyone else is shared in.
- **Emails you approve.** In a task workspace whose task is yours, ask the
  agent to email someone. It drafts the email and nothing is sent: the
  workspace shows the exact email (from, reply-to, to, cc, subject and the
  whole body) with **Approve and send** and **Reject**. Approved, it is sent
  once, from the company's assistant mailbox with replies going to you, and a
  receipt appears in the workspace (sent, or not sent and why). Once it is
  sent, the task is done: it moves to Done, its resolution naming the email.
  Rejected, failed, unconfirmed or expired, nothing is sent, the receipt says
  so, and the task stays open. A report workspace proposes no email, and
  nobody else can see or decide yours.
- **Other approvals.** A member decides only an email their own turn in their
  own task workspace proposed. Everything else is decided by those who approve
  actions for the company.

## Notifications

Each person chooses for themselves, per workspace (members only):

| Choice | Effect |
|---|---|
| In-app | Notifications in the dashboard inbox (on by default) |
| Also send to | An external channel you have linked to your account (for example Telegram or Slack). Only a linked channel can be turned on |
| Mute | Nothing from this workspace |

When you are shared into a workspace you get an in-app notification with its
link. Unread counts cover new messages and new editions.

## For admins: tuning what roles may do

The main agent works on workspaces through its `room_*` tools (see
[Tools](TOOLS.md#workspaces-room_)), and those are gated by role permissions.
The defaults:

| Role | Default |
|---|---|
| `owner`, `admin` | Every tool (`*`) |
| `member`, `user` | `room_*` allowed, `room_admin_*` denied |
| `viewer` | `room_list` and `room_read` only |

A company changes them with its own `role_permissions` rows, for example
denying `room_create` to `member`, and per person with `user_permissions`.
The access rules above apply on top: a permission never grants access to a
workspace the person could not otherwise open.

## Related

- [Tools](TOOLS.md#workspaces-room_): the `room_*` tools and
  `room_publish_edition`.
- [Web chat channel](channels/webchat.md#chat-in-a-workspace-a-room): chat in
  a workspace.
- Moving tenants (`docs/runbooks/TENANT_HIERARCHY.md`): a workload move
  carries an agent's workspaces, with their members, revisions and editions,
  and refuses a workspace that has linked tasks; an account move removes the
  account's memberships, since workspaces belong to the company.
