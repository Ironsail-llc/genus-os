# Slack

Slack is a built-in delivery channel. An agent whose manifest says
`delivery.channel: slack` posts its output into a Slack conversation, and
`genus channel verify slack` proves the whole path works before you wait for a
scheduled run to tell you it does not.

Two halves, and they are independent:

| Half | What it does | What it needs |
|------|--------------|---------------|
| **Outbound** (the channel) | Posts agent output to a conversation | A bot token |
| **Inbound** (the Socket Mode bot) | Lets people talk to the main agent from Slack | A bot token **and** an app-level token |

They were not independent before: outbound delivery only existed once the
inbound listener had started, so an instance that merely wanted to post a daily
briefing had to run a socket connection to do it. If you only want briefings,
configure the bot token and stop.

## Set up the Slack app

1. Create an app at `api.slack.com/apps` → **From an app manifest**, and paste
   [`slack-app-manifest.json`](slack-app-manifest.json). It declares exactly the
   scopes this code calls and nothing else.
2. **Install to Workspace**. Copy the **Bot User OAuth Token** (`xoxb-…`) from
   *OAuth & Permissions*.
3. Only if you want the inbound half: *Basic Information* → **App-Level Tokens**
   → generate one with `connections:write`. That is the `xapp-…` token.
4. Invite the bot into the conversation you want it to post in (`/invite @your
   app`). A bot that is not a member cannot post, and the error says
   `not_in_channel`.

## Configure this instance

```bash
genus channel add slack
```

It prompts for each token without echoing it, and writes them to the instance
vault when this install has a master key, or to the 0600 `genus.env` file in
your workspace otherwise. It prints **where** it wrote and a SHA-256
fingerprint — never the value. You can pass `--bot-token` / `--app-token`
instead of being prompted, but the prompt is the safe path: a flag value lands
in your shell history.

Set the conversation `verify` and `genus doctor` aim at:

```bash
genus channel add slack --default-target C0000000000
```

That is a **Slack id**, not a `#name` — see *Targets* below. It is deliberately
not a fallback for delivery: an agent whose manifest names no target fails
loudly rather than having its briefing land wherever this happens to point.

Restart the engine after either, so it re-reads the credentials:

```bash
sudo systemctl restart robothor-engine
```

## Prove it works

```bash
genus channel verify slack
```

Four steps, in the order setups actually fail:

| Step | What a failure means |
|------|----------------------|
| `auth.test` | The bot token is wrong, revoked, or from another workspace |
| `conversations.list` | The app is missing an OAuth scope — the error names which one |
| `chat.postMessage` | The bot cannot post there: not invited, or the target is not an id |
| `apps.connections.open` | Socket Mode will not start, so the inbound bot answers nobody |

Exit codes, because this is a command scripts wrap: **0** every step passed,
**1** a step failed, **2** there was nothing to verify — an unknown channel
name, or Slack is not configured on this instance. Two is not a failure: an
instance that never wanted Slack has not failed a check it did not ask for.

`genus doctor` runs a shorter version of the same thing (`slack.token` for the
shape of each setting, `slack.verify` for `auth.test`). It stops short of
posting: a diagnostic that writes into your workspace every time a health panel
refreshes is not a diagnostic.

## Targets

`delivery.to` in an agent manifest, and `--target` here, take a Slack id:

| Form | Meaning |
|------|---------|
| `C0000000000` | A public channel |
| `G0000000000` | A private channel |
| `D0000000000` | An already-open DM conversation |
| `U0000000000` | A person — the channel opens a DM with them and posts there |

A `#name` is refused (`failed:slack_unresolved_target`). Resolving one means
walking `conversations.list` on every send, and the id is one click away: open
the conversation in Slack, **View channel details**, and it is at the bottom.

Long output is split at 4,000 characters, and chunks after the first are posted
as replies in the thread of the first — three loose 4,000-character posts in a
busy channel is not a briefing.

## What gets recorded

Delivery status is derived from what Slack acknowledged, never from the send
call returning:

| `agent_runs.delivery_status` | What happened |
|------------------------------|---------------|
| `delivered` | Every chunk posted |
| `partial:2/3` | Some chunks posted; the ids of those that did are kept |
| `failed:slack_send` | Nothing posted |
| `failed:slack_not_configured` | No bot token in the environment or the vault |
| `failed:slack_no_target` | The manifest names no `delivery.to` |
| `failed:slack_unexpanded_target` | A literal `${VAR}` reached the target |
| `failed:slack_unresolved_target` | The target is not a Slack id |

## Not here yet

* **Interactive approvals.** Block Kit buttons are the natural implementation
  and `SlackChannel.ask` refuses rather than returning a plausible default — an
  approval nobody gave is worse than a refusal.
* **Pairing and identity.** The inbound bot maps a sender to `slack:<user id>`
  without consulting the identity graph. Restrict who can reach it with
  `ROBOTHOR_SLACK_ALLOWED_USERS` / `ROBOTHOR_SLACK_ALLOWED_CHANNELS` — with
  neither set, anyone in a joined channel can drive the main agent, and the
  engine warns about that at start.
* **Threading from a manifest.** `send(..., thread=…)` exists; there is no
  manifest syntax for it.
