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
  "invitations_requested": true,
  "htmlLink": "https://calendar.google.com/…"
}
```

`kind` is `operator`, `own` or `other`. An agent that says "it's on your
calendar" should be reading `kind` before it says so.

**Invitations.** When an event has attendees, the create passes
`sendUpdates` (and a delete passes it too — a cancellation nobody is told about
is not a cancellation). The value is the governed setting
`ROBOTHOR_CALENDAR_SEND_UPDATES`, default `all`. Set it to `none` and events
still get created, `invitations_requested` comes back `false`, and the agent should
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
| `gws_calendar_create` | The created event, plus `calendar` (`operator`/`own`/`other`), `invitations_requested` and `htmlLink` — **or `{"status": "deduped"}` with nothing created**, when a matching event already exists within ±14 days. Writes to the **operator's** calendar by default, adds a Google Meet link, and emails the attendees. See [Whose calendar](#whose-calendar). |
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

### Vision: `view_image` and `analyze_image`

Two tools, one rule:

| You want to… | Call | What comes back |
|---|---|---|
| Study **one** picture — read a chart, find the bug in a screenshot, describe a photo | `view_image` | The picture itself, in the agent's own context |
| Ask the **same question of many** pictures — sort, label, filter, search a folder | `analyze_image` | One text answer per image; no picture enters the context |

**Why the split.** `view_image` puts the image in front of the agent's own
model. That is the right thing for one image and the wrong thing for fifty:
every call costs a turn, the image tokens stay in the conversation for the
rest of the run, and an agent that has learned looking is expensive stops
looking. Measured on 2026-09-16: on a task that hands an agent a folder of
photographs to categorise, a competing harness called its out-of-band vision
tool 100 times and scored 0.99; this engine called `view_image` four times and
scored 0.28.

`analyze_image` is the out-of-band form. Each image goes to a vision model on
its own, concurrently, and only the text answer returns — so the hundredth
call costs the same as the first.

```jsonc
// call
{"paths": ["photos/a.jpg", "photos/b.jpg"],   // 1-200, inside the workspace
 "question": "Is there a person in this photo? Answer yes or no.",
 "detail": "low",                              // or "high" for small text
 "max_concurrency": 4}                         // 1-16

// result
{"question": "...", "model": "…", "backend": "remote", "analyzed": 2, "failed": 0,
 "results": [{"path": "…/a.jpg", "answer": "yes", "model": "…", "ms": 812,
              "tokens": 612, "cost_usd": 0.000123},
             {"path": "…/b.jpg", "error": "the vision model timed out after 90s on this image",
              "ms": 90004}],
 "summary": "2 of 2 images answered by … in 3.1s (4 at a time)",
 "tokens": 1224, "cost_usd": 0.000246}
```

Answers arrive in the order the paths were given. A row carries `answer` or
`error`, never both — one image that fails does not cost you the batch.

**Refusals.** A path resolving outside the workspace (symlinks followed
first), a credentials file, a missing file, something that is not an image,
and anything over 32 MB are each refused as that image's `error`, without a
model ever being called.

**Cost.** Per-image tokens and cost are reported where the backend gives them,
and the call's total `cost_usd` is added to the run's spend like any other
tool cost. `detail: "low"` is the default deliberately: it is what makes the
tool cheap enough to call in a loop. On the local (Ollama) backend `detail` is
ignored and the result says so — the API has no such knob.

**Big batches spill to a file.** Two hundred rows are ~40,000 characters of
short answers and ~432,000 of long ones, which would put back in the context
what the tool exists to keep out of it. Past
`ROBOTHOR_VISION_BATCH_MAX_CHARS` the result keeps its totals, its counts and
its first rows, and the **whole** table goes to
`<workspace>/.robothor/analyze_image/<run>-<n>.json`:

```jsonc
{"question": "…", "model": "…", "analyzed": 198, "failed": 2,
 "results": [ /* the first rows that fit */ ],
 "results_shown": 12, "results_total": 200,
 "results_file": "/…/.robothor/analyze_image/<run>-1.json",
 "tokens": 121600, "cost_usd": 0.0243,
 "note": "200 rows did not fit … work over that file …"}
