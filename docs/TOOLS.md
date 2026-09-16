# Tools

What an agent can actually do, what each tool gives back, and how it finds a
tool it cannot see.

Written after an operator reported that the main agent was "calling the wrong
Google tools". It was: the tools' descriptions did not match what they
returned, the tool search could not find them from the words a person uses,
and nothing told an agent that most of its toolset was not in front of it.

---

## How an agent gets its tools

Every agent's manifest (`docs/agents/<id>.yaml`) carries a `tools_allowed`
list. That list is the agent's reach. What the *model* sees each turn is a
narrower thing.

### Deferred loading

When an agent's allowed set is larger than the deferral threshold, the engine
advertises only a small always-on core plus three meta-tools, and the rest load
on demand. A broad agent with 98 allowed tools is shown **19** schemas — the 16
`CORE_TOOLS` that are registered, plus the three meta-tools — so it carries
about a fifth of the schemas, and rather less than a fifth of the tokens, on
every call.

The three meta-tools:

| Tool | What it does |
|---|---|
| `tool_search(query)` | Ranks the agent's reachable tools against a free-text query. Returns names and descriptions. |
| `tool_describe(name)` | The full parameter schema for one tool. |
| `tool_call(name, arguments)` | Runs a tool that is not in the advertised set. |

**`tool_call` cannot reach outside the agent's allow-list.** It checks the name
against the allow-set the runner published before it dispatches anything, so a
denied tool is refused there, not at the registry.

**The agent is told.** On a deferred run the engine's context turn carries one
sentence:

> `19 tools are in your toolset; 79 more are reachable: call tool_search(query) then tool_call(name, arguments)`

On a run that is not deferred it carries nothing, because there is nothing true
to say. Before this existed, deferral was invisible from inside the turn: the
model saw a short list of schemas with `exec` in it, read instructions telling
it to use `gws_gmail_reply`, and did the cheapest thing that satisfied both —
shelled out to a CLI.

### On a run that is not deferred

`tool_search` still works. It searches the tools the agent already has and says
so (`in_toolset: true`, and a hint not to wrap them in `tool_call`). It used to
answer `tool_search is only available on deferred runs`, which taught the agent
nothing and did not stop it asking again.

### What this means for agent authors

* **Name tools by their registered names.** Never a CLI when a tool exists.
* **A plan may name a reachable tool**, but the plan step has to reach it the
  same way the executing turn will — the planner is shown the same set the
  executor gets, with the deferred ones marked `(via tool_call)`.
