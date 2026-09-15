# Microsoft Teams

Teams is the first channel that ships as a **plugin** rather than as part of the
engine. Everything on this page is delivered by the `genus-teams` distribution;
the platform contributes the channel protocol, the access gate and the place the
endpoint is mounted, and nothing else.

An agent whose manifest says `delivery.channel: teams` posts its output into a
Teams conversation, and people you have paired can talk to your main agent from
Teams. `genus channel verify teams` proves the path before a scheduled run tells
you it does not work.

Two halves, and unlike Slack's they are **not** independent:

| Half | What it does | What it needs |
|------|--------------|---------------|
| **Outbound** (the channel) | Posts agent output into a conversation | An app id and a client secret — **and** a conversation somebody has already opened |
| **Inbound** (the messaging endpoint) | Lets people talk to the main agent | The same credentials, plus a public HTTPS route to one path |

The reason they are not independent is the one thing about Teams worth reading
before you start.

## Teams cannot be addressed from an id

Telegram has a chat id; Slack has a conversation id. Both are addresses: given
one, the bot can post. Teams has neither. A proactive message needs a
**conversation reference** — the tenant's own regional `serviceUrl` plus the
conversation id its platform minted — and both of those exist only because
somebody messaged the bot first and the inbound half wrote them down.

So:

* **someone must message the bot once** before anything can be delivered to
  them, including the pairing code that would let them drive an agent;
* a delivery to a target with no recorded reference is
  `failed:teams_no_conversation_reference` in `agent_runs`. It is loud on
  purpose. There is no fallback conversation and there will not be one: posting
  a briefing into whichever conversation the box happens to know about is
  exactly the failure `failed:telegram_unexpanded_chat_id` exists to prevent.

References live in `channel_conversation_refs`, one row per (tenant, channel,
sender). They are routing, not authorization — see *What gets recorded*.

## What you create in Azure

You need two objects and one route. The wizard makes the first two together.

1. **An Azure Bot resource** (portal → *Create a resource* → *Azure Bot*). Pick
   **Multi-tenant** unless you have a reason not to; single-tenant works and
   needs one extra value below. Creating it also creates the **Entra
   application** the bot authenticates as.
2. From that application: the **Application (client) ID**, a **client secret**
   (*Certificates & secrets* → *New client secret* — note the expiry, Azure caps
   it at two years and an expired secret is the most common way this channel
   stops working), and, only for a single-tenant bot, the **Directory (tenant)
   ID**.
3. On the bot resource, set the **Messaging endpoint** to
   `https://<your ingress host>/api/channels/teams/messages`.
4. **Channels** → add **Microsoft Teams**.
5. Publish the app to Teams with
   [`teams-app-manifest.json`](teams-app-manifest.json): replace `botId`/`id`
   with the application id and the developer block with your own, zip it with a
   192×192 `color.png` and a 32×32 transparent `outline.png`, and upload it in
   the Teams admin centre (or *Apps → Manage your apps → Upload a custom app*
   for a personal install).

No Graph API permissions are needed. This channel talks to the Bot Framework,
not to Graph.

### The ingress is your decision, and it is not automatic

The messaging endpoint must be reachable from Microsoft over public HTTPS. That
is one path — `/api/channels/teams/messages` — on the engine's API, and it
authenticates every request itself (see *What the endpoint refuses*).

On this platform's reference deployment the API sits behind **Cloudflare
Access**, which challenges every request with an email OTP. Microsoft's servers
cannot answer that challenge, so out of the box Teams gets an Access login page
and your bot appears dead. You must either:

* add a **Cloudflare Access bypass policy** for that exact path (not for the
  API, and not for `/api/channels/*`), or
* publish a **separate tunnel route / hostname** that maps only that path to the
  engine.

Either way, exactly one path becomes public. Nothing else on the engine's API
should be, and this channel does not need anything else to be.

## Configure this instance

```bash
genus plugin install genus-teams --sha256 <digest>
```

The install verdict is **`review`**, not `safe`, and that is expected: the
scanner flags a package that reaches off the box (it calls the Bot Framework)
and one that contributes to an "ambient" group (a channel runs without a tool
call). Read the reasons it prints, then accept with `--accept-review`.

```bash
genus channel add teams --app-id <application-id> --tenant-id <directory-id>
```

It prompts for the client secret without echoing it and writes it to the
instance vault when this install has a master key, or to the 0600 `genus.env`
file in your workspace otherwise. It prints **where** it wrote and a SHA-256
fingerprint — never the value. The application id and the directory id are not
secrets and go to `config.yaml`.

`--app-password` exists only to be **refused**. A credential on a command line
is readable by every account on the box through `ps` and `/proc/<pid>/cmdline`,
your shell has already written it to a history file, and nothing the command
does afterwards takes that back. For a script, export it instead:

```bash
export ROBOTHOR_TEAMS_APP_PASSWORD=...   # read from your secret store, not typed
genus channel add teams --app-id <application-id>
```

Then **arm** the channel. Installing the plugin does not arm it — an installed
channel is inert until you name it, because a package that became your delivery
surface merely by being installed could intercept every briefing:

```bash
genus config set channels.enabled teams     # ROBOTHOR_CHANNELS
sudo systemctl restart robothor-engine
```

