# Channel access: who may drive this instance

A channel is a way for a person to reach the agent. That makes "who may use it"
an authorization question, and until this release each channel answered it
differently — or not at all.

- **Telegram** answered with a ladder inside `_resolve_user`: an unregistered
  sender in the operator's own chat was handed a fabricated `owner` identity, a
  group sender got `user`, and a private stranger got a refusal sentence.
- **Slack** answered with `_authorized`, which returned **true when neither
  allowlist was set**. An instance that had merely been pointed at a workspace
  let any member of it drive the main agent, with a startup warning as the only
  compensating control.

`robothor/engine/channels/access.py` is now the single gate both go through,
and it has three named modes.

| Mode | An unknown sender gets | Use it when |
|------|------------------------|-------------|
| `pairing` | One sentence and a six-character code. They reach no agent until an operator approves it | The default for anything new. The only mode that is safe on a surface strangers can reach |
| `allowlist` | Nothing, unless the channel's own membership test says yes | You already maintain a list of ids and want to keep maintaining it |
| `open` | A run, with no identity attached | A surface only trusted people can reach at all |

**On Telegram, `pairing` closes every surface** — a private chat, a group, and
the operator's own `default_chat_id` — so only senders with a `tenant_users` row
run. The one exception is `ROBOTHOR_ALLOW_UNREGISTERED_OWNER_FALLBACK=1`, which
keeps the operator out of locking themselves out of a fresh install. Under
`open`, Telegram's existing ladder is unchanged: an unregistered group sender is
still fabricated as `user` and an unregistered sender in the operator's chat as
`owner`, both governed by `ROBOTHOR_TELEGRAM_ROLE_GATES` rather than by this
setting.

### What an unknown sender sees, per surface

A code is only ever sent on a **1:1** surface — a code posted in a shared room is
a code anyone in the room can carry to you. What happens on the other surfaces is
not uniform, because the two channels do not have the same failure mode:

| Surface | Under `pairing`, an unknown sender gets |
|---------|------------------------------------------|
| Telegram private chat | The one-sentence reply and a code |
| Telegram group | The ordinary "not open for self-registration" refusal, on **every** message. No code |
| Slack DM | The one-sentence reply and a code |
| Slack channel or group | **Nothing.** Silence, logged as a count |

Telegram answers because its handler sends whatever
`_handle_unregistered_sender` returns, and `message.answer("")` is an API error —
so "say nothing" is not a thing that call site can express without changing
`telegram_handlers.py`, which is at its module-size cap. The refusal it sends is
the sentence that shipped before this gate existed, so this is the existing
closed-onboarding behaviour reaching one more surface rather than a new reply.

Slack is silent because its call site *can* be: `say` is only called when there
is something to say. That is the better behaviour of the two — an unknown sender
in a room learns nothing, not even that somebody is listening — and Telegram
should follow when the inbound pipeline moves behind `Channel.inbound_router`.

Neither is a per-message flood: the operator notification is rate-limited to once
per sender per hour, and the pairing reply itself to three per sender per hour.

A **known identity short-circuits every mode**, checked before the mode is even
read. Turning a channel to `pairing` is a decision about strangers, not a
re-decision about the people already bound to it — a gate that made everyone
re-pair would be one whose safe setting nobody dares turn on.

## Configuring it

| Setting | Default | Notes |
|---------|---------|-------|
| `ROBOTHOR_TELEGRAM_ACCESS` | `open` | **A compatibility default, not a recommendation.** Telegram's own resolution ladder and closed-onboarding refusal already decide who may run an agent; changing that as a side effect of shipping this gate would be a silent behaviour change on the one surface every instance already uses |
| `ROBOTHOR_SLACK_ACCESS` | `pairing` | One compatibility clause, and it turns on whether the mode was **configured**, not on what it resolves to: an instance with a legacy `ROBOTHOR_SLACK_ALLOWED_USERS`/`_CHANNELS` and **no** `ROBOTHOR_SLACK_ACCESS` keeps running under `allowlist`, and warns at start. Setting `ROBOTHOR_SLACK_ACCESS` always wins, in either direction — including setting it to `pairing` while an allowlist is still exported, which then ignores the allowlist entirely |
| `ROBOTHOR_CHANNEL_ACCESS_DEFAULT` | `pairing` | Any channel with no setting of its own — every plugin channel |

