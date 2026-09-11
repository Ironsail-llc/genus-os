# Configuration

All configuration is via environment variables with sensible defaults. No config files required for basic usage. See `infra/robothor.env.example` for a complete template.

!!! tip "Complete list"
    This page is a hand-written tour of the settings an operator touches most.
    The **complete** list — every variable the platform reads, with its type,
    default, restart and secret flags — is generated from the typed settings
    model. It lives in the repository at `docs/reference/configuration.md`
    (not published on this site), and `genus config schema` prints the same
    information as JSON Schema.

## Reading and changing settings

You should not have to grep `/etc` to answer "what is this set to?", and you
should not have to guess what a change needs restarting. `genus config` reads
the same typed registry this page is generated from.

```bash
genus config get ROBOTHOR_MAX_CONCURRENT_AGENTS   # value + where it came from
genus config explain ROBOTHOR_RBAC_MODE           # everything declared about it
genus config list --group engine --changed        # what is off its default
genus config set ROBOTHOR_MAX_CONCURRENT_AGENTS 6
genus config validate                             # --json for machines
genus config schema                               # JSON Schema, for tooling
```

| Command | What it does |
|---------|--------------|
| `get NAME` | The effective value and its provenance: `runtime` (an operator-set `feature_flags` row), `env`, `config.yaml`, or `default`. |
| `set NAME VALUE` | Routes by the setting's own metadata — see below. |
| `explain NAME` | Description, group, type, default, deprecated aliases, secret/governed/restart flags, current provenance. |
| `list [--group G] [--changed]` | Every setting, or one group, or only what is not on its default. |
| `validate [--json]` | Connectivity checks, plus unknown keys, deprecated names in use, and settings the running process disagrees with. Exit 1 only for errors — see below. |

`validate` separates what is broken from what is merely worth knowing:

- **errors** (exit 1) — a key nothing reads, a connectivity check that failed,
  a Telegram credential that is not shaped like one. Something is wrong.
- **warnings** (exit 0) — a deprecated name still set, and a `settings:` value
  the environment is overriding. The second is documented precedence, not a
  fault: a variable in `/etc/robothor/robothor.env` beats the file, so the
  file's value applies once you clear the variable and restart the units
  named. `--json` lists those settings separately under `pending_restart`,
  alongside `errors`.

**`set` routes by what the setting is**, not by what you typed:

- a **governed flag** (`ROBOTHOR_RBAC_MODE` and the rest of `infra/flags.yaml`)
  goes to the flag store and is live within seconds — no restart, no file edit;
- a **secret** is refused, naming `genus vault set`. Writing a credential into
  a config file is not a shortcut worth having;
- **everything else** is written into the `settings:` block of
  `$ROBOTHOR_WORKSPACE/.robothor/config.yaml`, atomically and without
  disturbing your comments, and the reply is either `applied` or
  `restart required: robothor-engine`.

A secret is never printed. `get` and `list` show `<set, sha256:ab12cd34>` —
enough to tell two boxes apart without putting the value on your screen.

### config.yaml and unknown keys

`config.yaml` sits below the environment and above the defaults: a variable in
the environment still wins. Its `settings:` block mirrors the groups in the
generated reference (`docs/reference/configuration.md` in the repository):

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
| `enforce` | Resolving settings fails, naming the key. Recommended for new installs, and for any box where `genus config validate` reports no unknown keys. |

The rung can be set in the environment or in the file it governs (as above), so
a new install can ship `enforce` without an environment file.

## Loading

```python
from robothor.config import get_config

cfg = get_config()
print(cfg.db.dsn)           # "dbname=robothor_memory host=127.0.0.1 ..."
print(cfg.redis.url)        # "redis://127.0.0.1:6379/0"
print(cfg.ollama.base_url)  # "http://127.0.0.1:11434"
print(cfg.workspace)        # Path("/home/your-user/robothor")
```

The config is a singleton. Call `reset_config()` in tests to reload from environment.

