# Configuration

This page is the hand-written tour: where a setting comes from, how to read and
change one, and what the install wizard wrote. It deliberately does **not** list
the variables.

!!! tip "The complete list is generated"
    Every `ROBOTHOR_*` and `GENUS_*` setting — with its type, default, what a
    change waits on, and whether it holds a credential — is rendered from
    the typed settings model into the
    [configuration reference](reference/configuration.md). A hand-written list
    is how variables ended up documented nowhere at all; this page stopped
    carrying one for that reason. `genus config schema` prints the same
    information as JSON Schema.

## Where a value comes from

Four layers, lowest priority first:

| Layer | Where it lives | Who writes it |
|-------|----------------|---------------|
| 1. Default | The typed settings model | The platform |
| 2. `config.yaml` | the `settings:` block of `<workspace>/.robothor/config.yaml` | `genus init`, `genus config set` |
| 3. Environment | the process environment — a systemd `EnvironmentFile`, a drop-in, `genus.env`, your shell | You, or the container |
| 4. Runtime | a `feature_flags` row, for **governed** flags only | `genus config set`, the dashboard |

`<workspace>` is `$ROBOTHOR_WORKSPACE`, or `~/robothor` when that is unset.

Two consequences worth internalising:

- **The environment beats the file.** A value you edited into `config.yaml`
  that is also set in the unit environment does nothing, and the running process
  is right. `genus config get` names the layer that won, which is the only
  reliable way to answer "what is this actually set to?" on a box with a
  drop-in, an env file and a config file.
- **A governed flag outranks everything.** The settings marked **governed** in
  the reference (`ROBOTHOR_RBAC_MODE` and most of the inventory in
  `infra/flags.yaml`) resolve from the flag store, which is live within
  seconds and needs no restart. That is deliberate: a guardrail you cannot
  turn off without a deploy is a guardrail nobody turns on. Not every flag in
  that inventory is governed — `ROBOTHOR_CONFIG_STRICT_MODE` deliberately is
  not, because it is read before a database exists, and neither is
  `ROBOTHOR_MANIFEST_SCHEMA_MODE`. Check the reference rather than assuming.
- **A governed flag written into `config.yaml` is never read.** The engine's
  flag reader falls back from the database row to the environment and stops
  there, while `genus config get` would report `config.yaml` as the winning
  layer. Set a governed flag with `genus config set`, which routes it to the
  store, or set it in the environment.

## Reading and changing settings

You should not have to grep `/etc` to answer "what is this set to?", and you
should not have to guess what a change needs restarting.

```bash
genus config get ROBOTHOR_MAX_CONCURRENT_AGENTS   # value + where it came from
genus config explain ROBOTHOR_RBAC_MODE           # everything declared about it
genus config list --group engine --changed        # what is off its default
genus config set ROBOTHOR_MAX_CONCURRENT_AGENTS 6
genus config schema                               # JSON Schema, for tooling
genus doctor                                      # is this instance working?
```

