# Configuration

This page is the hand-written tour: where a setting comes from, how to read and
change one, and what the install wizard wrote. It deliberately does **not** list
the variables.

!!! tip "The complete list is generated"
    Every `ROBOTHOR_*` and `GENUS_*` setting — with its type, default, what a
    change waits on, and whether it holds a credential — is rendered from
    the typed settings model into the
    [configuration reference](reference/configuration.md). A hand-written list
    is how 91 of the platform's variables ended up documented nowhere; this page
    stopped carrying one for that reason. `genus config schema` prints the same
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
- **A governed flag outranks everything.** Guardrails and feature gates
  (`ROBOTHOR_RBAC_MODE` and the rest of the inventory in `infra/flags.yaml`)
  resolve from the flag store, which is live within seconds and needs no
  restart. That is deliberate: a guardrail you cannot turn off without a
  deploy is a guardrail nobody turns on.

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
| `observe` | **Default.** The key is ignored and logged once, naming it. What every existing install gets on upgrade. |
| `enforce` | Resolving settings fails, naming the key. Recommended for new installs, and for any box where `genus doctor --only config.unknown_keys` reports none. |

The rung can be set in the environment or in the file it governs (as above), so
a new install can ship `enforce` without an environment file. An unrecognised
value falls back to `observe` rather than crashing — a typo in the strictness
knob must not be the thing that stops an instance booting.

## What the install wizard wrote

`genus init` does not scatter an instance's configuration across `/etc`. Five
files, each with one job:

| What | Where | Why there |
|------|-------|-----------|
| Operator identity | `~/.robothor/owner.yaml` | The file the platform reads. Never a `.env`: two files naming one operator is how an instance answers to one name and files CRM rows under another. |
| Settings — database host/port/name/user, provider model, Redis and Ollama endpoints, secrets backend, timezone, substrate | `<workspace>/.robothor/config.yaml` | Written through the same writer `genus config set` uses. The database **password** is never written here. |
| Sign-in secrets (`AUTH_SECRET`, `GENUS_BRIDGE_SSO_SECRET`) and, for the compose substrate, every other credential | `<workspace>/genus.env`, mode 0600 | One file the services read and nothing else may. `genus doctor` refuses to read it at any other mode. |
| Provider key, Telegram token | The instance vault | Secrets never go in `config.yaml`, which gets copied into bug reports. |
| Fleet model defaults | `<workspace>/docs/agents/_defaults.yaml` (instance data) | The probed model becomes the fleet primary, with up to two registry fallbacks — merged, never replacing a chain you wrote. |

`<workspace>/.env` holds exactly one variable, `ROBOTHOR_WORKSPACE`: a
`config.yaml` that lives inside the workspace cannot say where the workspace is.

## Secrets

Three rules, and the platform enforces all three rather than documenting them:

1. **A credential never goes in `config.yaml`.** `genus config set` refuses a
   setting declared secret and names `genus vault set` instead.
2. **Read one through the accessor, not `os.environ`.**

    ```python
    from robothor.secrets import get_secret, secret_source

    get_secret("OPENROUTER_API_KEY")      # value, or None
    secret_source("OPENROUTER_API_KEY")   # "env" | "vault" | "missing" — safe to print
    ```

    It walks the process environment (whatever the backend put there) and then
    the encrypted vault, and treats an unreadable vault as "not configured"
    rather than raising. An instance whose vault has no master key resolves from
    the environment only and says so once at INFO — an engine that could not
    make an LLM call because its credential store is empty would be worse than
    no vault at all.
3. **Nothing prints a key.** Logs, alerts and `repr()` use a one-way
   fingerprint (`key-1a2b3c4d`) rather than the last-four convention, which
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
it was written to, while a spare left in the shell keeps working.

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
`{configured, source, fingerprint, state, updated_at}` per slot and never a
value, and the test connection's error text is scrubbed of every credential the
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
| Rejected key (401) | Out for the life of the process; a revoked key never recovers |
| Model denied (403) | **No** rotation — OpenRouter answers 403 for "this key may not use *this model*", which is the model's problem, not the key's |
| Provider outage (5xx) | **No** key is retired — the credential was not the problem |
| Retry target | The **same** model, not the next one — a dead key is not a dead model |
| Nothing configured | No pool is built and litellm's own env lookup is used, exactly as before |

## Settings the generated reference cannot carry

The reference is rendered from the typed registry, so three kinds of variable
are missing from it by design. They are listed here because otherwise they are
documented nowhere:

| Variable | What it is |
|----------|------------|
| `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY` (+ `_2`…`_16`) | Provider credentials. They belong to the providers, not to this platform, so they keep the providers' names rather than being renamed into a Genus namespace |
| `BRAVE_SEARCH_API_KEY` | Brave Search API key. Set it and `web_search` prefers the Brave API; unset means the provider is absent — no call, no error |
| `AUTH_SECRET` | The dashboard's own Auth.js secret. Read by Next.js, not by the Python settings model. Required, with `GENUS_BRIDGE_SSO_SECRET`, before the dashboard reports ready |
| `REDIS_URL` | A full Redis URL that overrides the host/port/db settings, for a managed Redis with credentials in the URL |
| `EVENT_BUS_ENABLED`, `EVENT_BUS_MAXLEN` | The Redis Streams event bus: on by default, 10,000 entries per stream |
| `BRIDGE_URL`, `ORCHESTRATOR_URL`, `VISION_URL`, `SEARXNG_URL` | Per-service base-URL overrides that outrank `robothor-services.json` |
| `ROBOTHOR_MEMORY_GENERATION_PROVIDER`, `_REMOTE_MODEL`, `_MIN_INTERVAL_S` | Offload memory generation (fact extraction, episode summaries, insight discovery) from the local GPU to a remote provider. Embeddings and reranking always stay local; a remote failure falls back to local Ollama with a `MEMORY_GENERATION_REMOTE_FALLBACK` WARNING |
| `ROBOTHOR_DECLARED_TOOL_OUTAGES` | `tool:reason,tool:reason` — outages the operator has already decided about, so the tool-outage detector stops alerting on them |
| `GENUS_IMAGE_TAG`, `GENUS_WORKSPACE`, `GENUS_ENV_FILE`, `GENUS_UID`, `GENUS_GID` | These configure the **compose file**, not the platform, so `genus config` does not know them. See [Deployment](deployment.md#docker-compose) |

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

- Passwords are argon2id (`robothor/auth/passwords.py`), minimum 12 characters.
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

Unknown keys are a warning at load and an error only under `strict`. Nothing in
the engine runs `strict`; the repository's own template test does, against every
bundle under `templates/agents/`. A live instance may legitimately carry a field
a plugin reads; a template shipped from this repository may not.

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
(`searxng`, `browser`, `brave`, `perplexity`); omitting it runs the automatic
chain. `ROBOTHOR_WEB_SEARCH_BROWSER_FALLBACK=off` disables the *implicit*
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
print(cfg.db.dsn)           # "dbname=robothor_memory host=127.0.0.1 ..."
print(cfg.redis.url)        # "redis://127.0.0.1:6379/0"
print(cfg.ollama.base_url)  # "http://127.0.0.1:11434"
print(cfg.workspace)        # Path("<workspace>")
```

The config is a singleton. Call `reset_config()` in tests to reload from the
environment.