## Database (PostgreSQL + pgvector)

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_DB_HOST` | `127.0.0.1` | PostgreSQL host |
| `ROBOTHOR_DB_PORT` | `5432` | PostgreSQL port |
| `ROBOTHOR_DB_NAME` | `robothor_memory` | Database name |
| `ROBOTHOR_DB_USER` | `$USER` | Database user (falls back to system user) |
| `ROBOTHOR_DB_PASSWORD` | *(empty)* | Database password |

## Redis

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_REDIS_HOST` | `127.0.0.1` | Redis host |
| `ROBOTHOR_REDIS_PORT` | `6379` | Redis port |
| `ROBOTHOR_REDIS_DB` | `0` | Redis database number |
| `ROBOTHOR_REDIS_PASSWORD` | *(empty)* | Redis password |
| `ROBOTHOR_REDIS_MAXMEMORY` | `2gb` | Redis maxmemory (Docker only) |
| `REDIS_URL` | *(derived)* | Full Redis URL override (e.g., `redis://:pass@host:6379/0`) |

## Ollama (LLM Inference)

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_OLLAMA_HOST` | `127.0.0.1` | Ollama server host |
| `ROBOTHOR_OLLAMA_PORT` | `11434` | Ollama server port |
| `ROBOTHOR_EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | Embedding model (1024-dim) |
| `ROBOTHOR_RERANKER_MODEL` | `Qwen3-Reranker-0.6B:F16` | Cross-encoder reranker |
| `ROBOTHOR_GENERATION_MODEL` | `qwen3:8b` | RAG generation model |
| `ROBOTHOR_VISION_MODEL` | `llama3.2-vision:11b` | Vision scene analysis model |
| `ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB` | `24` | autoDream only unloads the generation model when available memory (GiB) drops below this; `0` disables the unload entirely |

## Memory Generation Provider

Memory generation (fact extraction, episode summaries, insight discovery,
conflict classification, intent inference) can be offloaded from the local
Ollama GPU to a remote provider. Embeddings and reranking always stay local.
On remote failure the system falls back to local Ollama with a WARNING log
containing `MEMORY_GENERATION_REMOTE_FALLBACK` — watch for that marker.

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_MEMORY_GENERATION_PROVIDER` | `ollama` | `ollama` (local, unchanged behavior) or `openrouter` (remote) |
| `ROBOTHOR_MEMORY_GENERATION_REMOTE_MODEL` | `openrouter/xiaomi/mimo-v2.5` | Remote model when the provider is `openrouter` |
| `ROBOTHOR_MEMORY_GENERATION_MIN_INTERVAL_S` | `1.5` | Minimum seconds between remote generation calls (`0` disables pacing) |

`openrouter` requires `OPENROUTER_API_KEY` in the environment; if it is
missing, an ERROR is logged once and generation stays local. Remote 429/503
responses are retried up to 3 attempts with jittered exponential backoff
(finite `Retry-After` honored, each sleep capped at 20s, 45s total budget);
timeouts and network errors get one retry; other errors fall back to local
immediately.

### Provider keys

Five providers can hold credentials: `openrouter`, `anthropic`, `openai`,
`gemini`, `deepseek`. Each resolves **vault-first, then environment**, slot by
slot — a key written from the Settings page or the setup wizard wins the slot
it was written to, while a spare left in the shell keeps working.

| Slot | Vault key | Environment variable |
|------|-----------|----------------------|
| 1 (primary) | `providers/<id>/api_key` | `<PROVIDER>_API_KEY` |
| 2+ (spare) | `providers/<id>/api_key_<N>` | `<PROVIDER>_API_KEY_<N>` |

From a terminal:

```
genus vault set providers/openrouter/api_key sk-or-v1-primary
genus vault set providers/openrouter/api_key_2 sk-or-v1-spare
```

**Spares must be contiguous.** The pool walks slots from 1 and stops at the
first empty one, so a key in slot 3 with slot 2 empty would be stored and never
dialled. `PUT /api/providers/{id}/keys/{n}` refuses that with a 409 naming the
slot to fill first, and `GET /api/providers` reports any pre-existing stranded
row as `state: "orphaned"`.

**When the engine reads the vault.** Once at startup (before any subsystem
runs, so direct-`os.environ` consumers like memory generation see the key on
the first turn), and again on `POST /api/admin/secrets/reload` — what the
Settings page calls after a save — or on `SIGHUP`. Neither cancels in-flight
work, so no restart is needed. Between refreshes the values are served from an
in-memory snapshot: credential resolution sits on the LLM hot path and must not
open a database connection per call.

An instance whose vault has no master key resolves from the environment only
and says so once at INFO — the credential store is optional, and an engine that
could not make an LLM call because it is empty would be worse than no vault at
all.

Secrets are write-only end to end. `GET /api/providers` reports
`{configured, source, fingerprint, state, updated_at}` per slot and never a
value, and the test connection's error text is scrubbed of every credential
the provider might have echoed back.

### Credential pools

Every model in a fallback chain authenticates with the same provider key, so
a chain built on one credential is a chain of one link: on 2026-08-25 a single
capped `OPENROUTER_API_KEY` stopped the whole fleet, four cloud models and a
local tier notwithstanding.

Configure spares as numbered siblings and the engine rotates through them:

```
OPENROUTER_API_KEY=sk-or-v1-primary
OPENROUTER_API_KEY_2=sk-or-v1-spare
```

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

Keys are never logged. Anything naming a credential — logs, alerts, `repr()` —
uses a one-way fingerprint (`key-1a2b3c4d`) rather than the usual last-four
convention, which would print real key material.

## Service Ports

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_API_PORT` | `9099` | RAG Orchestrator / API server |
| `ROBOTHOR_BRIDGE_PORT` | `9100` | Bridge service (CRM, contacts) |
| `ROBOTHOR_VISION_PORT` | `8600` | Vision service |
| `ROBOTHOR_HELM_PORT` | `3004` | Helm dashboard |
| `ROBOTHOR_TTS_PORT` | `8880` | TTS service |

