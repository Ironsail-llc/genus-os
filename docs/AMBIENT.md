# Ambient follow-ups

An agent can watch the conversations it already reads, notice the ones where a
person wrote and nobody answered, and propose what to do about them: nudge the
operator, send the operator a digest, or reply in the thread on our behalf.
Each kind of message is held to a tier the manifest chooses: sent alone,
sent only after a person approves it, or never proposed.

It is off unless two things are true: the instance sets `ROBOTHOR_AMBIENT`,
and the agent's manifest has an `ambient:` block.

## The manifest block

```yaml
ambient:
  watch: [inbox, stream:support]   # what to look at; nothing the agent cannot already read
  stalled_after: 4h                # how long a person may wait before the thread is stalled
  digest_at: "0 9,15 * * *"        # when the pass runs: a cron, or "09:00, 15:30"
  tiers:
    nudge_thread: approve          # auto | approve | never   (default approve)
    send_digest: auto              # default approve
    reply_on_behalf: never         # default never
```

| Key | Meaning |
|-----|---------|
| `watch` | `inbox` is the CRM conversation inbox: every open conversation, read page by page (up to 10,000), with incoming vs outgoing messages; private notes are ignored. `stream:<name>` is a Redis stream the agent already lists in `streams.read`; its events count when the payload carries `thread_id` and `direction` (`inbound` or `outbound`), with an optional `person`. Any other source, or a stream the agent does not read, is a manifest error. |
| `stalled_after` | A duration: `90s`, `30m`, `4h`, `1d`, `1h30m`. Required. |
| `digest_at` | A 5-field cron, or comma-separated `HH:MM` times, in the agent's schedule timezone. Required. One scheduler job per agent, `<agent>:ambient`. |
| `tiers` | One entry per action kind. Unknown kinds and values other than the three tiers are manifest errors. |

The action kinds:

| Kind | What is sent, and to whom |
|------|---------------------------|
| `nudge_thread` | A short note to the agent's delivery target (`delivery.channel` / `delivery.to`) that one thread is waiting. One per stalled thread. |
| `send_digest` | One message to the delivery target covering every stalled thread. |
| `reply_on_behalf` | A reply to the person who is waiting, in the thread itself. **Proposed and recorded, never sent yet:** the CRM inbox is a log of conversations held on other channels, so a row written there would reach nobody. Under `enforce` its outcome is `unsupported` until a reply can go out on the channel the conversation lives on. |

A thread is stalled when the first message from a person after our last reply
is at least `stalled_after` old. A second "any news?" does not restart the
clock; a reply from us stops it.

## What one pass does

1. Reads the watched sources off the event loop and finds the stalled threads.
   Nothing stalled means no run.
2. Composes with the agent's own run: its model and instructions, a `cron`
   trigger whose detail is `ambient:observe` or `ambient:enforce`, delivery
   off, and only the read-only tools it already had (the goal-writing tools the
   registry adds to every explicit list are denied). The composing run cannot
   send anything; the worker decides what goes out.
3. Asks only for the kinds whose tier is not `never`. A `never` kind the model
   writes anyway, a thread that is not stalled, an empty section, and output
   with no section headers at all are dropped. A composing run that did not
   complete proposes nothing.
4. Holds each proposal to its tier (below), then records it.

## `ROBOTHOR_AMBIENT`

| Rung | Behaviour |
|------|-----------|
| `off` (default) | No ambient job is registered and nothing runs. Any unrecognised value is `off`. |
| `observe` | Every proposal is recorded, with outcome `observed`, and **nothing is sent**: no channel message, no approval card, no reply in a thread. |
| `enforce` | `auto` sends. `approve` asks the approval gate first — the same prompt a guarded tool call raises: the Telegram inline keyboard, or the webchat approval card — and sends only on Approve. A pass asks all its approvals at once. A denial, a timeout (`v2.human_approval_timeout`) or no reachable approver sends nothing. |

It is a governed flag, so it moves from the dashboard's Controls page as well
as the environment, and `ROBOTHOR_DISABLE_ALL_RIPS=1` forces it off.

## Where the proposals show up

- **The Runs view.** Each proposal is a `checkpoint` step named
  `ambient_proposal` on the run that composed it. Its input says `kind`,
  `tier`, `target`, `mode` and how long the thread waited; its output carries
  the `text` and the `outcome` (`observed`, `sent`, `denied`, `no_approver`,
  `no_target`, `send_failed`, `unsupported`). The run detail renders these like any other
  step; the same rows come back from the run steps API.
- **A digest file** per agent per day, appended on every pass:
  `$ROBOTHOR_WORKSPACE/.robothor/ambient/<agent>/<YYYY-MM-DD>.md`.
- **The engine log**, one line per proposal: `ambient <agent> (<mode>): <kind> -> <target> [<tier>, <outcome>]`.
- **Approval cards** for `approve`-tier proposals under `enforce`. "Approve
  All" covers the rest of that kind in the same pass only; the next pass asks
  again, so one tap never turns an `approve` tier into `auto`.
- **The Automations page**, as its own `<agent>:ambient` card while the flag is
  on, with the job's last run and error count. The pass goes through the same
  circuit breaker and fleet admission as every scheduled run.

## Rolling it out

1. Add the block to one agent with conservative tiers (`approve`, `approve`,
   `never`).
2. Set `ROBOTHOR_AMBIENT=observe`. Read the digest files and the
   `ambient_proposal` steps for a week: are the stalled threads real, and is
   the text what you would send?
3. Set `enforce`. Move a kind to `auto` only once its observed proposals are
   ones you would have sent unread.

Roll back by setting `off`: the next reconcile removes the jobs.

## Not covered

Telegram, Slack and Teams history are not watch sources; only the CRM inbox and
Redis streams that carry `thread_id` and `direction`. The pass runs on the
ordinary scheduler path.