A value that is none of the three modes resolves to `pairing`. A typo must not
be the thing that opens a surface. An **empty** value is "unset" and takes that
field's own declared default, so blanking `ROBOTHOR_TELEGRAM_ACCESS` gives you
`open` and not `pairing`.

## Pairing, end to end

1. A stranger messages a `pairing` channel in a **1:1** conversation.
2. They get a six-character code from an alphabet with no `I`, `O`, `0` or `1`,
   and **nothing else** — no operator name, no instance name, no hint about who
   can approve it. The code lives ten minutes.
3. The operator sees it waiting and settles it.

```bash
genus channel access list slack
genus channel access approve slack ABC234 --user u-alice        # or --email alice@example.com
genus channel access approve slack ABC234 --user u-alice --role member
genus channel access deny    slack ABC234
genus channel access revoke  slack <identity-id>
```

Or from the Helm's **Settings › Channels** page, which drives these routes —
operator-gated and audited:

```
GET    /api/channels                                  the mode and pending count per channel
GET    /api/channels/{name}/pending
POST   /api/channels/{name}/pairings/{code}/approve   {"user_id"|"email", "role"}
POST   /api/channels/{name}/pairings/{code}/deny
GET    /api/channels/{name}/identities
DELETE /api/channels/{name}/identities/{id}
POST   /api/channels/{name}/verify                    {"target"?}
```

`GET /pending` returns neither the code nor the native id — only that somebody is
waiting, and when their code dies. That is why the Helm's pending list cannot
offer a one-click approve: the operator types the six characters the sender was
given, which is also the step where they check it is the person they think it is. `GET /identities` returns a sha256
**fingerprint** of each native id rather than the id itself: telling two bindings
apart is what an operator needs, and one leaked operator session should not be
able to enumerate a whole workspace. A malformed id is a 422, a code that is gone
is a 404, a binding that collides with an existing one is a 409, and a database
that is not answering is a 503 — never a 500.

### Roles: the default is `viewer`, because `member` is not narrow

`--role` accepts `member` or `viewer`, and **`viewer` is what you get by not
choosing**. That is not a stylistic default. The seeded `member` policy is
`("member", "*", "allow")` (`robothor/engine/permissions.py`), and migration 088
tightens it to read-only **for the `__default__` tenant only** — so on any other
tenant, a pairing "capped" at `member` granted every tool the fleet has.
`viewer`'s rows are `search_*` / `get_*` / `list_*` plus a deny-all, which is
what the word implies.

`member` is still grantable, by name: `--role member`, or `{"role": "member"}` on
the bridge route. Check `role_permissions` for your own tenant before you do.

### The one invariant

**A channel message may never approve a pairing.**