* **Only name tools the manifest grants.** `genus doctor --category agents`
  checks this; see [The doctor checks](#the-doctor-checks).

---

## Which mail tool

Three different things in this platform answer to the word "inbox". They are
not interchangeable, and picking the wrong one is how an agent reports that a
mailbox is empty when it is not.

| You want | Use | What it is |
|---|---|---|
| Live email — what is in the mailbox right now | `gws_gmail_search`, then `gws_gmail_get` | The real Gmail account, through the `gws` CLI. The only tools that see an unread message. |
| The CRM's record of past correspondence with a contact | `list_messages`, `get_conversation` | Ingested into the CRM by the email pipeline. Can be hours or days behind, and holds no unread state. |
| The agent's own notification queue | `get_inbox`, `ack_notification` | Notifications other agents and the engine raised for this agent. Not email at all. |

`look` is a webcam. It came back for "look at my inbox" too.

---

## Whose calendar

**The assistant has its own Google account.** Its own address, its own
calendar, its own Drive. `primary` — the Google API's word for "the signed-in
account's default calendar" — is therefore the **assistant's** calendar, not
the operator's.

That sentence is the whole of a 2026-09-16 incident. An assistant planned the
operator's itinerary, created every leg with `calendar_id` at its default of
`primary`, added the operator as an attendee, and told him it was on his
calendar. It was on the bot's. No invitation was sent, because the insert never
passed `sendUpdates`, so nothing reached him by mail either. The API returned a
successful event for each call, so the assistant reported success truthfully as
far as it could see.

The three calendar tools now take a `calendar` parameter:

| Value | Calendar | When |
|---|---|---|
| `operator` | The operator's own calendar, resolved from `~/.robothor/owner.yaml` | **The default.** Anything the operator attends, plans or needs to see. |
| `own` | `primary` — the assistant's account | Only when the event is genuinely the assistant's own. Must be said out loud. |
| (`calendar_id`) | An explicit id, overriding `calendar` | A third, shared calendar. |

And every calendar result now carries the facts needed to report it honestly:

```json
{
  "calendar": {"kind": "operator", "id": "…"},
  "invitations_sent": true,
  "htmlLink": "https://calendar.google.com/…"
}
```

`kind` is `operator`, `own` or `other`. An agent that says "it's on your
calendar" should be reading `kind` before it says so.

**Invitations.** When an event has attendees, the create passes
`sendUpdates` (and a delete passes it too — a cancellation nobody is told about
is not a cancellation). The value is the governed setting
`ROBOTHOR_CALENDAR_SEND_UPDATES`, default `all`. Set it to `none` and events
still get created, `invitations_sent` comes back `false`, and the agent should
say so rather than claim the attendees were told.

**This needs one thing set up outside the platform**: the operator's calendar
must be shared with the instance's Google account with "Make changes to
events". `genus doctor --category calendar` checks exactly that
(`calendar.operator_calendar_writable`, required) and names the fix.

If no operator identity is configured at all, the tools fall back to `primary`
and report `kind: "own"` — they degrade honestly rather than pretending.

---

## The catalogue

### Google Workspace (`gws_*`)

Eleven tools, all shelling out to the `gws` CLI with the instance's Workspace
credentials. Every one of them is refused on a benchmark run — reads included:
a graded agent that can read the operator's mail can put the operator's mail in
its answer.

| Tool | Returns |
|---|---|
| `gws_gmail_search` | One entry per matching message: `id`, `thread_id`, `date`, `from`, `to`, `subject`, `snippet`, `labels`. Already readable — you usually do not need `gws_gmail_get` after it. Drops results rather than letting them be cut (`truncated: true` says so). |
| `gws_gmail_get` | Headers (`from`/`to`/`cc`/`date`/`subject`/`message_id`), `labels`, `snippet`, and a **decoded** `body_text` — plain text, never base64. Attachments listed by `filename`, `mime_type`, `size_bytes`, never inlined. `body_truncated` + `body_chars` when it had to cut. A `thread_id` returns every message in the thread, oldest first. |
| `gws_gmail_reply` | The sent message id and thread id. Threads correctly, replies to all original recipients, refuses a second reply on the same thread. **Use this, not `gws_gmail_send`, for anything that answers an existing thread.** |
| `gws_gmail_send` | The sent message id and thread id. New conversations only. |
| `gws_gmail_modify` | The message id and its labels after the change. Mark read/unread, archive, add or remove labels. |
| `gws_calendar_list` | The events in a range: id, start, end, summary, location, attendees — plus `calendar`, saying whose calendar was read. Reads the **operator's** by default. |
| `gws_calendar_create` | The created event, plus `calendar` (`operator`/`own`/`other`), `invitations_sent` and `htmlLink` — **or `{"status": "deduped"}` with nothing created**, when a matching event already exists within ±14 days. Writes to the **operator's** calendar by default, adds a Google Meet link, and emails the attendees. See [Whose calendar](#whose-calendar). |
| `gws_calendar_delete` | Confirmation plus `calendar` and `cancellations_sent`. Deletes permanently from the **operator's** calendar by default and emails the attendees a cancellation. |
| `gws_chat_send` | The created message's resource name. |
| `gws_chat_list_spaces` | Each space's resource name and display name. |
| `gws_chat_list_messages` | Each message's sender, time and text, newest last. |

#### Errors

A failed `gws` call returns `{"error", "hint"}`. `error` is the CLI's own
diagnostics when it produced any — stderr, else stdout; `hint` classifies the
failure as `not_found`, `auth`, `invalid_params`, `rate_limited`, `timeout` or
`not_installed`. When nothing matches, `hint` is a sentence rather than a class
token, and it says what to check. Either way it is never a bare exit code: the
CLI writes nothing to stderr for several failure classes, and `gws exited with
code 1` is not something an agent can act on.

`not_found` usually means the id is not real. A placeholder id copied out of a
prompt (`thread_def456`) is the common case.

#### What does not exist

There is **no Drive, Docs, Sheets, Google Tasks or Contacts tool** on this
platform, and no plan to fake one. `tool_search` says so outright rather than
returning the least-bad match. The only route is `exec` with a Workspace CLI,
if the agent's manifest grants `exec` and its `exec_allowlist` admits the
command.

`send_email` is not a tool either — it is the `send-email` **skill**, invoked
with `invoke_skill(name="send-email")`.

### Everything else

The full registry is large and changes with the release; `tool_search` over the
agent's own allow-set is the authoritative answer. The families:

| Family | Examples |
|---|---|
| Files and shell | `read_file`, `write_file`, `list_directory`, `exec` |
| Web | `web_fetch`, `web_search`, `browser` |
| Memory | `search_memory`, `memory_block_read`, `memory_block_write`, `store_memory` |
| CRM | `search_records`, `get_person`, `create_task`, `list_my_tasks`, `resolve_task` |
| Notifications | `get_inbox`, `ack_notification`, `send_notification` |
| Skills | `list_skills`, `invoke_skill` |
| Sub-agents | `spawn_agent`, `spawn_agents` (only with `can_spawn_agents`) |
| Vision | `look`, `who_is_here` (a real camera) |
| Desktop | `desktop_*` (a real screen) |

---

## How search ranks

`tool_search` scores a tool on its name, its description and a **keyword
vocabulary** — the words a person uses, which are rarely the words a schema
uses. Nobody asks to "search Gmail messages"; they ask to "check my email".

* A keyword hit counts as much as a hit on the tool's own name.
* A verb ("read", "check", "cancel") only earns its routing bonus for a tool
  whose vocabulary mentions the **object** the query named. That is what keeps
  `read_file` behind Gmail for "read my emails".
* Ties break on keyword coverage, then name. They used to break alphabetically,
  which is how `browser` came first for "check my email".
* The description comes back **whole**. It used to be cut at 200 characters,
  and `gws_gmail_reply`'s decisive sentence started at 199.

The vocabulary lives in `robothor/engine/tools/keywords.py`, one table keyed by
registered tool name. It never reaches the model's request body: `keywords`,
`when_to_use` and `rank_bias` are stripped before a schema is advertised,
because they are not part of the function-schema contract.

---

## The doctor checks

```bash
genus doctor --category agents
genus doctor --category tools
genus doctor --category calendar
```

**`agents.tools_named_but_not_granted`** — for each agent, the registered tool
names that appear in its instruction and bootstrap files but are absent from
`tools_allowed`, plus `tools_allowed` entries that resolve to no registered
schema. Both halves are silent in production: the agent cannot see its own
manifest, so a tool it was told to use simply stops working and it finds
another way; and an unresolvable name is dropped after one journald warning.
Results name agents and tools, never instruction text.

**`calendar.operator_calendar_writable`** (required, and skipped entirely on an
instance where no agent holds a calendar tool) — the instance's Google account
lists the operator's calendar with `accessRole` `owner` or `writer`. Without it
every event an agent creates lands on the assistant's own calendar while
looking like a success. The fix is in the operator's Google Calendar: share it
with this instance's account, "Make changes to events".

**`tools.exec_allowlist_bypasses_denied_tool`** — an `exec_allowlist` regex
that admits a Google CLI (`gog gmail send`, `gws calendar events insert`) for
an agent whose `tools_denied` names the native tool for the same action. The
denial reads as a policy and is not one: the same account is reached through
`exec`, past the do-not-contact check, the duplicate-reply guard, the threading
and the benchmark gate.

The two `agents.*`/`tools.*` checks are `recommended` — reported, never fatal.
`calendar.operator_calendar_writable` is `required`: an instance that cannot
write the operator's calendar will silently do the wrong thing every time.

---

## Limits

One tool result is capped at `MAX_TOOL_OUTPUT_CHARS` (4,000 characters) **when
it is persisted with the run step** — head-and-tail, with a marker in the
middle. It is not capped on the way to the model: nothing shortens that message
unless an agent sets `tool_offload_threshold`, which defaults to off.

Handlers still fit inside it, for two reasons:

* the stored row is what a **resumed** or persistent-history run reads back, so
  a result cut in the middle is what that run is handed;
* the step row is the **audit trail** — the run viewer, the verification pass
  and any support bundle read it.

And a 3 MB email body is bad for the context window whether or not anything
truncates it. Cutting blind lands the hole wherever the character count falls,
which is why the stored record of every long email used to be two halves of a
base64 blob. A handler that caps itself keeps the beginning and says
`body_truncated`.

## See also

* [Agents](enterprise/04-agents.md) — manifests, and what an agent is allowed
  to do.
* [Benchmark sandbox](runbooks/BENCHMARK_SANDBOX.md) — what a graded run may
  touch.
* `docs/agents/INSTRUCTION_CONTRACT.md` in the repository — what an agent's
  instruction file may say, tools included.
