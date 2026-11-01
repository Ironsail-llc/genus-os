# Web chat (the Helm)

The Helm is a built-in delivery channel. An agent whose manifest says
`delivery.channel: webchat` writes its output into one member's own chat session
and inbox — the same place their conversation with the agent already lives.

There is nothing to configure. Unlike Slack or Telegram there is no credential
an instance can be missing: the Helm is part of the platform, and the channel is
registered on every install.

```yaml
# docs/agents/weekly-digest.yaml
delivery:
  mode: announce
  channel: webchat
  to: 6f2c1b40-0c3e-4c1f-9c2a-000000000000   # a user_accounts.id
```

## Targets are `user_accounts.id`

`delivery.to` is the UUID of a **user account**, not an email address, a person
id, or a display name. It is the same id the Helm authenticates as, which is why
the delivery lands in the session that member actually has open.

A target that does not resolve to an *active* account in this tenant is refused
before anything is written — writing a turn into a session nobody can open is
worse than a recorded failure.

`genus user list` names this tenant's members and the email each account was
created with:

```bash
genus user list --tenant default
```

The id itself is the `user_accounts` row for that email — the account the
browser signs in as, not the `tenant_users` or `crm_people` row beside it:

<!-- doc-check: skip -->
```sql
SELECT id, email, role, status FROM user_accounts WHERE tenant_id = 'default';
```

## Both halves of a delivery

One send writes **two** rows, because a web-chat message has two halves and
either can fail on its own:

| Row | Table | Who sees it |
|-----|-------|-------------|
| The assistant turn | `chat_sessions` + `chat_messages` | The member whose session it is, with the Helm open — and the agent's next turn, as conversation history |
| The notification | `crm_agent_notifications` (`notification_type: info`, `metadata.kind: webchat_delivery`) | The same member, through their inbox, whether or not they were watching — plus owner/admin, who may read any inbox |

**Who can read the inbox half.** `GET /api/notifications/inbox/{id}` is
caller-scoped: owner and admin may read any inbox, and every other caller may
read only the one whose id is their own (403 otherwise). Its sibling
`GET /api/notifications` follows the same rule in the two shapes a query can
take — a non-operator naming somebody else's `toAgent` is refused, and one naming
nobody gets their own rather than the tenant's. Both arrived with this channel,
because it is the first writer to put member-private chat content in a table
whose reads had only ever been tenant-scoped. The notification carries no session
key for the same reason — `metadata.chat_message_id` points at the row that knows
its session instead.

So a receipt expects 2, and `delivered` means both landed. The statuses:

| `agent_runs.delivery_status` | Meaning |
|------------------------------|---------|
| `delivered` | Both rows written. The only value that sets `delivered_at`. |
| `failed:webchat_no_target` | The manifest named no `delivery.to`. |
| `failed:webchat_unexpanded_target` | `delivery.to` still contains `${…}` — an unexpanded variable, not an id. |
| `failed:webchat_unknown_user` | The target is not an active `user_accounts` row in this tenant. Nothing was written. |
| `failed:webchat_no_session_write` | The inbox notification landed; the chat turn did not. |
| `failed:webchat_no_notification` | The chat turn landed; the inbox notification did not. |
| `failed:webchat_send` | Neither row was written — nobody saw it. |

The two asymmetric statuses are separate names on purpose: "half of it arrived"
is a different problem with a different fix depending on *which* half, and a
status a query cannot match exactly is not a status.

## Which session a delivery lands in

Per-user sessions are on by default (`ROBOTHOR_PER_USER_SESSIONS`, see
`docs/runbooks/IDENTITY_ROLLOUT.md` in the repository for the ladder and the
escape hatch), so:

* a **member** gets `agent:{agent}:user:{user_accounts.id}` — the same key their
  own messages to that agent resolve to, derived by the one function both paths
  call (`chat.derive_user_session_key`);
* the **owner** keeps the shared `agent:main:primary`, which is the session the
  Telegram bot writes into too. A briefing reaches the operator in the
  conversation they already have rather than a second one beside it.

The turn is appended to the in-memory session as well as the database, because
`GET /chat/history` reads RAM and the database only at boot — without that, a
delivery would be invisible in the Helm until the next engine restart.

## Asking a question

`webchat` implements `Channel.ask`, so an agent that calls `ask_user` during a
Helm run now **waits** for the answer instead of recording the question and
moving on.

1. The `ask_user` tool writes the durable `agent_questions` row first, as it does
   for every surface.
2. The row's id and options go out over the run's own SSE stream as an
   `approval_required` event, and the Helm renders an answer card.
3. The browser answers through the bridge's existing
   `POST /api/approvals/question/{id}`, which settles the row.
4. The channel polls that row (every ~2s) until it is answered, and returns the
   answer to the agent mid-run.

