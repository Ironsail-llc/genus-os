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
tool 100 times and scored 0.992 on the task; this engine called `view_image`
four times and scored 0.424, classifying at 0.28 accuracy where five classes
make 0.20 random — it was guessing from filenames.

`analyze_image` is the out-of-band form. Each image goes to a vision model on
its own, concurrently, and only the text answer returns — so the hundredth
call costs the same as the first.

```jsonc
// call
{"paths": ["photos/a.jpg", "photos/b.jpg"],   // 1-200, inside the workspace
 "question": "Which of these categories does this image belong to?",
 "choices": ["chart", "photo", "screenshot"], // optional; 2-20 labels
 "detail": "low",                              // or "high" for small text
 "max_concurrency": 4}                         // 1-16

// result
{"question": "...", "model": "…", "backend": "remote", "analyzed": 2, "failed": 0,
 "results": [{"path": "…/a.jpg", "choice": "chart",
              "reason": "a horizontal bar chart titled 'Net bilateral aid flow'",
              "model": "…", "ms": 812, "tokens": 612, "cost_usd": 0.000123},
             {"path": "…/b.jpg", "error": "the vision model timed out after 90s on this image",
              "ms": 90004}],
 "summary": "2 of 2 images answered by … in 3.1s (4 at a time). A filename is a claim; …",
 "tokens": 1224, "cost_usd": 0.000246}
```

Answers arrive in the order the paths were given. A row carries exactly one of
`choice`, `answer` or `error` — one image that fails does not cost you the
batch.

**`choices` makes the contract "a label from this set".** Pass 2–20 labels and
each row comes back as a validated `choice`, spelled the way **you** spelled it
rather than the way the model did, so rows group without splitting on a
trailing full stop or a capital letter. A reply that names none of them is
asked once more, quoting what was rejected; if the second reply is off-list too
the row becomes an `error`. Leave `choices` out and the answer is free text
under `answer`, as before.

*The matching rule, exactly.* Both the reply and each label are put through the
same normalisation — **case folded, surrounding punctuation and markdown
stripped, inner whitespace collapsed** — and then compared for **equality**.
Nothing else matches:

| Reply, against `choices: ["photo", "chart"]` | Result |
|---|---|
| `photo`, `Photo`, `photo.`, `**photo**`, `"photo"` | `choice: "photo"` |
| `a photo of a beach` | off-list → re-asked → `error` |
| `照片` | off-list → re-asked → `error` |
| `categ`, against `["cat", "category"]` | off-list — neither, and `cat` never swallows `category` |

There is **no prefix match, no substring match and no edit distance**. Two
labels that would normalise to the same key (`["Chart", "chart."]`) are refused
when you pass them, rather than silently merged, so a `choice` always names
exactly one of the labels you gave.