```

Work over that file with `exec` (jq, python) rather than reading it whole —
that keeps the win. It matters for the record as well as the context: the
engine replaces any tool output over 4,000 characters in `agent_run_steps`
with a flat head/tail string, so the per-image `tokens`/`cost_usd` ledger
survives in the JSON file and in the run total, not in the step row. The
default budget sits just under that cap so the two agree; raise it and the
step row starts being truncated instead.

#### Configuring the backend

| Setting | What it does |
|---|---|
| `ROBOTHOR_VISION_MODEL` | The local VLM, served by Ollama. The default backend. |
| `ROBOTHOR_VISION_REMOTE_MODEL` | A provider model used instead, for a deployment with no local GPU (a container, the cloud, the benchmark sandbox). Must be **declared** `accepts_images=True` in the engine's model registry — a model the registry has never heard of is refused, same as one it declares text-only. |
| `ROBOTHOR_VISION_BATCH_CONCURRENCY` | Ceiling on images in flight at once (default 4, platform maximum 16). An agent's `max_concurrency` may ask for fewer, never for more. |
| `ROBOTHOR_VISION_BATCH_TIMEOUT` | Seconds one image gets (default 90). |
| `ROBOTHOR_VISION_BATCH_DEADLINE` | Seconds the whole call gets (default 600). |
| `ROBOTHOR_VISION_BATCH_MAX_CHARS` | How much of the result comes back inline before the table spills to a file (default 3500 — just under the 4,000-character cap the step writer truncates at). |

A remote model the registry does not **declare** able to accept images is
**not** dialled — whether it declares the model text-only or has no entry for
it at all. The batch falls back to the local model (the result's `note` says
it did, and which model answered), or refuses and names the model and the
registry field to set. Handing images to a model that cannot take them is the
failure `view_image` was fixed for: a provider 404 one layer down and an agent
that believes it looked. It is worth being strict here rather than optimistic,
because one misconfigured setting is 200 of those 404s in a single call.

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
| Vision | `view_image`, `analyze_image` (image files — see above); `look`, `who_is_here` (a real camera) |
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

Human approval takes two checks, because the gap has two owners and only one
of them is the reader's to fix.

**`agents.destructive_tool_not_gated`** (`recommended`) — an agent that granted
one of the four record-deleting CRM tools — `delete_person`, `delete_company`,
`delete_note`, `delete_task` — without declaring it under
`v2.human_approval_tools` alongside `v2.guardrails: [human_approval]`, or
having exempted itself with `human_approval_fail_open: true`.

**Those four and nothing else.** It does not cover `gws_gmail_send`,
`write_file`, `exec` or `git_push`: naming those fired on 16 of the 16 stock
templates, because they are ordinary grants, and a check that fires on a clean
install is a check nobody reads. Outbound mail is NOT gated by this, and an
earlier version of this paragraph said it was. `gws_calendar_delete` and
`vault_delete` are irreversible too and are deliberately still outside the set —
adding them is a live question, not an oversight.

No engine setting can fix this one: a tool the manifest never named cannot be
escalated whatever the flags say.

**`agents.approval_gate_not_armed`** (`info`) — the manifests asked for human
approval and the ENGINE is not enforcing it, so the declared gates do not
apply. `approval_mode()` needs BOTH `ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED` and
`ROBOTHOR_APPROVAL_MODE=enforce`; `_enforcement_mode` returns `off` whenever
the first is falsy no matter what the mode says, so
`ROBOTHOR_APPROVAL_MODE=enforce` **alone is a no-op** — and the mode is the
name an operator reaches for. A manifest can read as carefully gated in review
and run ungated in production, which is the whole reason this check reads the
engine's settings as well as the files.

It is `info`, so it never marks the instance `degraded`. `observe` is a
deliberate rung on a documented ladder and the Helm chart ships it: a chart
cannot guarantee an approver is wired, and `enforce` with none denies every
escalated call. Reporting a correctly-configured instance mid-soak as broken is
the same "check nobody reads" failure as above, pointed the other way. The
result names the promotion step; `docs/runbooks/approval-enforce.md` in the
repo has the full matrix and the checklist. (Not linked: that runbook is in
`mkdocs.yml`'s `exclude_docs`, so a link here would 404 for a reader of the
published site.)

Of the `agents.*`/`tools.*` checks, three are `recommended` — reported, never
fatal, but they do mark the instance `degraded` — and
`agents.approval_gate_not_armed` is `info`, which does not.
`calendar.operator_calendar_writable` is `required`: an instance that cannot
write the operator's calendar will silently do the wrong thing every time.

---

## Limits

One tool result is capped at `MAX_TOOL_OUTPUT_CHARS` (4,000 characters) **when
it is persisted with the run step** — head-and-tail, with a marker in the
middle. It is not capped on the way to the model: nothing shortens that message
unless an agent sets `tool_offload_threshold`, which defaults to off.

Handlers still fit inside it, for two narrow reasons. Resume reads
`agent_run_checkpoints.messages` and persistent history reads
`chat_messages.message` — neither touches this column, and the run viewer names
the step columns it wants without `tool_output` among them. What does read it
back is `scripts/cleanup_benchmark_crm_debris.py`, which parses
`tool_output->>'id'` to find the CRM rows a benchmark left behind — a row cut
mid-JSON is debris it cannot identify, so cannot remove — and
`bench/wildclaw/run_one.py` when grading a run.

And a 3 MB email body is bad for the context window whether or not anything
truncates it on the way there. Cutting blind lands the hole wherever the
character count falls, which is why the stored record of every long email used
to be two halves of a base64 blob. A handler that caps itself keeps the
beginning and says `body_truncated`.

## See also

* [Agents](enterprise/04-agents.md) — manifests, and what an agent is allowed
  to do.
* [Benchmark sandbox](runbooks/BENCHMARK_SANDBOX.md) — what a graded run may
  touch.
* `docs/agents/INSTRUCTION_CONTRACT.md` in the repository — what an agent's
  instruction file may say, tools included.