## Vision

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_VISION_MODE` | `disarmed` | Default mode: `disarmed`, `basic`, `armed`, `disabled` |
| `ROBOTHOR_CAMERA_SOURCE` | `/dev/video0` | Camera device, RTSP URL, or video file |
| `ROBOTHOR_CAMERA_WIDTH` | `640` | Capture width |
| `ROBOTHOR_CAMERA_HEIGHT` | `480` | Capture height |
| `ROBOTHOR_YOLO_MODEL` | `yolov8n` | YOLO variant: `yolov8n` (6MB), `yolov8s` (22MB), `yolov8m` (52MB) |
| `ROBOTHOR_VLM_MODEL` | `llama3.2-vision:11b` | Vision language model for scene analysis |

## Memory

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_MEMORY_TTL_HOURS` | `48` | Short-term memory TTL |
| `ROBOTHOR_IMPORTANCE_THRESHOLD` | `0.3` | Minimum importance for long-term archival |
| `ROBOTHOR_MEMORY_BLOCK_MAX_CHARS` | `5000` | Maximum characters per memory block |
| `ROBOTHOR_MEMORY_DIR` | `$WORKSPACE/memory` | Memory file directory |

## Workspace

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_WORKSPACE` | `~/robothor` | Base directory for runtime data |
| `ROBOTHOR_LOG_DIR` | `/var/log/robothor` | Log directory |

## Agent manifests

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_MANIFEST_SCHEMA_MODE` | `observe` | Validate each manifest against `robothor/engine/schema/agent_manifest.yaml` at load. `off` skips validation; `observe` logs each error with the agent, path and code and counts it in `robothor_manifest_schema_would_reject_total`; `enforce` refuses the manifest and reports the agent **broken** (never absent), so the scheduler will not prune its schedules. |

Only `error`-severity findings block under `enforce`. Advisory findings — a
model that is not in the registry, an unknown guardrail name, a key sitting in
the wrong block — stay warnings on every rung, because each describes a
manifest that loads and runs.

Validation runs on the **merged** manifest: the file plus `_defaults.yaml` —
exactly the layers a run merges. A typo in the fleet defaults therefore marks
every agent that inherits it broken, rather than passing a per-file check and
failing at run time.

Under `enforce`, `/ready` returns `503` with `checks.fleet: "error:broken:<n>"`
and a `broken_agents` list of ids, and the scheduler refuses to prune any
schedule while a manifest is broken. An agent reported **broken** needs its
manifest fixed; an agent reported **missing** needs re-creating. The two are
never collapsed.

Unknown keys are a warning at load and an error only under `strict`. Nothing in
the engine runs `strict`; the repo's own template test does, against every
bundle under `templates/agents/`. A live instance may legitimately carry a field
a plugin reads; a template shipped from this repo may not.