The tool also reads the shapes models actually write, without loosening that
rule: `ANSWER: chart WHY: bars` on one line, `{"answer": "chart", "why":
"bars"}`, a ```` ``` ```` fence, a `<think>` preamble, `**ANSWER:** chart`, and
`chart (bars)` all yield the label plus its reason. What is extracted still has
to *be* a label — `banana (a chart)` names nothing and is off-list like anything
else.

**Without `choices`, a JSON reply is kept whole** unless it looks like a reply
to that contract — an answer key *and* a reason key. So asking "transcribe the
JSON on this screen" returns the whole object even when it happens to contain a
key called `answer`, `label` or `category`, while `{"answer": "42", "why": "the
big number"}` is still read as an answer and its reason. Without that shape
test a transcription would silently come back as one of its own fields.

The two-key test is a shape test, not a mind-reader: an object that carries
*both* key kinds is read as a reply whatever it was meant to be. A quiz-shaped
payload — `{"question": "2+2?", "answer": "4", "explanation": "basic sum"}` —
therefore folds to `answer: "4"` with `reason: "basic sum"`, and the `question`
field is not in the row. If you are transcribing structured data rather than
asking a question about it, say so in the question and read the row as a
transcription, or ask for a field at a time.

**Every answered row carries a `reason`** — one short sentence (capped at 120
characters) of what the model saw. That is what makes a batch auditable:
`{"answer": "1"}` is a bare token you can talk yourself out of believing, and
`{"choice": "1", "reason": "horizontal bar chart of aid flow from Sweden"}` is
not. A backend that ignores the two-line reply contract still answers; that row
says `reason_missing: true` rather than having one invented for it, and it is
**not** re-asked — a re-ask is for a wrong answer, not a thin one.

**A look outranks a filename.** Every result's `summary` ends by saying so, and
so does behavioural rule 18: a filename, caption or alt text is a *claim* about
an image, not evidence of its contents. When a batch disagrees with the names,
read two or three `reason`s, spot-check one with `view_image`, and then trust
the batch. This is measured advice — on a 100-image sort whose filenames were
deliberately misleading, the tool answered 92.9% correctly and the agent threw
every answer away in favour of the names, scoring 0.43 where the answers it
already had were worth 0.94.

**Refusals.** A path resolving outside the workspace (symlinks followed
first), a credentials file, a missing file, something that is not an image,
and anything over 32 MB are each refused as that image's `error`, without a
model ever being called. A refused row reports the same resolved path an
answered row does, so an agent can line its request up against the results.

**A missed extension is not a miss.** Ask for `scan.png` when the file is
`scan.jpg` and the one image sharing that stem is read instead. Two candidates
is a question for you, not a coin flip for the tool, so it is an error naming
both. A *relative* path that misses says which workspace root it was joined to
and what that produced — fifty rows of `no such file: render.jpg` with no hint
that a root join was tried is a wasted round.

Both vision tools report a substitution the same way, and the two fields never
mean the same thing:

| Field | Meaning |
|---|---|
| `path` | the file that was **actually read** |
| `resolved_from` | the path you **asked for**, present only when they differ |

So `{"path": "…/scan.jpg", "resolved_from": "…/scan.png"}` reads "you asked for
the png, I read the jpg". `view_image` used to set `resolved_from` to the
substitute — making it a copy of `path` that told you nothing — and was
corrected to match `analyze_image`.

**Plan mode.** `analyze_image` counts as read-only — it changes nothing on the
box or anywhere else — so an agent in plan mode may call it. On a remote
backend that is real money spent while planning; `ROBOTHOR_VISION_BATCH_MAX_CHARS`
and the concurrency ceiling bound the call, not the spend. Deny the tool for
an agent where that is not wanted.

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
 "sample": [{"path": "…/a.png", "choice": "chart", "reason": "bars and a titled y axis"},
            {"path": "…/q.png", "choice": "photo", "reason": "a beach at sunset"}],
 "results": [ /* the first rows that fit */ ],
 "results_shown": 12, "results_total": 200,
 "results_file": "/…/.robothor/analyze_image/<run>-1.json",
 "tokens": 121600, "cost_usd": 0.0243,
 "note": "200 rows did not fit … work over that file …"}