| Setting | Default | What it does |
|---------|---------|--------------|
| `ROBOTHOR_TEAMS_APP_ID` | — | The Entra application (client) id. Also the audience every inbound token is checked against |
| `ROBOTHOR_TEAMS_APP_PASSWORD` | — | The client secret. Secret: vault or env, never `config.yaml` |
| `ROBOTHOR_TEAMS_TENANT_ID` | — | Directory id for a **single-tenant** bot. Empty means multi-tenant |
| `ROBOTHOR_TEAMS_ACCESS` | `pairing` | Who may drive the agent from Teams: `pairing`, `allowlist` or `open` |
| `ROBOTHOR_TEAMS_VERIFY_TARGET` | — | The conversation `verify` and the doctor aim at. Never a delivery fallback |
| `ROBOTHOR_CHANNELS` | — | Must name `teams`, or the channel stays inert |

### The two migrations

This channel adds `channel_conversation_refs` (121) and widens the
`agent_runs.trigger_type` CHECK (122). Look at the ledger before you apply
anything:

```bash
genus migrate --status
```

`genus migrate` applies **everything pending**, in prefix order. On an instance
that is behind — and instances are, routinely — that is a much larger change
than the one you are making, and it is not the change you are here to review. If
121 and 122 are the only two pending, run it; if they are not, apply the backlog
deliberately (`robothor.db.migrate.apply` takes a selector) and separately from
this channel's install.

Both are idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE UNIQUE INDEX IF NOT
EXISTS`, and 122's `DO $$ … IF EXISTS`), so re-running them costs nothing.

## Prove it works

```bash
genus doctor --only genus_teams_credentials
genus doctor --only genus_teams_endpoint
```

The first says what is configured and which layer it came from; the second says
whether the channel is armed and its endpoint mounted. Neither contacts
Microsoft, so both work on a box with no outbound network.

Then, once somebody has messaged the bot:

```bash
genus channel verify teams --target <directory-object-id>
```

Three steps, in the order setups fail:

| Step | What it proves |
|------|----------------|
| `credentials` | What is set, and whether it came from the environment, the vault or `config.yaml` |
| `oauth2.token` | Entra accepts the client credentials — the step an expired secret fails |
| `activity.typing` | A real activity reached a real conversation and Teams returned its id |

Exit 0 means every step passed, 1 that one failed, and 2 that this instance has
not configured Teams at all.

## Who may talk to it

`ROBOTHOR_TEAMS_ACCESS` decides, and it defaults to `pairing`:

* an unknown sender in a **1:1 chat** is answered with a one-shot code and
  reaches nothing until you approve it (`genus channel access approve teams
  <code>`);
* an unknown sender in a **channel, group chat or meeting** gets nothing at all
  — no code, no reply. A code posted in a room is a code anyone in the room can
  carry to you, and a stranger who guessed the bot's name should not even learn
  that somebody is listening.

Pairing binds the sender's **directory object id** (`aadObjectId`), which is the
person in your tenant's directory, not the per-installation id their Teams
client happens to be using today. Guests and anonymous meeting participants have
no directory id; they are paired on their platform id instead, and that binding
is worth fewer assumptions.

See [Channel access](access.md) for the approval commands, which are identical
for every channel.

## Targets

`delivery.to` takes either:

* a **directory object id** — the person; or
* a **conversation id** (`19:…@thread.v2`) — the room.

Both must already have a recorded reference. A name, an email address or an
`@mention` is not a target and will not be resolved into one: the bot has no
directory read permission and this channel is not getting one.

## What the endpoint refuses

Every request to `/api/channels/teams/messages`, in this order:

1. anything over 256 KB → **413**, refused while it is still being streamed;
2. anything past 120 requests a minute → **429**;
3. a token that is not a live Bot Framework token for **this** application:
   wrong signature, wrong audience, wrong issuer, expired, unsigned, or naming a
   key the Bot Framework does not publish → **401**;
4. a valid token whose signed `serviceurl` claim is not the `serviceUrl` in the
   activity → **401**. This is the check that stops a replayed activity from
   pointing the bot's own reply, bearer token attached, at somebody else's
   server.

Every 401 is identical and says nothing about which check failed: an error that
distinguished a bad signature from a wrong audience would tell whoever is
probing which half to keep working on.

A valid activity is acknowledged **200 immediately** and answered afterwards,
because Teams abandons the request after about 15 seconds and a real run takes
longer than that routinely. Everything else — recording the conversation, the
access gate, the run, the reply — happens after the acknowledgement. What is
left before it is a size-capped read and one signature check against a cached
key; the first activity after a restart also fetches Microsoft's published keys,
which is two requests bounded at five seconds each.

## Questions the agent asks you

`ask_user` over Teams sends an **Adaptive Card**: the options as buttons, or a
text box when there are none. It goes to the conversation the run came from, not
to wherever that person spoke most recently.

**Who may answer it.** The question is bound to the conversation *and* to the
person it was asked of, and an answer from anyone else is refused and logged —
which matters because everyone in a channel can see the card and press its
buttons. A question raised with no particular addressee (an escalation, which is
the operator's) falls back to the platform's own authorization: only a paired
identity holding a privileged role can settle it. Either way the sender is
re-checked against the access gate at the moment they answer, so somebody
revoked since the card went out cannot use it.

Nobody answering returns nothing; a timeout never picks an option for you.

## What gets recorded

| Where | What | Why |
|-------|------|-----|
| `channel_conversation_refs` | `serviceUrl`, conversation id, sender id, display name | The only way to reach somebody back. Routing, never authorization: the row has no role and no grant |
| `user_channel_identities` | The pairing you approved | What decides who may drive an agent |
| `agent_runs` | `trigger_type = channel` | So a Teams-driven run is accounted as one, not as Slack |

Nothing logs a message, a person's id, or any part of a credential. `health` and
`verify` report which *layer* a credential came from, never the value.

## Not here yet

Attachments, proactive conversation creation (somebody has to message the bot
first — see the top of this page), reading a Teams roster, and anything that
would need Graph API permissions.