`observe` counts every occurrence in `robothor_manifest_schema_would_reject_total`
(labelled by agent) and logs each distinct agent/path/code once — `/metrics` is
how the count leaves the process today.

## Event Bus

| Variable | Default | Description |
|----------|---------|-------------|
| `EVENT_BUS_ENABLED` | `true` | Enable Redis Streams event bus |
| `EVENT_BUS_MAXLEN` | `10000` | Max entries per stream (circular buffer) |
| `ROBOTHOR_CAPABILITIES_MANIFEST` | `$WORKSPACE/agent_capabilities.json` | Agent RBAC manifest path |
| `ROBOTHOR_SERVICES_MANIFEST` | `$WORKSPACE/robothor-services.json` | Service registry path |

## Authentication

Three sign-in methods, and a deployment needs at least one. Local
email+password exists so a five-minute install does not have to stand up an
identity provider first; OIDC and Cloudflare Access are unchanged and can run
alongside it.

| Variable | Default | Description |
|----------|---------|-------------|
| `GENUS_AUTH_SIGNING_KEY` | *(vault)* | HS256 key the Bridge signs sessions with. At least 32 bytes; required in production. Also derives the key that encrypts stored TOTP secrets — rotating it invalidates every enrolled second factor (recover with `genus user mfa-reset`) |
| `GENUS_BRIDGE_SSO_SECRET` | *(empty)* | Shared dashboard↔Bridge secret. Required in production for every method |
| `GENUS_OIDC_ISSUERS` | *(empty)* | Comma-separated allowlist of OIDC issuers the Bridge will JIT-provision for |
| `GENUS_LOCAL_LOGIN` | `false` | Exactly `true` enables local email+password sign-in. Off by default: a password endpoint must be opted into, never appear on upgrade |
| `CF_ACCESS_TEAM_DOMAIN` / `CF_ACCESS_AUD` | *(empty)* | Sign in through a fronting Cloudflare Access policy |
| `GENUS_INSECURE_DEV_MODE` | `false` | Loopback-only development escape hatch. Not a sign-in method; forbidden in production |

### Local email and password

With `GENUS_LOCAL_LOGIN=true` the sign-in page renders an email + password
form (alongside the SSO button when both are configured) and the Bridge serves
`GET /api/auth/methods` and `POST /api/auth/login`.

- Passwords are argon2id (`robothor/auth/passwords.py`), minimum 12 characters.
- Every failure — unknown email, wrong password, disabled account, locked
  account — answers the same `invalid credentials`. The only distinguishable
  state is `mfa_required`, and only after a correct password.
- Ten consecutive failures freeze the account for 15 minutes; five attempts per
  (email, IP) per minute are allowed before a 429.
- **Owner MFA is mandatory when local login is the only configured method.**
  The owner still signs in, but the Helm shows a banner that cannot be
  dismissed until a factor is enrolled at `/account/security`.

Operator commands:

```bash
genus user set-password alice@example.com      # prompts twice, no echo
genus user set-password alice@example.com --password-stdin < secret
genus user mfa-reset alice@example.com         # clears a lost authenticator
```

## Notifications (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_TELEGRAM_BOT_TOKEN` | *(empty)* | Telegram bot token for alerts |
| `ROBOTHOR_TELEGRAM_CHAT_ID` | *(empty)* | Telegram chat ID for notifications |