```

**`sample` is the spot-check, free.** **One row per distinct answer**, up to
five — never two rows carrying the same label, and never simply the first five,
because the first five rows of a folder being sorted are five looks at the same
label. So a two-label sort samples two rows, and one glance tells you whether
the batch decided sensibly without a `read_file` round. It appears only when the
table spills (a small batch already has every row inline) and it competes for
the same characters as everything else: it yields a row at a time rather than
leave you with a preview and no `results` at all, and the `note` mentions it
only when there is one. Padding it out to five with duplicates would buy a
fourth look at one decision by taking away rows you actually read.

Work over that file with `exec` (jq, python) rather than reading it whole —
that keeps the win. It matters for the record as well as the context: the
engine replaces any tool output over 4,000 characters in `agent_run_steps`
with a flat head/tail string, so the per-image `tokens`/`cost_usd` ledger
survives in the JSON file and in the run total, and the inline result is kept
under that cap so the step row is never flattened — including when every note
fires and the workspace path is long. That is the invariant; raising the
setting cannot break it, because the budget is clamped below the cap.

Spilled tables are deleted by the daily retention sweep after
`ROBOTHOR_VISION_BATCH_RETENTION_DAYS` (7), so the directory does not grow
without bound. Copy one somewhere else if you want to keep it.

#### Configuring the backend

| Setting | What it does |
|---|---|
| `ROBOTHOR_VISION_MODEL` | The local VLM, served by Ollama. The default backend. |
| `ROBOTHOR_VISION_REMOTE_MODEL` | A provider model used instead, for a deployment with no local GPU (a container, the cloud, the benchmark sandbox). Must be **declared** `accepts_images=True` in the engine's model registry — a model the registry has never heard of is refused, same as one it declares text-only. |
| `ROBOTHOR_VISION_BATCH_CONCURRENCY` | Ceiling on images in flight at once (default 4, platform maximum 16). An agent's `max_concurrency` may ask for fewer, never for more. |
| `ROBOTHOR_VISION_BATCH_TIMEOUT` | Seconds one image gets (default 90). |
| `ROBOTHOR_VISION_BATCH_DEADLINE` | Seconds the whole call gets (default 600). |
| `ROBOTHOR_VISION_BATCH_MAX_CHARS` | How much of the result comes back inline before the table spills to a file (default 3500). Clamped to 3800 — the step writer flattens a tool result over 4,000 characters, so a larger value would destroy the per-image record it exists to keep. 0 or less means the default; the bound cannot be turned off. |
| `ROBOTHOR_VISION_BATCH_RETENTION_DAYS` | How long a spilled table is kept before the daily retention sweep deletes it (default 7). 0 disables the prune. |

A remote model the registry does not **declare** able to accept images is
**not** dialled — whether it declares the model text-only or has no entry for
it at all. The batch falls back to the local model (the result's `note` says
it did, and which model answered), or refuses and names the model and the
registry field to set. Handing images to a model that cannot take them is the
failure `view_image` was fixed for: a provider 404 one layer down and an agent
that believes it looked. It is worth being strict here rather than optimistic,
because one misconfigured setting is 200 of those 404s in a single call.

### Several calls in one turn

A model turn may carry more than one tool call, and the engine runs the
independent ones at the same time.

The rule is narrow on purpose:

* a call is eligible for concurrency only when the platform has **classified**
  it read-only — core's `READONLY_TOOLS` table, plus whatever an installed MCP
  adapter or plugin declared in its own `read_only` list. Absent means write,
  in every one of those three sources, so a capability nobody classified is
  never treated as safe;
* below that classification sits a hard list: `exec`, `execute_code`,
  `browser`, `ask_user`, and anything spelled `send_*`, `spawn_*` or
  `desktop_*` are never concurrent whatever a table says about them;
* a tool matching the agent's `human_approval_tools` patterns is never
  concurrent either — that gate blocks on a person, and a batch waiting on a
  person is not a batch;
* **the first ineligible call ends grouping for the rest of the turn.** It runs
  alone, and so does everything after it, in the order the model asked. A write
  is never reordered relative to anything, including relative to a read that
  might observe what it wrote.

Admission is untouched: plan mode, `tools_allowed`, the `PRE_TOOL_USE` hook,
the guardrail engine (including human approval) and the RBAC check all run one
call at a time, in the model's order, before anything executes. Results come
back with their own `tool_call_id`s in the model's order, a refused call does
not hold up its siblings, and per-call timeouts and the run watchdog apply
exactly as before.

**A turn is one iteration, whatever it carries.** Three calls in one turn cost
one check-in, not three — which is the point: the accounting has to reward
asking for three things at once.

`ROBOTHOR_PARALLEL_TOOL_CALLS` sets how many run at once (default 4, platform
ceiling 16). **1 restores fully sequential execution**, which is the
before-picture for a differential measurement.

Step rows record `batch_id` and `batch_position`, so the ledger can answer
"which turns fanned out, and did the results come back in the order asked?"
Timestamps cannot: two calls that overlapped look identical to two that queued.

### `execute_code` — calling tools from inside Python

`execute_code` runs a Python snippet that can call the agent's own tools:

```python
from genus_tools import web_fetch, read_file
import genus_tools

for paper_id in ids:                      # one model turn, not fifty
    page = web_fetch(url=f"https://example.org/{paper_id}")
    ...
