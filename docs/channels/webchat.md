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

**Who may answer.** The bridge's approvals route is operator-gated
(owner/admin), so a member sees the card and gets an honest refusal — "an
operator has to answer this" — rather than a silent failure. Letting the person
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