## Web fetch

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_WEB_FETCH_USER_AGENT` | `GenusOS-web-fetch/1.0 (+https://github.com/Ironsail-llc/genus-os)` | Identity `web_fetch` sends. Sites that block HTTP-library defaults answer 403 to `python-httpx/*`, so the tool names itself; set this to add your own contact address |

`web_fetch` resolves a host, refuses any private, loopback, link-local or
carrier-internal (CGNAT) address in either IP family, and then connects to the
exact address it vetted — pinned at the socket, so the
request keeps its hostname and Host, TLS SNI and certificate verification all
see the real name. Certificate verification is never disabled; an untrusted
certificate is reported as such. Redirects are followed one hop at a time and
re-vetted at every hop, up to 5 real hops (the canonical `http→https` and
`apex↔www` bounces do not count against that).

## Web search (optional)

**From a residential or datacenter IP, most scraped engines block you.** Google,
Startpage, Brave-via-SearXNG, DuckDuckGo, Qwant and Mojeek all answer "access
denied"/"CAPTCHA"/"too many requests" from a typical home or cloud egress, and
DuckDuckGo's HTML endpoint answers a real browser with an HTTP 202 challenge
page. **The Brave Search API is the supported reliable path**; the scraping
chain below is the best effort for an instance without a key.

The `web_search` tool grades the answer it gets back. It scores results by the
*rare* terms of the query — a term carried by nearly every result is what the
results have in common, not evidence they answer the question — and always
requires a place or number the operator named (Jamaica, Queens, a ZIP) to
appear. When SearXNG errors, reports its general engines unresponsive, or comes
back failing that grade, the search re-runs through the `browser` provider: the
engine's own browser loads a real Bing (then Startpage, then DuckDuckGo HTML)
results page **in a background tab**, never moving the page an agent is working
on. It advances to the next source when one returns too few rows *or* when the
query named a place and the rows never mention it — Bing pins a local query to
the egress IP's own city, Startpage honours the words that were typed — and the
rows that name the place lead the answer, with `place_source` saying which
source supplied them. The result then carries `"provider": "browser"`,
`"fallback_from": "searxng"`, a `fallback_reason`, `sources` (every source
tried, in order), and the unresponsive-engine list.

`fallback_reason` values: `low_relevance`, `missing_place_terms`,
`engines_unresponsive`, `error`, `browser_low_relevance` (the browser answer
missed too), `browser_parse_empty` (page loaded, nothing parsed — the result
selectors have moved), `browser_blocked` (challenge/interstitial),
`browser_timeout`, `browser_denied` (the run may not use the browser tool),
`browser_unavailable`, `browser_fallback_disabled`.

A local-looking query ("… near X", "… in Springfield", a ZIP) also gets up to
five OpenStreetMap rows under `places`: for a mapped category (coworking,
cafe/coffee, gym) the place is geocoded once with Nominatim — using exactly the
words the query gave, never an inferred city or state — and the amenities around
it come from Overpass; anything else falls back to a free-text Nominatim lookup.
Both APIs share one ≤1 request/second courtesy budget. When the local path runs
and finds nothing, the result says why in `places_reason`:
`geocode_failed:<place>`, `overpass_empty`, `overpass_error`,
`category_unmapped`, or `place_not_in_query`.

An agent can force one provider with the tool's `provider` argument
(`searxng`, `browser`, `brave`, `perplexity`); omitting it runs the automatic
chain.

| Variable | Default | Description |
|----------|---------|-------------|
| `BRAVE_SEARCH_API_KEY` | *(empty)* | Brave Search API key. When set — and when the caller did not name a different provider — `web_search` prefers the Brave API and falls back to the SearXNG → browser chain on any error. Unset means the provider is absent: no call, no error. |
| `ROBOTHOR_WEB_SEARCH_BROWSER_FALLBACK` | `on` | `off`/`0`/`false`/`no` disables the *implicit* browser fallback (it drives a headed Chromium on the operator's display). An explicit `provider="browser"` still runs. |

## Failure-mode detectors

See [Observability](OBSERVABILITY.md#failure-mode-detectors) for what each
detector watches.

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_DETECTORS_ENABLED` | `1` | `0` disables every detector |
| `ROBOTHOR_DECLARED_TOOL_OUTAGES` | *(empty)* | `tool:reason,tool:reason` — tool outages the operator has already decided about, so `tool_outage_detector` stops alerting about them |
| `ROBOTHOR_RECORD_ASSISTANT_TURNS` | *(off)* | `true` persists each assistant turn onto its `agent_run_steps` row, so a finished run's reasoning can be read back. Off by default: a turn contains whatever the agent was reasoning about, which on a real instance is the operator's own data. Capped at 2,500 characters per turn. |

## Service URL Overrides

The service registry supports environment variable overrides for any service:

| Variable | Overrides |
|----------|-----------|
| `BRIDGE_URL` | Bridge base URL |
| `ORCHESTRATOR_URL` | API server base URL |
| `VISION_URL` | Vision service base URL |
| `ROBOTHOR_OLLAMA_URL` | Ollama base URL (`OLLAMA_URL` still works, deprecated) |
| `SEARXNG_URL` | SearXNG search URL |

These take precedence over `robothor-services.json` values.