genus_tools.call("list_people", query="acme")
print(summary)                            # only stdout comes back
```

**Why it exists.** Measured 2026-09-16: on the three WildClawBench Productivity
tasks this engine scored 0.000 on, a competing harness imported its tool
library inside a code sandbox 52, 13 and 7 times. One of those tasks needs 130
papers classified. `web_fetch` is a turn-level tool, so that loop costs 130
turns here and one there — and 130 turns is not a thing a context window holds.

**The module.** Any tool name is importable (`from genus_tools import
gws_gmail_get`), `genus_tools.call(name, **args)` takes a computed name, and
`genus_tools.tools()` lists what this run may reach. A call returns the tool's
own result dictionary, **errors included**, so one bad item does not end a loop
over fifty. `genus_tools.ToolError` is raised only when the call could not be
made at all — the per-snippet limit is spent, the tool may not be reached from
code, or the channel to the engine is gone.

**The contract, exactly.**

| | |
|---|---|
| Reach | Every tool the agent could call from a turn, and nothing else. Each proxied call passes the same admission gates in the same order, then `registry.execute` — so the repeat guard, the benchmark sandbox, the audit row and post-condition verification all apply. |
| Never reachable | `execute_code` (no recursion), `spawn_agent`/`spawn_agents` (a spawn runs a whole child runner inline), `ask_user` (it blocks on a person). |
| Requires | `exec` in the agent's `tools_allowed`. An agent that can run commands already has this process capability; what is new is the tool proxy. |
| Transport | A unix socket in a 0700 directory, authenticated by a token in a 0600 file. The wire carries a tool name and arguments — there is no `tool_call_id` field, so there is nothing for a snippet to forge. One call at a time, so two writes cannot race. |
| Environment | The scrubbed child environment at **`enforce`**, always — whatever `ROBOTHOR_EXEC_ENV_MODE` the instance is on. The snippet gets the process essentials, the declared non-secret `ROBOTHOR_*` settings and this agent's own `secrets:` grants: no database password, no provider key, no channel token. Scrubbing removes INHERITANCE, though, not the credentials from the engine — and the snippet's parent IS the engine, whose `/proc/<pid>/environ` a same-uid process may read. So the handler also calls `harden_process()` (`PR_SET_DUMPABLE=0`) before spawning, which makes those entries root-only. That is a **mitigation, not a boundary**: the remedy is the engine's environment ceasing to hold application credentials at all (see the SOPS bootstrap runbook), after which procfs leaks only bootstrap values. `exec` has the same exposure and the same mitigation. |
| Interpreter | Isolated (`-I`: no `PYTHONPATH`, no user site) with an import guard that refuses `robothor`, `crm`, `psycopg2`, `litellm` and `redis`. The guard is defence in depth, not the boundary — the boundary is that the environment holds nothing worth importing the engine for. |
| Lifetime | Its own process group **and a census of its descendants**, killed when the call returns — whether it finished, timed out, or was cancelled. Two kills, because neither alone is enough: the snippet reaps its own children on the way out (where the parent chain is intact, so a `setsid` child or a double-forked daemon is still reachable), and the engine kills the group plus everything its census saw (which is what catches a timeout, a cancellation, or a snippet that skipped its own cleanup). **A snippet can defeat both deliberately**, and this is reproducible rather than a race: start a child with `start_new_session=True` (outside the process group) and then call `os._exit` (skipping the snippet's own reaper), and it survives every time. So treat this as a budget for honest work, not a containment boundary for hostile code — which is the same footing `exec` is on, and why `execute_code` requires it. The closure is a cgroup the engine can kill as a unit, which needs `Delegate=yes` on the engine's unit: an operator change. |
| Iterations | One. A snippet that makes two hundred proxied calls still costs the run a single turn — while each proxied call is still seen by the repeat guard and still earns its own step row. |

**Bounds.** `ROBOTHOR_EXECUTE_CODE_MAX_CALLS` (default 200) caps proxied calls
per snippet; past it `genus_tools` raises and the result says
`tool_call_limit_reached`. `ROBOTHOR_EXECUTE_CODE_MAX_APPROVALS` (default 1)
caps how many of those may need a person: a proxied call passes the same
approval gate a turn's call does — a snippet can never bypass one — so without
this a loop could queue one prompt per call at the operator, each holding the
engine for `human_approval_timeout`. Past the cap the call is refused with a
sentence telling the agent to make it from a turn instead. `ROBOTHOR_EXECUTE_CODE_TIMEOUT` (default 300, ceiling
900, and the run's own remaining budget clamps it further) caps wall clock.
`ROBOTHOR_EXECUTE_CODE_MAX_OUTPUT` (default 50,000 characters) caps the stdout
that reaches the model; past it the output is cut **with a marker saying how
much was cut** and the whole of it is written under
`<workspace>/.robothor/execute_code/`, whose path comes back as `stdout_file`.
Truncation is pagination, not amputation — the same trade `analyze_image`
makes.

**Not available to a container-sandboxed agent.** An agent declaring
`sandbox: docker` runs with `--network none` and no host environment, so a
snippet there has no route to the tool proxy. The tool refuses and says so
rather than quietly running on the host, which would undo the isolation the
manifest asked for.

### `exec` — what you see of a command's output

A command's output reaches you through a bounded window: **~4,000 characters of
stdout and ~2,000 of stderr**. Past that the result carries four more fields and
a marker:

```json
{
  "stdout": "…the first 4,000 characters…\n\n[truncated: 4000 of 12431 chars shown — the full output is at <workspace>/.robothor/exec/<run>__stdout__<id>.txt; read_file it, or re-run with a narrower command]",
  "stdout_truncated": true,
  "stdout_chars": 12431,
  "stdout_shown_chars": 4000,
  "stdout_path": "<workspace>/.robothor/exec/<run>__stdout__<id>.txt",
  "exit_code": 0
}
```

`stdout_chars` is how much the command produced; `stdout_shown_chars` is how
much of it is above, which is the marker's first number in a field. stderr
carries the same four under `stderr_*`.

**Truncation is pagination, not amputation** — up to a limit, and the limit is
stated rather than discovered. The stream is written to that file before
anything is cut, so `read_file` on `stdout_path` gets what the window could not
hold. A stream that fit carries none of these fields at all, and that absence
does mean nothing was lost.

**Two things can make the file less than the whole**, and the result says so
both times rather than leaving you to find out:

* **The spill has its own ceiling** (`ROBOTHOR_EXEC_SPILL_MAX_BYTES`, 8 MiB by
  default). A command that printed more than that gets a file holding the first
  8 MiB, and the result then carries `"stdout_spill_capped": true` and
  `"stdout_spill_chars"` — how much the file actually holds — while the marker
  changes to *"the first N chars are at `<path>` (the spill is capped)"*. If
  what you need is past that, narrow the command; the file will not have it.
* **A spill that would leave the filesystem with under 64 MiB free is skipped.**
  No file is written, `stdout_path` is absent, and the marker falls back to
  *"re-run with a narrower command, or write the full output to a file and
  read_file it"*. The command itself still succeeds.

So the rule to work from: **`stdout_path` present and `stdout_spill_capped`
absent means the file is the whole stream.** Anything else, read the marker.

**Why it matters more than it looks.** Measured 2026-09-16: one listing call
returned `{"records": [...], "total": 20}`, the cut landed inside record twelve,
and `total` — the one field that would have revealed the cut — was itself in the
discarded tail. The agent reported over twelve of twenty records as though they
were all of them and scored zero on every graded item past the cut, while
scoring full marks on everything inside the window. Nothing about the reasoning
was wrong; the input was smaller than the task.

The spill lives under `<workspace>/.robothor/exec/`, so it is never mistaken for
a deliverable. It is deleted when the run ends. Reading it back is exempt from
the repeat guard and the no-progress detector — paging in the rest of your own
output is progress, not a loop — and that exemption is keyed on the path
resolving to a real spill file, so quoting the path in a comment buys nothing.

`execute_code` has the same contract with a bigger window: see
[its section](#execute_code-calling-tools-from-inside-python) and `stdout_file`.

### Calling an API from code: `genus_tools`, not `curl`

Both work. They differ in what survives the call.

| | What the run keeps |
|---|---|
| `genus_tools.call("x", …)` inside `execute_code` | The **whole response**, on this run's step ledger, whatever the snippet printed — plus the audit row, the guardrail pass and the post-condition check a turn's call gets. |
| `urllib` / `requests` / `curl` inside `exec` or a snippet | **Only what you printed.** Everything else is gone the moment the process exits. |

**The step ledger is not your context.** A proxied call earns a durable row
that the run viewer, the verification pass and an operator can read afterwards
— and it deliberately does **not** put a message in front of you, because the
whole value of the code path is that fifty lookups cost one turn's context
rather than fifty. You cannot read that row back later. So `genus_tools` makes
the response *recoverable by a human*; only printing it makes the response
*available to you*. Print what you need to reason about, every time.

That difference decided a graded task. An agent sent twelve messages in one
loop and printed `result.get("status")`; the service had returned a follow-up
message inline in three of those very responses, carrying a deadline, a penalty
and an account list. The model's entire view of the twelve calls was
`[1/12] To: … → sent`, twelve times, and the report it then wrote scored 1.0
for quality and 0.0 for accuracy.

So: **route API calls through `genus_tools` where the tool exists**, and where
you must use HTTP directly, print the response rather than a status field.
`execute_code` now counts what you skipped — a result carrying
`unread_responses` is telling you that many proxied calls returned a body your
snippet never showed itself.

### After you change something, look again

A call that changed remote state invalidates what you knew about that source.
Sending a message, creating a record or POSTing to an endpoint can cause the
other side to produce something new — and it very often does. If a run makes
state-changing calls and then writes its deliverable with no read in between,
the engine says so once at a check-in. Treat that note as what it is: the
report you are about to write is about the world as it was *before* you acted.

### Everything else

The full registry is large and changes with the release; `tool_search` over the
agent's own allow-set is the authoritative answer. The families:

| Family | Examples |
|---|---|
| Files and shell | `read_file`, `write_file`, `list_directory`, `exec`, [`execute_code`](#execute_code-calling-tools-from-inside-python) |
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

### Autonomy first

Genus OS runs agents **autonomously**. That is the default and the design: an
agent holds the tools its manifest grants and uses them without asking. When an
agent lacks a permission it needs, the remedy is to grant it or give it another
route — never to put a person in front of it. Neither check below is telling
you to add a gate.

The approval gate is a real feature and turning it on is one manifest edit, per
agent, for the tools that agent chooses:

```yaml
v2:
  guardrails: [human_approval]
  human_approval_tools: [issue_refund]
