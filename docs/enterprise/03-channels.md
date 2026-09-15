# 3. Channels

Goal: your people reach an agent from the place they already work, and nobody
else does.

A channel is both directions: a surface an agent can deliver to, and a surface
a person can drive it from. The second is an authorization question, so decide
the access mode before you tell anybody the bot exists.

| Channel | Shipped as | Needs |
|---|---|---|
| Slack | Core | A Slack app in your workspace |
| Microsoft Teams | The `genus-teams` plugin | An Azure bot registration and a public HTTPS endpoint |
| Web chat | Core, in the Helm | Nothing |
| Email | Core, outbound only | SMTP, or an authorised `gws` account |
| Telegram | Core | A bot token |

```bash
genus channel list
genus channel verify slack
genus channel add slack
```

`genus channel verify` proves a channel works step by step and exits 1 on a
failed step, 2 when the channel is not configured at all. Never take a
green dashboard for a working channel without it.

Credentials are never passed on a command line — a token there is
world-readable in `/proc` — and there is no API that writes a channel
credential. `genus channel add` prompts, or reads the channel's environment
variable. See [Secrets](05-secrets.md).

## Who may drive it

One gate, three modes, applied to every channel.

| Mode | An unknown sender gets | Use it when |
|---|---|---|
| `pairing` | One sentence and a six-character code. They reach no agent until an operator approves | The default for anything new, and the only mode that is safe on a surface strangers can reach |
| `allowlist` | Nothing, unless the channel's own membership test says yes | You already maintain a list of ids |
| `open` | A run, with no identity attached | A surface only trusted people can reach at all |

A value that is none of the three resolves to `pairing`: a typo must not be the
thing that opens a surface. A **known identity short-circuits every mode**, so
turning a channel to `pairing` is a decision about strangers, not a demand that
everybody already bound to it re-pair.

A code is only ever sent on a 1:1 surface — a code posted in a shared room is a
code anyone in the room can carry to you. In a Slack channel an unknown sender
gets silence, logged as a count; they learn nothing, not even that somebody is
listening.

## Slack, step by step

1. Create the app from the manifest this repository ships, so the scopes are
   the ones the channel actually uses, and install it to your workspace.
2. Store the bot and app tokens on the instance — `genus channel add slack`, or
   export `ROBOTHOR_SLACK_BOT_TOKEN` and `ROBOTHOR_SLACK_APP_TOKEN`.
3. Name the channel in `ROBOTHOR_CHANNELS` so the engine loads it.
4. Prove it: `genus channel verify slack`, which posts a real message to the
   verify target.
5. Set the access mode, and approve the first pairings.

The manifest, the exact scopes, the target formats and the full status table
are on the [Slack channel page](../channels/slack.md).

One compatibility clause worth knowing: an instance that already exports a
legacy Slack allowlist and has never set `ROBOTHOR_SLACK_ACCESS` keeps running
under `allowlist` and warns at start. Setting `ROBOTHOR_SLACK_ACCESS`
explicitly always wins, in either direction.

## Microsoft Teams

Teams is the plugin channel — the proof that a channel can ship outside the
core. Install `genus-teams`, then:

1. Register an Entra application and an Azure Bot. You need the application
   (client) id, a client secret and, for a single-tenant bot, the directory id.
2. **Decide the ingress.** Teams calls you; the Bot Framework will not reach a
   bot it cannot resolve over public HTTPS. That is your decision and it is not
   automatic — a reverse proxy, a tunnel, or an ingress with a real
   certificate.
3. `genus channel add teams --app-id <id> --tenant-id <directory-id>`, with the
   password in `ROBOTHOR_TEAMS_APP_PASSWORD`.
4. Name `teams` in `ROBOTHOR_CHANNELS`. The channel is inert until you do.
5. `genus channel verify teams`, and check the doctor's `teams.credentials` and
   `teams.endpoint`.

Every inbound request's Bot Framework token is validated — signature, issuer,
audience, expiry, and the service URL claim — and any failure is a 401 with an
empty body. Full walkthrough, including what the endpoint refuses and why Teams
cannot be addressed from a bare id, is on the
[Teams channel page](../channels/teams.md).

## Web chat, and email

**Web chat** needs no setup: it is a per-user session in the Helm, and delivery
targets are account ids. It is the right first channel for a pilot, because
nobody has to leave the instance's own authentication to use it. See
[Web chat](../channels/webchat.md).

**Email** is outbound only. Either the instance's authorised `gws` account
sends as itself, or you configure SMTP — host, port, from-address, and a
password in the environment. STARTTLS is not optional when there is a password.
Every send is checked against the tenant's do-not-contact list, and
`genus channel verify email --to alice@example.com` names the recipient every
time, because the email channel has no configured fallback address. See
[Email](../channels/email.md).

## Asking a question on each surface

An agent that needs an answer uses `ask_user`, and every channel implements it
in its own idiom: a message and a reply on Slack and Telegram, an Adaptive Card
on Teams, an inline prompt over SSE in web chat. Email cannot ask — it is
outbound only, so an agent that needs an answer must reach the person another
way.

A Teams card is bound to the conversation *and* the person it was addressed to,
and settles only for a matching answer. An empty addressee falls back to a
verified privileged identity, and "no answer" is a distinct outcome from any
answer — a question that timed out must not read as a "no".

## What gets recorded

Every delivery attempt records a `delivery_status`. A failure is recorded as
`failed:<channel>_<reason>`, and every such value has a row in that channel's
own page — an operator reading one out of `agent_runs` has nothing else to look
it up in.

Pairing and approval are audited: who approved, which code, which identity,
when. `GET /api/channels/{name}/identities` returns a sha256 **fingerprint** of
each native id rather than the id, because telling two bindings apart is what an
operator needs, and one borrowed session should not enumerate a whole
workspace.

The invariant underneath all of it: **a channel message may never approve a
pairing.** A message asking to be let in is exactly the message an attacker
sends. Approval happens in a shell or in the Helm, by somebody who is already
inside.

```bash
genus channel access list slack
genus channel access approve slack ABC234 --email alice@example.com
genus channel access approve slack ABC234 --email alice@example.com --role member
genus channel access deny slack ABC234
```

`--role` defaults to `viewer`, not `member`, and that is not stylistic:
`viewer` is search, get and list plus a deny-all, while `member` can be much
wider than its name suggests on a tenant whose policy you have not read. Check
`role_permissions` for your own tenant before you grant it. The whole model —
per-surface behaviour, the rate limits, the staleness window — is on
[Channel access](../channels/access.md).

Next: [Agents](04-agents.md) — giving those channels something worth talking
to.