The attack is the shortest one there is: the stranger who was handed a code
sends `approve ABC234` back down the same wire, and something on the inbound
path spends it. So `approve_pairing` and `deny_pairing` take a mandatory
keyword `actor` and refuse any value that is not prefixed `operator:` (minted
by the bridge's operator gate) or `cli:` (a process with a shell on the box).
The inbound path holds no value either prefix accepts, so the refusal is
structural rather than a check somebody has to remember — and it is tested by
feeding the plausible message shapes back through the gate. A prefix alone is
not enough either: `operator:` with no id behind it is refused, so a null actor
id cannot land in `paired_by` as a row naming nobody.

**Where "structural" stops.** It is structural up to the `cli:` prefix, and
`cli:` means "a process on this box". An agent whose manifest `exec_allowlist`
admits `genus` could be talked by a channel message into running
`genus channel access approve`, and the DAL would accept it — the actor really
is a local process. Keep `genus` out of an agent's `exec_allowlist` unless you
intend that.

### The rest of the rules

- **Codes are never stored.** The row carries a sha256 digest, exactly as
  `user_sessions.refresh_token_hash` does for a refresh token. For its ten
  minutes a code *is* a credential, and a database dump must not carry a
  spendable one.
- **Single use is enforced in SQL**, by `UPDATE … WHERE used_at IS NULL …
  RETURNING`. The returned row is the authorization; two concurrent approvals
  cannot both get one.
- **A denied code can never be approved.** Denial spends the row rather than
  deleting it, and the denied sender gets **no new code** until the original
  would have expired — otherwise a denial is something a stranger undoes by
  sending another message, and the operator's pending list refills.
- **No codes on group surfaces.** A code posted in a shared room is a code
  anyone in the room can carry to the operator. The gate itself returns "send
  nothing" and logs a count; what the *channel* then does differs, and the table
  above says which — Slack sends nothing, Telegram falls back to its existing
  refusal sentence because its call site cannot send nothing.
- **At most three replies per sender per hour.** The code is idempotent inside
  its TTL, so this is not about minting — it is about not letting a script make
  the bot answer forever.
- **Pairing never grants a privileged role, and defaults to the narrow one.**
  `--role` accepts `member` or `viewer`, mirroring
  `accounts.JIT_PROVISIONABLE_ROLES`; omitting it gives **`viewer`**. A flow whose
  first step is "a stranger sent a message" does not end in an admin — and, see
  below, does not end in `member` either unless you say so.
- **No native ids or display names in logs.** The Slack line this replaced
  logged the raw user id on every refusal, which put a workspace's member ids
  into every log shipper the instance has.

## Where the rows live

`crm/migrations/118_user_channel_identities.sql` adds two tables, both with the
permissive-when-unbound RLS policy the rest of the platform uses.

| Table | Holds |
|-------|-------|
| `user_channel_identities` | `(tenant, channel, native_id) -> user_id`, plus the granted `role`, who granted it (`paired_by`) and when it was revoked. Live-row uniqueness is partial on `revoked_at IS NULL`, so revoking somebody does not bar that id from ever pairing again |
| `channel_pairing_codes` | The one-shot grant: `code_hash`, `expires_at`, `used_at`, `denied_at`. Live-row uniqueness is partial on unspent-and-undenied, so one sender has at most one live code |

**Not `contact_identifiers`.** That table is the CRM's rolodex — rows arrive
there by ingestion, not by a decision, and it has no `revoked_at` and no
`paired_by` because nothing there was ever granted. Reusing it would make "we
know who this person is" and "this person may operate the instance" the same
fact, and every mailing list the agent ever parsed would become an
authorization.

## The staleness window

Approve, deny and revoke are rows, written by whichever process ran the command.
The **engine** caches what it believes about a sender in its own process —
60 s in `identity.resolvers`, and **300 s** in `engine.users`, which is the one
Telegram reads — so a revoke written from the Helm or the shell would otherwise
take up to five minutes to bite.

So both callers finish by posting `/api/admin/identities/reload` to the engine
(`engine:control`), which drops both caches. It is **best effort**: the row is
already durable and the caches expire anyway, so an engine that is restarting
does not fail your approval — it just means the old window applies. If a revoke
seems not to have taken, the engine log says whether that call was refused.

A sender waiting on a pairing is cached as a miss for 5 s rather than 60 s, so an
approval takes effect on their next message instead of minting a second pending
code that arrives as a duplicate request.

## Resolution

`Channel.resolve_identity(native_id)` maps a sender to a Genus identity, or
`None` — which means *nobody is bound to this id*, and is what the gate acts on.
Both implementations delegate to
`robothor.identity.resolvers.resolve_identity`, so there is one cache and one
answer.

- **Telegram** keeps `tenant_users` as its source of truth: `lookup_user` reads
  that table and the whole inbound ladder is built on it, so a Telegram approval
  writes **both** the `tenant_users` row and the mirror in
  `user_channel_identities`.
- **Slack and every plugin channel** resolve out of `user_channel_identities`
  through `_resolve_generic`. A channel earns resolution by having rows, not by
  shipping code.