| Command | What it does |
|---------|--------------|
| `get NAME` | The effective value and its provenance: `runtime` (an operator-set `feature_flags` row), `env`, `config.yaml`, or `default`. |
| `set NAME VALUE` | Routes by the setting's own metadata — see below. |
| `explain NAME` | Description, group, type, default, deprecated aliases, secret/governed/restart flags, `since`, and the current provenance. |
| `list [--group G] [--changed]` | Every setting, or one group, or only what is not on its default. |
| `validate [--json]` | **Deprecated.** Runs [`genus doctor --offline`](deployment.md#diagnostics-genus-doctor) and says so on stderr. |

A misspelled name exits 2 and suggests the nearest real one, rather than
reporting a value for something nothing reads.

**`set` routes by what the setting is**, not by what you typed:

- a **governed flag** goes to the flag store and is live within seconds — no
  restart, no file edit. The value is validated against the flag's own declared
  rungs first;
- a **secret** is refused, naming `genus vault set`. Writing a credential into
  a config file is not a shortcut worth having;
- **everything else** is written into the `settings:` block of
  `$ROBOTHOR_WORKSPACE/.robothor/config.yaml`, atomically and without
  disturbing your comments, and the reply is either `applied` or
  `restart required: robothor-engine`.

A secret is never printed. `get` and `list` show `<set, sha256:ab12cd34>` —
enough to tell two boxes apart without putting the value on your screen.

### From the Helm, without a shell

Everything above is also a screen. **Settings › Config** renders the same
declarations `genus config list` prints — one collapsible section per group,
with a filter over names and descriptions — and saves a section's changed
fields in one all-or-nothing write. It obeys the same three routing rules as
`set`, because it calls the same code: a secret is shown as
`configured · sha256:ab12cd34` with no box to type in, a field the box's
environment supplies is read-only with the sentence explaining why a write
would apply to nothing, and a governed flag carries a link to the page that
says what it is actually doing. **Settings › Flags** is that page: every
governed flag, its rungs, and the verdict — what the control has done, and when
it last did it. A change there is live within seconds and needs no restart, and
it records the reason you type alongside it.

After a save that is *not* hot, Config shows a banner naming the units to
restart — `robothor-engine`, `robothor-bridge`, whatever the settings you
changed declare. Until you restart them the running services keep the values
they started with, and the file and the processes disagree. That banner is the
browser session's own memory of what it just saved: nothing on the instance
records it, so it is gone after a reload, and it is not the same thing as
`genus doctor --only config.pending_restart`, which reports the other case —
config.yaml being *ignored* because an environment variable overrides it.

### Hot or restart?

Every setting declares this, and both the reference table and
`genus config set` report the same answer from the same declaration:

| The setting is… | What a change needs |
|-----------------|---------------------|
| a governed flag | nothing — it applies live, through the flag store |
| declared `restart: no` | nothing — it is read each time it is used |
| declared `restart: next run` | the next invocation of the script or timer that reads it |
| anything else | the units it names restarted (usually `robothor-engine`, `robothor-bridge`) |

`genus doctor --only config.pending_restart` reports the one case that catches
people out: the file says one thing and the running process's environment says
another. It is a `recommended` finding, not a failure — the environment beating
the file is documented precedence, not a fault.

### `validate` is now an alias for `genus doctor`

Every question it used to ask is a doctor check — unknown keys, deprecated
names, a `settings:` value the running process disagrees with, a Telegram
credential that is not shaped like one — and the doctor asks a dozen more that
a fresh install actually fails on. Two things changed, and a script that reads
this command's output has to know both:

- **`--json` emits the doctor's payload**: `{status, summary, checks}`. The old
  document's `errors` and `pending_restart` keys are gone, so
  `genus config validate --json | jq '.errors'` now yields `null` — which reads
  as healthy and is not. Use `.summary.required_failed`, or the per-row
  `.checks[] | select(.status=="fail")`.
- **exit 1 means a *required* check failed.** A key nothing reads is a
  *recommended* finding and exits 0 while `ROBOTHOR_CONFIG_STRICT_MODE` is
  `observe` — under `enforce` the same key stops settings resolving at all and
  `config.settings_load` fails, which is required, so it exits 1 there.

The alias runs `--offline`: no model call, no Telegram round trip, no host
script. The command it replaces made no upstream call either, and a deprecation
is the last thing that should get more expensive. Run `genus doctor` for the
full report, including a live one-token model call.

## config.yaml and unknown keys

The `settings:` block mirrors the groups in the generated reference:

```yaml
settings:
  engine:
    max_concurrent_agents: 6
  flags:
    config_strict_mode: enforce
```

A key nothing reads is the quietest failure a config file has — the setting
simply never applies, and you read the default as your value.
`ROBOTHOR_CONFIG_STRICT_MODE` decides what happens to one:

| Rung | Behaviour |
|------|-----------|
| `off` | The key is ignored silently. |
| `observe` | **Default.** The key is ignored and logged once, naming it. What every install gets, new or upgraded — nothing writes `ROBOTHOR_CONFIG_STRICT_MODE`, so `enforce` is always an explicit act. |
| `enforce` | Resolving settings fails, naming the key. Recommended for new installs, and for any box where `genus doctor --only config.unknown_keys` reports none. |

The rung can be set in the environment or in the file it governs (as above), so
a new install can ship `enforce` without an environment file. An unrecognised
value falls back to `observe` rather than crashing — a typo in the strictness
knob must not be the thing that stops an instance booting.

## What the install wizard wrote

`genus init` does not scatter an instance's configuration across `/etc`. A
handful of files, each with one job:

| What | Where | Why there |
|------|-------|-----------|
| Operator identity | `~/.robothor/owner.yaml` | The file the platform reads. Never a `.env`: two files naming one operator is how an instance answers to one name and files CRM rows under another. |
| Settings — database host/port/name/user, provider model, Redis and Ollama endpoints, secrets backend, timezone, substrate | `<workspace>/.robothor/config.yaml` | Written through the same writer `genus config set` uses. The database **password** is never written here. |
| Sign-in secrets (`AUTH_SECRET`, `GENUS_BRIDGE_SSO_SECRET`) and, for the compose substrate, every other credential | `<workspace>/genus.env`, mode 0600 | One file the services read and nothing else may. `genus doctor` refuses to read it at any other mode. |
| Provider key | The instance vault, slot 1 | Secrets never go in `config.yaml`, which gets copied into bug reports. |
| Telegram bot token | `genus.env` on compose; **nowhere** on `local` | The channel and the doctor read `ROBOTHOR_TELEGRAM_BOT_TOKEN` from the environment, not the vault. `genus init --telegram-token` verifies the token and, on `local`, records only the bot's name. |
| Slack tokens | The instance vault (`channels/slack/bot_token`, `channels/slack/app_token`) when this install has a master key; `genus.env` otherwise | `genus channel add slack` prompts for each without echoing it and prints where it wrote (a token passed as a flag is refused, not stored). Every surface -- the daemon that starts the inbound bot, the outbound channel, `genus channel verify` and `genus doctor` -- resolves them through the one reader, so either place works and all four agree. [Setting Slack up](channels/slack.md). |
| Fleet model defaults | `<workspace>/docs/agents/_defaults.yaml` (instance data) | The probed model becomes the fleet primary, with up to two registry fallbacks — merged, never replacing a chain you wrote. |

`<workspace>/.env` holds exactly one variable, `ROBOTHOR_WORKSPACE`: a
`config.yaml` that lives inside the workspace cannot say where the workspace is.

## Secrets

### The rule: a value you hand the assistant wins over what the box booted with

The vault is the store you and the assistant manage. The environment — which on
a systemd instance is `/run/robothor/secrets.env`, decrypted at boot from a
root-owned SOPS file — is **bootstrap only**.

**Why.** On 2026-09-15 the operator handed the assistant a GitHub token over
Telegram and expected it to be kept, used and rotated by the assistant. It
could not be. The assistant could write the vault, but the accessor read the
environment first, so the expired `GH_TOKEN` the box had booted with shadowed
the fresh vault row — and clearing it needed root to edit the SOPS file and
restart the unit, which the assistant cannot do and must never need to. A dead
environment value must never silently shadow a live vault row.

So precedence depends on **what the credential is**:

| Class | Which store wins | What is in it |
|-------|------------------|---------------|
| **Application** | **vault**, then environment | Every third-party token: provider keys, `GITHUB_TOKEN`, channel tokens, SMTP passwords, webhook URLs. The credentials an assistant is handed, uses, proves and rotates. |
| **Bootstrap** | **environment**, then vault | `ROBOTHOR_DB_PASSWORD` (the vault's own rows live in that database), the test DSNs, `ROBOTHOR_REDIS_PASSWORD`, `GENUS_AUTH_SIGNING_KEY`, `AUTH_SECRET`, `GENUS_BRIDGE_SSO_SECRET`, `ROBOTHOR_INTENT_HMAC_SECRET`, the NATS transport, and `ROBOTHOR_VAULT_*` / `SOPS_*`. |

A name nobody declared is an **application** credential — the safe default,
because the tokens this rule exists for are exactly the ones the platform has
never heard of.

The set is a marker on the settings declaration itself (`declare(...,
secret=True, bootstrap=True)`), never a list maintained beside it. `genus
secrets status` prints the whole table, and `robothor.secrets.classification`
is where the rule lives.

**An unreadable vault still falls through to the environment.** Failing closed
would take every channel, provider and integration down with a vault outage,
for credentials a root-owned file is still holding good copies of.

### Handing the assistant a credential

Paste the token to your main agent. It will:

1. `vault_set` the credential — encrypted, and redacted out of the stored step
   and the transcript, so it does not survive in `agent_run_steps.tool_input`.
2. `vault_test` it — which dials the vendor's identity endpoint and answers
   `{ok, identity_hint, error_class}`. The identity hint is the vendor's own
   name for the account (a GitHub login, a Slack team), which is what catches a
   token that works but belongs to the wrong account.
3. Answer you with the fingerprint and the test result.

It will not echo the value back, and it will not write it to a memory block, a
note or a file. It also **cannot read it**: `vault_get` returns
`{key, configured, fingerprint, source, updated_at}` and never a value — the
same write-only shape the Helm Secrets page uses.

The write takes effect immediately. No restart, and nothing for you to do.

The vault tools are **operator-tier**. An operator grants the tier explicitly:

```yaml
id: main
v2:
  credentials: operator
```

Everything else is refused, including any sub-agent one of them spawns — a
spawned child runs under its own agent id, so its own manifest is what is
checked.

This is deliberately **not** the `role:` field. `role:` feeds RBAC, and setting
it to a value no `role_permissions` row seeds (`main`, `operator`) denies that
agent *every* tool under `ROBOTHOR_RBAC_MODE=enforce`. Two postures, two
fields; `ROBOTHOR_DEFAULT_SERVICE_ROLE` cannot grant the credential tier either.

### What a sub-agent sees

Nothing *inherited*, unless its own manifest says so. An agent's `exec` commands used to
inherit the engine's whole process environment — all ~50 credentials. The child
environment is now built from an allowlist: the process essentials (`PATH`,
`HOME`, `LANG`, `LC_*`, `TERM`, `TZ`, `TMPDIR`, `ROBOTHOR_WORKSPACE`,
`ROBOTHOR_AGENT_ID`) plus the `ROBOTHOR_*`/`GENUS_*` settings the model marks
non-secret.

An agent that genuinely needs a credential names it in its own manifest:

```yaml
id: devops
secrets:
  - GITHUB_TOKEN
```

Those names, and only those, are resolved through the accessor (vault first) and
injected into that agent's children. **Grants are never inherited** — a
sub-agent gets what its own manifest names, which is usually nothing. Bootstrap
names are refused however a manifest spells them, and a grant that resolves from
neither store becomes a note in the tool result rather than a silent absence.

**`gh` in particular.** With neither `GH_TOKEN` nor `GITHUB_TOKEN` in the child
environment, `gh` falls back to its own login file — so the agent acts as
whoever ran `gh auth login`, not as the instance. Grant `GITHUB_TOKEN` if you
want the instance's identity instead.

`ROBOTHOR_EXEC_ENV_MODE` is the ladder: `off` disables the scrub, `observe` (the
default for an existing install) changes nothing and logs per agent exactly what
`enforce` would withhold, `enforce` applies it. Read the observe lines, grant
what your agents actually need, then promote. New installs start at `enforce`.
Grants apply on every rung, so promoting is never what first gives an agent a
credential.

#### What the scrub is not

It removes **ambient inheritance**. It is not a process boundary, and it is
worth knowing exactly where the line is:

- The credentials are still in the **engine's own** environment. On Linux,
  `/proc/<pid>/environ` of a dumpable process is readable by any process of the
  same uid — and an `exec` child is one, with the engine as its parent. Genus
  sets `PR_SET_DUMPABLE=0` at startup so those entries become root-only, and
  `secret_paths` refuses `/proc/*/environ`, `set` and `declare -p`; the first is
  a real kernel boundary, the second is a denylist.
- **The remedy is to stop holding them there.** Run the migration and shrink the
  SOPS file (below): once the engine's environment carries only bootstrap
  credentials, that is all procfs can leak.
- **The boundary for an agent you do not trust is the sandbox.** `sandbox: docker`
  gives the container no host environment at all — which is also why a `secrets:`
  grant does not reach a sandboxed agent, and the tool result says so.

#### `gh` and other HOME-based logins

Taking `GH_TOKEN` out of the child does not make `gh` fail — it makes it fall
back to `~/.config/gh/hosts.yml`, so an ungranted agent would run as whoever ran
`gh auth login`. That is usually the operator personally, which is a *wider*
identity than the instance's token, not a narrower one.

So under `enforce` Genus points `GH_CONFIG_DIR` at an empty per-run directory
unless the agent is granted `GITHUB_TOKEN`: an ungranted agent's `gh` is logged
out, and a granted one acts as the instance. Reading the login files directly
(`gh auth token`, `cat ~/.config/gh/hosts.yml`, `~/.config/gcloud`) is refused
by `secret_paths`. Grant `GITHUB_TOKEN` to the agents that genuinely need
GitHub.

### Moving out of the environment

```bash
genus secrets status                      # what is where, and which store wins
genus secrets migrate --from-env --dry-run
genus secrets migrate --from-env
```

`migrate` copies every application credential the process environment holds into
the vault, refuses bootstrap names, and prints names and fingerprints only.
Afterwards, delete the migrated entries from the secrets file: until you do,
`genus doctor`'s `secrets.shadowed` check reports them, because a stale copy in
either store is a credential somebody will eventually read and a rotation
somebody will think they performed.

SOPS stays — as the bootstrap layer. See
[the SOPS runbook](runbooks/SOPS_BOOTSTRAP.md) for shrinking the file to just
that.

### The rules the platform enforces

1. **A credential never goes in `config.yaml`.** `genus config set` refuses a
   setting declared secret and names `genus vault set` instead.
2. **Read one through the accessor, not `os.environ`.**

    ```python
    from robothor.secrets import get_secret, resolve_secret

    get_secret("OPENROUTER_API_KEY")       # value, or None
    resolve_secret("OPENROUTER_API_KEY")   # (value, "env"|"vault"|"missing"|"unavailable")
    ```

    The accessor applies the precedence above, and treats an unreadable vault as
    "not configured" rather than raising — an engine that could not make an LLM
    call because its credential store is momentarily unreachable would be worse
    than no vault at all.
3. **Nothing prints a key.** Logs, alerts, tool results, the status table and
   `repr()` use a one-way fingerprint (`sha256:1a2b3c4d`, from
   `robothor.secrets.fingerprint`) rather than the last-four convention, which
   prints real key material.

### Where the values come from

The instance chooses one backend at install time (`genus init
--secrets-backend env|file|sops`), and `scripts/load-secrets.sh` is what
implements it. **SOPS is opt-in** — it used to be a prerequisite for starting at
all. The three backends, what each validates, and how a failure reaches a human
are in [Deployment § Secrets backends](deployment.md#secrets-backends).

### Provider keys, slot by slot

Five providers can hold credentials: `openrouter`, `anthropic`, `openai`,
`gemini`, `deepseek`. Each resolves **vault-first, then environment**, slot by
slot — a key written from the Settings page or the setup wizard wins the slot
it was written to, while a spare left in the shell keeps working. (Provider
slots have always worked this way; since 2026-09-15 every other application
credential does too.)

`vault.naming.vault_keys_for_env_name` is the one place the environment name
and the vault key are related in that direction: `OPENROUTER_API_KEY` finds
`providers/openrouter/api_key`, and a name with no richer spelling finds its own
lower-cased form. Both the accessor's search and `genus secrets migrate`'s
choice of where to write go through it, so a row the migration writes is a row
a reader finds.

| Slot | Vault key | Environment variable |
|------|-----------|----------------------|
| 1 (primary) | `providers/<id>/api_key` | `<PROVIDER>_API_KEY` |
| 2+ (spare) | `providers/<id>/api_key_<N>` | `<PROVIDER>_API_KEY_<N>` |

```bash
genus vault set providers/openrouter/api_key sk-or-v1-primary
genus vault set providers/openrouter/api_key_2 sk-or-v1-spare
```

**Spares must be contiguous.** The pool walks slots from 1 and stops at the
first empty one, so a key in slot 3 with slot 2 empty would be stored and never
dialled. `PUT /api/providers/{id}/keys/{n}` refuses that with a 409 naming the
slot to fill first, and `GET /api/providers` reports any pre-existing stranded
row as `state: "orphaned"`.

**When the engine reads the vault.** Once at startup (before any subsystem
runs, so direct-`os.environ` consumers see the key on the first turn), and again
on `POST /api/admin/secrets/reload` — what the Settings page calls after a save
— or on `SIGHUP`. Neither cancels in-flight work, so no restart is needed.
Between refreshes the values come from an in-memory snapshot: credential
resolution sits on the LLM hot path and must not open a database connection per
call.

Secrets are write-only end to end. `GET /api/providers` reports
`configured` for the provider and `{position, source, fingerprint, state,
updated_at}` for each slot — never a value — and the test connection's error text is scrubbed of every credential the
provider might have echoed back.

### Why spares matter

Every model in a fallback chain authenticates with the same provider key, so a
chain built on one credential is a chain of one link: on 2026-08-25 a single
capped `OPENROUTER_API_KEY` stopped the whole fleet, four cloud models and a
local tier notwithstanding.

| Behaviour | Rule |
|-----------|------|
| Order | Priority order — put the cheapest or highest-limit key first |
| Discovery | `_2`, `_3`, … up to `_16`; the walk **stops at the first gap** |
| Spend cap (HTTP 402) | Key sits out `900s`, then returns on its own — topping up needs no restart |
| Calendar-window quota (daily/weekly/monthly) | Key sits out **6 hours** (`ROBOTHOR_PERIODIC_QUOTA_COOLDOWN_SECONDS`). The short cooldown is wrong here: a weekly cap clears when the provider says so, and retrying it every 900s is ~96 revivals a day, each firing a fresh burst of 403s through every fallback chain — that retry loop was the 2026-08-27 outage |
| Rejected key (401) | Out for the life of the process; a revoked key never recovers |
| Model denied (403) | **No** rotation — OpenRouter answers 403 for "this key may not use *this model*", which is the model's problem, not the key's |
| Provider outage (5xx) | **No** key is retired — the credential was not the problem |
| Retry target | The **same** model, not the next one — a dead key is not a dead model |
| Nothing configured | No pool is built and litellm's own env lookup is used, exactly as before |

## Settings the generated reference cannot carry

The reference is rendered from the typed registry, so three kinds of variable
are missing from it by design — a provider's own credential names, what the
Next.js dashboard reads, and what configures the compose file. They are
collected here so the reference's absence is not read as their absence; several
also have a home of their own, named in the row:

| Variable | What it is |
|----------|------------|
| `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY` (+ `_2`…`_16`) | Provider credentials. They belong to the providers, not to this platform, so they keep the providers' names rather than being renamed into a Genus namespace |
| `BRAVE_SEARCH_API_KEY` | Brave Search API key. Set it and `web_search` prefers the Brave API; unset means the provider is absent — no call, no error |
| `AUTH_SECRET` | The dashboard's own Auth.js secret. Read by Next.js, not by the Python settings model. Required, with `GENUS_BRIDGE_SSO_SECRET`, before the dashboard reports ready — see [Deployment](deployment.md#docker-compose) |
| `REDIS_URL` | A full Redis URL, read by the **event bus** and the service registry only — `RedisConfig.url` in `robothor/config.py` is composed from `ROBOTHOR_REDIS_*` and never reads it. The hazard is the inverse of an override: set one and the other stays on its own default, so set both or neither |
| `EVENT_BUS_ENABLED`, `EVENT_BUS_MAXLEN` | The Redis Streams event bus: **on** by default, 10,000 entries per stream. Its own page is [Event Bus](event-bus.md) |
| `BRIDGE_URL`, `ORCHESTRATOR_URL`, `VISION_URL`, `SEARXNG_URL`, `HELM_URL`, `RTSP_URL` | Per-service base-URL overrides in the service registry, which outrank `robothor-services.json`. The MCP server reads `VISION_SERVICE_URL` for the same service — a separate name, and a real trap |
| `ROBOTHOR_MEMORY_GENERATION_PROVIDER`, `_REMOTE_MODEL`, `_MIN_INTERVAL_S` | Offload memory generation (fact extraction, episode summaries, insight discovery) from the local GPU to a remote provider. Embeddings and reranking always stay local; a remote failure falls back to local Ollama with a `MEMORY_GENERATION_REMOTE_FALLBACK` WARNING |
| `ROBOTHOR_DECLARED_TOOL_OUTAGES` | `tool:reason,tool:reason` — outages the operator has already decided about, so the tool-outage detector stops alerting on them |
| `GENUS_WORKSPACE`, `GENUS_ENV_FILE`, `GENUS_UID`, `GENUS_GID` | These configure the **compose file**, not the platform, so `genus config` does not know them. (`GENUS_IMAGE_TAG` looks like one of them but *is* declared, in the `substrate` group.) See [Deployment](deployment.md#docker-compose) |

### The log-unit allowlist has no variable

`GET /api/logs` will follow one unit per `robothor-*.service` file that
`scripts/install-units.sh` installs into `/etc/systemd/system` — falling back
to the `infra/systemd/` templates when none are installed (a checkout, or a
container). Nothing else: not `sshd`, not a template unit like
`robothor-alert@`, and not a name the caller invents. There is deliberately no
setting for it, because a configurable allowlist is one an operator can widen
to "everything" from a page that is meant to be read-only — add the unit to
`infra/systemd/` and install it, and the route follows it. `GET /api/logs/units`
is the live answer for any given box. See
[Deployment](deployment.md#the-helms-observe-pages).

## Channels

Which channels exist, and whether each one is actually set up, is visible over
the API as well as from `genus channel list` / `genus channel verify`. Two
operator-only routes on the Bridge, both proxying the engine — a channel is an
object in the engine process holding that process's credentials, so nothing
else can ask one anything:

| Route | What it answers |
|-------|-----------------|
| `GET /api/channels` | Every channel a manifest's `delivery.channel` could resolve to: whether it is built in, whether it reports itself configured, its (redacted) health report, whether it can be verified, the access mode in force (`pairing` / `allowlist` / `open`) and how many senders are waiting on a pairing decision. A `pending_pairings` of `null` means the pairing rows could not be read, which is not the same claim as `0`. |
| `POST /api/channels/{name}/verify` | Runs the channel's own `verify()` and returns each step. A channel that declares none gets `steps: []` and `verify_available: false` — never a fabricated pass; one that hangs or raises gets `configured: null` and an `error_class`, because a pass nobody observed is not a pass. Aimed with an optional `{"target": "..."}`, and it may really send a message. |

Neither route writes a credential, and neither returns one: a token, a chat id
or anything else secret-shaped in a health report comes back as a `sha256:`
fingerprint. **Adding a channel's token is still `genus channel add` on the
box** (and `genus init --telegram-token` for Telegram) — there is no API that
writes channel credentials, by design.

## Plugins

`ROBOTHOR_PLUGIN_LOCKFILE` points at the plugin lockfile — the record of which
installed distributions the operator accepted, what each one's manifest looked
like when it was recorded, and which are disabled. Empty (the default) means
`<workspace>/.robothor/plugins.lock`, beside `config.yaml`, and the file is
written mode `0600`. **Give it an absolute path.** A relative value resolves
against that same config directory rather than the working directory, because
the engine runs under systemd's `WorkingDirectory` and you run `genus plugin
disable` from wherever you happen to be standing — a CWD-relative path would
mean the two read different files and the disable would never reach the daemon.

Three things follow from it, and none of them is on until `genus plugin sync`
has run once: an installed plugin can be turned off without uninstalling it, a
manifest that changes underneath the engine is refused rather than imported,
and `genus doctor` can tell you which of your plugins stopped loading. A
distribution with no row loads exactly as it did before the lockfile existed,
and a lockfile that does not parse is ignored rather than fatal.

`ROBOTHOR_PLUGIN_MANIFEST_MODE` and `ROBOTHOR_PLUGIN_MANIFEST_ENABLED` are the
separate, older ladder that decides whether a distribution shipping no
`genus-plugin.yaml` is refused before import. Both, the verbs, the doctor
checks and the four HTTP routes are in [Plugins](PLUGINS.md#disabling-and-the-lockfile).

## Authentication

Three sign-in methods, and a deployment needs at least one. Local
email+password exists so a five-minute install does not have to stand up an
identity provider first; OIDC and Cloudflare Access are unchanged and can run
alongside it. The wizard turns local login on when it creates your account —
you do not set `GENUS_LOCAL_LOGIN` yourself — and the dashboard asks the bridge
which methods are live rather than reading its own environment. Names, defaults
and restart requirements for all of them are in the
[`auth` group of the reference](reference/configuration.md).

What the settings do not tell you:

- Passwords are argon2id, minimum 12 characters (`MIN_PASSWORD_LENGTH` in
  `robothor/auth/local_login.py`; the hashing itself is
  `robothor/auth/passwords.py`).
- Every failure — unknown email, wrong password, disabled account, locked
  account — answers the same `invalid credentials`. The only distinguishable
  state is `mfa_required`, and only after a correct password.
- Ten consecutive failures freeze the account for 15 minutes; five attempts per
  (email, IP) per minute are allowed before a 429. On top of that, a coarse
  ceiling nothing in the request can change: 30 credential attempts per minute
  per connecting peer and 300 per minute for the whole Bridge process, checked
  before any account is loaded, so a spray of fabricated addresses cannot spend
  one 64 MiB argon2 hash per request. A credential body over 8 KiB is refused
  with 413 before it is parsed.
- TOTP codes are single-use: the accepted time step is recorded, so a code
  cannot be replayed inside the verifier's ±1 step window (RFC 6238 §5.2).
  Rotating `GENUS_AUTH_SIGNING_KEY` invalidates every enrolled second factor,
  because it derives the key that encrypts stored TOTP secrets — recover with
  `genus user mfa-reset`.
- Changing a password revokes every other refresh session, keeping only the one
  that made the change.
- **Owner MFA is mandatory whenever local login is on.** The owner still signs
  in, but the dashboard shows a banner that cannot be dismissed until a factor
  is enrolled at `/account/security`. It does not matter what else is
  configured: `GENUS_OIDC_ISSUERS` is the Bridge's issuer allowlist, not a
  sign-in method, and the `CF_ACCESS_*` variables are dashboard-only, so
  neither can answer "is there another way in". Set
  `GENUS_OWNER_MFA_REQUIRED=false` to opt out explicitly.

### Users & roles over the API

`genus user add` / `genus user list` are not the only way to administer
accounts any more. Six operator-only Bridge routes, all scoped to the caller's
own tenant — an account in another tenant answers 404, never 403. The Helm's
**Settings › Users & roles** page is built on exactly these, and adds nothing of
its own: it shows an SSO grant's expiry and issuer once, because a binding grant
carries no secret to re-read — the first verified sign-in for that address spends
it. No mail is sent by any of this; the operator passes the invitation on.

| Route | What it does |
|-------|--------------|
| `GET /api/auth/roles` | The roles an account may hold, with one sentence each. Served from `robothor.auth.tokens.HUMAN_ROLES`, which `genus user` and the token layer now share rather than each keeping a copy. |
| `GET /api/users` | Every account in the tenant: id, address, display name, role, status, whether it is bound to an identity provider, whether MFA is enrolled, last sign-in. Never a password hash, an MFA secret or an IdP subject. |
| `POST /api/users` | Creates the `user_accounts` row — **narrower than `genus user add`**, which also writes the CRM person, the tenant membership and the channel identifiers. `{"sso": true}` also arms a one-shot SSO binding grant and returns its id, expiry and issuer pin. |
| `PATCH /api/users/{id}` | Role, display name, `status: active\|disabled`. |
| `POST /api/users/{id}/binding-grant` | Arms a fresh one-hour binding grant for an existing account, pinned to the configured issuer when the appliance has exactly one. |
| `GET /api/users/{id}/binding-grants` | Every grant armed for that account, live or spent. |

**The owner role is not an ordinary role here.** Only an owner may grant or
remove it, only an owner may demote, disable or arm a binding grant on the
owner account (403 otherwise), and the tenant's only owner cannot be demoted or
disabled at all (409) — whatever its status, because an `invited` or `disabled`
owner still occupies migration 071's single owner slot. No caller may demote or
disable *themselves* (409) either: the session keeps its old claims until the
token expires. Disabling revokes that account's live sessions — without it a
refresh token keeps working for up to thirty days. Changing the owner is
`genus user` on the box.

Not here, and still `genus user` on the box: setting a password, resetting a
second factor, and deleting an account. Nothing on this surface sends mail, so
an invited person has to be told out of band.

### The forwarded client address needs two allowlists to agree

The dashboard reads `X-Forwarded-For`, walks it from the **right**, and forwards
the first hop not named in `GENUS_DASHBOARD_TRUSTED_PROXIES` as `X-Client-IP`;
the Bridge honours that header only from a peer named in
`GENUS_TRUSTED_PROXIES`. Either one empty means no forwarded address, and the
Bridge uses its peer — safe, but then the sign-in limiter sees one address for
every user.

The left-most `X-Forwarded-For` entry is the one a client can write — every
appending proxy produces `<what the client sent>, <the real client>` — so
reading it would let a caller choose its own limiter key and write its own
address into the audit trail and `user_sessions.ip`.

Loopback is **not** implicitly trusted: a tunnel on the same host (a reverse
proxy, an SSH forward) makes every remote client a loopback peer. Set
`GENUS_TRUSTED_PROXIES=127.0.0.1/32` when the dashboard and the Bridge share a
host — but only when nothing else can reach that loopback port, because that one
line grants every client behind such a tunnel the `X-Client-IP` assertion.
Prefer a `/32` over a pod CIDR, which covers every workload in the namespace.

Operator commands:

```bash
genus user set-password alice@example.com      # prompts twice, no echo
genus user set-password alice@example.com --password-stdin < secret
genus user mfa-reset alice@example.com         # clears a lost authenticator
```

## Agent manifests

`ROBOTHOR_MANIFEST_SCHEMA_MODE` validates each manifest against
`robothor/engine/schema/agent_manifest.yaml` at load, on a three-rung ladder:
`off` skips validation, `observe` (the default) logs each error with the agent,
path and code and counts it in
`robothor_manifest_schema_would_reject_total`, and `enforce` refuses the
manifest and reports the agent **broken** — never absent, so the scheduler will
not prune its schedules.

Only `error`-severity findings block under `enforce`. Advisory findings — a
model that is not in the registry, an unknown guardrail name, a key sitting in
the wrong block — stay warnings on every rung, because each describes a
manifest that loads and runs.

Validation runs on the **merged** manifest: the file plus `_defaults.yaml`,
exactly the layers a run merges. A typo in the fleet defaults therefore marks
every agent that inherits it broken, rather than passing a per-file check and
failing at run time.

Under `enforce`, `/ready` returns `503` with `checks.fleet: "error:broken:<n>"`
and a `broken_agents` list of ids. An agent reported **broken** needs its
manifest fixed; an agent reported **missing** needs re-creating. The two are
never collapsed.

Unknown keys are a warning at load and an error only under `strict`. The engine
itself never loads a manifest that way — a live instance may fairly carry a
field a plugin reads — but two things do run `strict`: `genus doctor`'s
`manifests.schema_warnings` check, against the fleet in the configured
manifest directory, and the repository's own template test, against every
bundle under `templates/agents/`. So an unknown key is a recommended doctor
finding on a live box and a hard failure in CI for a shipped template.

## Web fetch and web search

`web_fetch` resolves a host, refuses any private, loopback, link-local or
carrier-internal (CGNAT) address in either IP family, and then connects to the
exact address it vetted — pinned at the socket, so the request keeps its
hostname and Host, TLS SNI and certificate verification all see the real name.
Certificate verification is never disabled; an untrusted certificate is
reported as such. Redirects are followed one hop at a time and re-vetted at
every hop, up to 5 real hops (the canonical `http→https` and `apex↔www` bounces
do not count). `ROBOTHOR_WEB_FETCH_USER_AGENT` is how the tool names itself —
sites that block HTTP-library defaults answer 403 to `python-httpx/*`.

**For web search, from a residential or datacenter IP, most scraped engines
block you.** The Brave Search API is the supported reliable path
(`BRAVE_SEARCH_API_KEY`); the chain below is the best effort for an instance
without a key.

Brave's free plan is 2,000 queries per 30 days at one per second, and every
reply carries the remaining quota. The engine reads it: a per-second limit is
retried after the interval Brave names; a spent month is **not** retried — the
tool skips Brave until the window resets and says so in its result
(`brave_skipped`, `brave_quota`). A Brave answer carries `brave_quota` once
fewer than a tenth of the month remains, and the `search_quota_detector` pages
a warning at that point and a critical alert when the month is gone. The fix
in either case is a second provider key or a paid plan.

The `web_search` tool grades the answer it gets back. It scores results by the
*rare* terms of the query — a term carried by nearly every result is what the
results have in common, not evidence they answer the question — and always
requires a place or number the operator named to appear. When SearXNG errors,
reports its general engines unresponsive, or comes back failing that grade, the
search re-runs through the `browser` provider: the engine's own browser loads a
real results page **in a background tab**, never moving the page an agent is
working on. It advances to the next source when one returns too few rows *or*
when the query named a place and the rows never mention it — one engine pins a
local query to the egress IP's own city, another honours the words that were
typed — and the rows that name the place lead the answer, with `place_source`
saying which source supplied them. The result then carries
`"provider": "browser"`, `"fallback_from": "searxng"`, a `fallback_reason`,
`sources` (every source tried, in order), and the unresponsive-engine list.

`fallback_reason` values: `low_relevance`, `missing_place_terms`,
`engines_unresponsive`, `error`, `browser_low_relevance` (the browser answer
missed too), `browser_parse_empty` (page loaded, nothing parsed — the result
selectors have moved), `browser_blocked` (challenge/interstitial),
`browser_timeout`, `browser_denied` (the run may not use the browser tool),
`browser_unavailable`, `browser_fallback_disabled`.

A local-looking query ("… near X", "… in Springfield", a postcode) also gets up
to five OpenStreetMap rows under `places`: for a mapped category the place is
geocoded once with Nominatim — using exactly the words the query gave, never an
inferred city or state — and the amenities around it come from Overpass;
anything else falls back to a free-text Nominatim lookup. Both APIs share one
≤1 request/second courtesy budget. When the local path runs and finds nothing,
the result says why in `places_reason`: `geocode_failed:<place>`,
`overpass_empty`, `overpass_error`, `category_unmapped`, or
`place_not_in_query`.

An agent can force one provider with the tool's `provider` argument
(`auto`, `searxng`, `browser`, `brave`, `perplexity`); `auto`, or omitting it,
runs the chain above. `ROBOTHOR_WEB_SEARCH_BROWSER_FALLBACK=off` disables the *implicit*
browser fallback (it drives a headed browser on the operator's display); an
explicit `provider="browser"` still runs.

## Failure-mode detectors

`ROBOTHOR_DETECTORS_ENABLED=0` disables every detector; what each one watches
is in `docs/OBSERVABILITY.md` (repository only), under failure-mode detectors.
`ROBOTHOR_RECORD_ASSISTANT_TURNS` persists each assistant turn onto its
`agent_run_steps` row so a finished run's reasoning can be read back. It is off
by default and should stay off unless you need it: a turn contains whatever the
agent was reasoning about, which on a real instance is the operator's own data.

## Reading settings from Python

```python
from robothor.config import get_config

cfg = get_config()
print(cfg.db.dsn)           # "dbname=robothor_memory port=5432 user=..." —
                            # host= is omitted entirely when the host is
                            # empty, which is how libpq is told to use the
                            # Unix socket
print(cfg.redis.url)        # "redis://127.0.0.1:6379/0"
print(cfg.ollama.base_url)  # "http://127.0.0.1:11434"
print(cfg.workspace)        # Path("<workspace>")
```

The config is a singleton. Call `reset_config()` in tests to reload from the
environment.