**It waits only if somebody is listening.** Step 2 reports whether a sink
actually took the event. If nothing is reading that run's stream — no browser on
it — the channel refuses to wait at all and the tool answers `delivered: false`
with `reason: no_listener` and the sentence *"nobody was connected to receive the
question"*. Waiting the full tool budget on a prompt that reached no screen, and
then telling the model the person stayed silent, is a claim about something that
never happened; the row is still there for a later turn.

The row is the single source of truth, which is why there is no engine endpoint
for this: the CLI, Telegram and the Helm all settle the same row, and a second
answer path would be a second opinion about what the person said.

**Who may answer.** The bridge's approvals route is gated to an owner or
admin of the tenant the session acts in, so a member sees the card and gets an
honest refusal — "an operator has to answer this" — rather than a silent
failure. Letting the person
who was *asked* answer means binding the answerer to the row's target, which is
an ask-binding change and not part of this channel.

**Nobody answering is `None`, never an option.** If the deadline passes, the tool
reports that no answer came and names the row id; the row stays answerable, and a
late answer still reaches a later turn.

## Adding context while the agent is working

The composer stays open while a reply streams. What you type then goes to the
task that is already running instead of starting a second one, the same as on
Telegram (see [the Telegram channel](telegram.md#adding-context-while-the-agent-is-working)
for when the agent picks it up).

Under your message the Helm shows where it went:

| Note | Meaning |
|------|---------|
| *Added to the running task* | The running task has it and will read it at its next safe point. |
| *Queued — sends when this reply finishes* | Nothing running could take it: a plan is executing, the task had just finished, or it belongs to someone else. It is sent as your next message the moment the current reply ends. |

If the agent was already writing its answer when your message arrived, that
answer stays on screen as its own message and a revised one follows. A message
the task never got to is sent as your next turn automatically, so nothing you
type is lost. **Stop** ends the task and discards what you added to it.

For API clients: this is opt-in. `POST /chat/send` with `"join_running": true`
answers with one SSE event, `followup_joined` or `followup_queued`, and never
starts a run itself; without the flag the endpoint behaves as it always has. The
running turn's own stream adds two events: `interim` (a superseded answer) and
`pending_followups` (messages the turn never took, to send as the next turn).

## Attaching files

The paperclip at the left of the composer adds files to the message you are
about to send. You can also paste a screenshot into the box or drop files onto
the composer. Each file shows as a chip with its name and size, and the × on a
chip takes it off again. A message can be only files, with no text.

| | |
|---|---|
| Types | Images (PNG, JPG, GIF, WebP); text, Markdown, CSV, JSON and PDF — by type **and** extension |
| Largest file | **5 MB** |
| Files per message | **3** |

The composer states these limits and refuses a file that breaks one with a line
under the chips, keeping everything else you added. The dashboard's send route
and the engine check the same limits again: too many files or too large a file
is refused with `413`, an unsupported type or a body that is not valid base64
with `422`. A file that claims to be a picture but is not is also a `422`.

The file's extension is part of its type. It must be one of `.png`, `.jpg`,
`.jpeg`, `.gif`, `.webp`, `.txt`, `.md`, `.markdown`, `.csv`, `.json` or `.pdf`,
and name the same kind of file as the declared type: `tool.exe` sent as
`text/plain`, or `evil.png` sent as text, is refused as "not a supported type"
(`422`). A name with no extension (`README`, `.env`) is taken on its declared
type only if the bytes agree: an image's magic number, a PDF's `%PDF` header,
or no NUL byte for text. The route checks the name; the engine checks both. Every
refusal answers `request_admitted: false`: no turn started, and nothing was kept.

The whole request, three files base64-encoded plus the message, can reach about
21 MB. The send route runs behind the dashboard's proxy, and Next buffers a
proxied body only up to `experimental.proxyClientMaxBodySize` (10 MB unless set),
so `app/next.config.ts` sets it from the same constant the route reads
(`MAX_SEND_BODY_BYTES`). A body larger than that is refused before it is read
(`413`), as is one that grows past it while it streams or arrives shorter than
its `Content-Length`, and one that is not JSON is a `400`.

**Anything in front of the dashboard must admit the same.** A reverse proxy,
ingress or tunnel with a smaller body limit answers its own `413` before the
request reaches the dashboard; the Helm then says the message was too large to
send and names the limits, and no turn starts. Let it admit about 22 MB on
`POST /api/chat/send` (see the [production checklist](../deployment.md#production-checklist)).
A file the engine cannot write (a full disk, say) is refused the same way, and
whatever that turn had already written is removed.

Files go with an ordinary, fresh message. In Plan or Deep mode, and while a reply
streams, the paperclip is off and a paste or a drop is refused with a line saying
why; switching Plan or Deep on removes the files waiting in the composer, and
says which. Files cannot join a running task (`join_running` with attachments is
a `422`); send them as the next message.

Files land in the same inbox Telegram uses, one channel over:

```
<workspace>/inbox/webchat/<session>/<YYYY-MM-DD>/<sha256>-<safe-name>
<workspace>/inbox/webchat/<session>/<YYYY-MM-DD>/secret/<sha256>-<safe-name>
```

They are kept `0600` and pruned by the same retention sweep (see [the Telegram inbox](telegram.md#receiving-the-inbox)).
The agent's turn is your message first, then the same structured note Telegram
writes: each file's path, kind and size, `call view_image on <path>` for a
picture, and the extracted text of a text or PDF file. When the model the chat
runs on is known to accept images, a picture is also sent to it directly; when
it is not, or nobody has said, the note alone goes and the agent can look with
`view_image`. A text file whose bytes are not text (a NUL in the first block) is
named with its path but not quoted.

A file is stored once per session, day and name: the same bytes sent again under
the same name the same day reuse the earlier path, while the same bytes under
another name are a second file. The stored name is sanitised to one safe path
segment (`Résumé.txt` becomes `R-sum.txt`), so the row also keeps the name you
sent as `original_name`, and that is what the chip shows. The rows use the same
fields as Telegram's, `telegram_file_id` and `telegram_file_unique_id` included:
for a webchat file those hold `<request id>:<n>` and the sha256 of the bytes.

**A file named like a credentials file is quarantined.** The test is the
engine's secrets-path rule (`robothor/engine/secret_paths.py`): `.env`,
`credentials.json`, `id_rsa` and the other names it recognises are saved under
the day's `secret/` directory, and the note says the file was kept but not read.
A name ending in a documentation suffix (`.txt`, `.md`) is treated as notes
*about* a secret, not the secret, so `aws_credentials.txt` is kept and quoted
like any other text file. A quarantined file's contents never reach the
prompt, and `read_file` and `send_file` refuse it. This is the
Telegram rule, described in full in
[the Telegram channel](telegram.md#the-attachment-row).

The names stay with the conversation. The stored user turn records the files
beside the text (`extras.attachments` on `GET /chat/history`, `attachments` on
the persisted row), so a reload shows the same chips under your message.

## When `done` arrives

A reply's `done` event carries the final text, `run_id`, `status`, token counts
and `duration_ms`. By default it is sent after the run is fully finalized:
telemetry, post-run guardrail bookkeeping, claim verification and the run-row
write all come first. With `ROBOTHOR_EARLY_DONE=1` it is sent as soon as the
final text is settled, and that work finishes in the background under the
engine task registry, so `genus run` and engine shutdown still wait for it.
Telegram sends its reply at the same point.

A post-run policy that can change what you are shown still runs first: the
deliverable contract in `enforce` mode turns the reply into the failure notice
before `done` goes out. A failure in the background finalization is logged and
noted on the run's `error_message`; the reply you already have stands.

## The docked chat panel

Chat is the Helm's home view. On any other view the chat stays docked as a
panel on the right, so the conversation is one glance away. To give that view
the whole width, collapse the panel with the button at the right of its header
or with **Chat panel** at the foot of the sidebar. A collapsed panel leaves a
slim tab at the right edge; click it to bring the chat back.

The Helm remembers the choice in this browser, so a reload keeps the panel the
way you left it. The panel opens by default, and on a phone the chat is always
a view of its own.

## What the agent can reach

The wrench button beside **Plan** and **Deep** in the composer (the **Tools**
chip on a phone) opens a read-only list of what the agent in this chat can use:

- **MCP servers**, each with its health (**Ready** when the session is up and
  initialized, **Starting** while it is still handshaking, **Stopped** when it
  is not running) and how many tools it contributed. How the engine reaches a
  server (its command or URL) is never shown.
- **Tools**, counted by where they come from: core, each plugin, each MCP
  server, and the opt-in families. Open a group to see the names.
- **The manifest's lists**: how many tools it allows by name (or that it has no
  allow list, which means every tool that is not opt-in), and its deny list.

Nothing here turns a tool on or off. An agent's tools are set by its manifest,
so **Manage in agent settings** opens the agent editor for that agent
(`?v=agents&agent=<id>`), where `tools_allowed` is edited. `tools_denied` is
set in the manifest file itself. After a save the engine reconciles, and the
dialog shows the new list the next time it opens.

The list is operator information, so a member sees a sentence saying so. A
chat whose fleet listing names no default agent says it has no agent to list. The
data comes from two engine routes, `GET /api/admin/agents/{id}/tools` and
`GET /api/admin/mcp/servers`, which the bridge proxies as
`GET /api/agents/{id}/tools` and `GET /api/mcp/servers` for operators.

## `genus channel verify webchat` exits 2

Exit code 2 means "there was nothing to verify", and this channel declares no
`verify()`. That reads oddly for a channel that is always available, and it is
deliberate: a verify that upserted a `chat_sessions` row and rolled it back would
prove the database is up — which `genus doctor` already tells you — and nothing
at all about a member seeing a message. A check that proves nothing is worse than
an honest "not applicable".

`genus channel list` does report it, including whether the database is reachable
and how many sessions this process is holding:

```
  webchat      configured=True, ok=True, sessions=3
```