```

Use it for an **irreversible external action** — a refund, a payment, a
deletion in someone else's system — where a wrong call cannot be taken back.
That is a small list on most instances. Putting it in front of ordinary work
costs a run: on an unattended schedule nobody answers, the call waits out
`human_approval_timeout` and is then denied, so the agent does nothing and the
operator gets a prompt per item.

Two checks report on it, because the two halves have different owners.

**`agents.approval_gate_available`** (`info`, and it reports as a pass) — which
of the four record-deleting CRM tools — `delete_person`, `delete_company`,
`delete_note`, `delete_task` — an agent granted without declaring under
`v2.human_approval_tools` alongside `v2.guardrails: [human_approval]`, or
having exempted itself with `human_approval_fail_open: true`. A fact for the
record and a pointer to the keys above, not a gap: ungated is how the platform
runs. It shipped as `agents.destructive_tool_not_gated` at `recommended`, which
read as advice — and an instance took it, gated `delete_person` on a nightly
unattended hygiene scan, and every duplicate-contact delete asked a person,
timed out and was denied.

**Those four and nothing else.** It does not cover `gws_gmail_send`,
`write_file`, `exec` or `git_push`: naming those fired on 16 of the 16 stock
templates, because they are ordinary grants, and a check that fires on a clean
install is a check nobody reads. Outbound mail is NOT gated by this, and an
earlier version of this paragraph said it was. `gws_calendar_delete` and
`vault_delete` are irreversible too and are deliberately still outside the set —
adding them is a live question, not an oversight.

No engine setting changes this one: a tool the manifest never named cannot be
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

It is `info` and it reports as a **pass**, like the check above, and the result
says so in words: an unarmed gate is the default posture, not a gap. Severity
alone was not enough — at `info` it already could not mark the instance
`degraded`, but it still printed a red ✗ beside a sentence explaining that the
state is the default, and a reader resolves that contradiction in favour of the
glyph. `observe` is a
deliberate rung on a documented ladder and the Helm chart ships it: a chart
cannot guarantee an approver is wired, and `enforce` with none denies every
escalated call. The result names the promotion step for an instance that wants
it; `docs/runbooks/approval-enforce.md` in the repo has the full matrix and the
checklist. (Not linked: that runbook is in `mkdocs.yml`'s `exclude_docs`, so a
link here would 404 for a reader of the published site.)

Of the `agents.*`/`tools.*` checks, two are `recommended` — reported, never
fatal, but they do mark the instance `degraded` — and both approval checks are
`info` lines that report as passes, so running autonomously carries no red mark
anywhere. `calendar.operator_calendar_writable` is `required`: an instance that
cannot write the operator's calendar will silently do the wrong thing every
time.

---

## Limits

`exec` caps what the MODEL sees at 4,000 characters of stdout and 2,000 of
stderr, and writes the whole stream to a file first — see [what you see of a
command's output](#exec-what-you-see-of-a-commands-output). Nothing below
changes that; this section is about the separate cap on what the DATABASE
keeps.

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

[`analyze_image`](#vision-view_image-and-analyze_image) is the other handler
that caps itself, and the reason its budget is 3,500 rather than a round
number: 200 rows of answers are tens of thousands of characters, so the result
keeps its totals and its first rows under this cap and writes the rest to a
file. What the agent reads is therefore also what the step row keeps —
including the per-image token and cost ledger, which a head-and-tail cut would
destroy.

## See also

* [Agents](enterprise/04-agents.md) — manifests, and what an agent is allowed
  to do.
* [Benchmark sandbox](runbooks/BENCHMARK_SANDBOX.md) — what a graded run may
  touch.
* `docs/agents/INSTRUCTION_CONTRACT.md` in the repository — what an agent's
  instruction file may say, tools included.
