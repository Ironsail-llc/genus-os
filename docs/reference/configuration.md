<!--
GENERATED FILE — do not edit.

Rendered from robothor/settings/model.py by scripts/gen_configuration_doc.py.
Change a setting there and re-run the script; the committed file and the
generator output are compared in tests/test_configuration_doc_generated.py.
-->

# Configuration reference

Every `ROBOTHOR_*` and `GENUS_*` setting Genus OS reads, with the exact
environment variable name.

Provider credentials that carry no Genus prefix — `OPENROUTER_API_KEY`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and their peers — are **not** in this
table. They belong to the providers, not to this platform, so they are
declared and documented with the provider integration that reads them rather
than renamed into a Genus namespace.

Settings resolve from four places, lowest priority first:

1. the defaults below,
2. the `settings:` block of `<workspace>/.robothor/config.yaml`,
3. the environment,
4. an explicit runtime override.

`<workspace>` is `$ROBOTHOR_WORKSPACE`, or `~/robothor` when that is unset.

Column meanings:

- **Restart** — `yes` means a change only takes effect when the service
  restarts; `no` means it is picked up without one.
- **Secret** — holds a credential. These never have a default and are redacted
  by `genus config`.
- **Since** — the release that introduced the setting. `legacy` predates this
  registry.

Run `genus config schema` for the same information as JSON Schema.

315 settings in 13 groups.

## paths

Where the instance keeps its files. Every path defaults under the workspace.

| Variable | Type | Default | Restart | Secret | Since | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `GENUS_SNAPSHOT_STAGING_DIR` | str | _(empty)_ | yes | no | legacy | Directory `genus snapshot` stages an export in before it is packed. Must already exist; empty uses a temporary directory. |
| `ROBOTHOR_ADAPTER_DIR` | str | _(empty)_ | yes | no | legacy | Directory scanned for LoRA/model adapters. Empty means ~/.config/robothor/adapters. |
| `ROBOTHOR_AGENTS_DIR` | str | _(empty)_ | yes | no | legacy | Directory the bridge's fleet API lists agent manifests from. Empty means the manifest directory under the workspace. |
| `ROBOTHOR_ALERT_SPOOL_DIR` | str | `/var/lib/robothor/alert-spool` | yes | no | legacy | Directory alerts are spooled to when delivery fails, so an outage of the notification channel does not lose the alert. |
| `ROBOTHOR_BACKUP_STATE_DIR` | str | _(empty)_ | yes | no | legacy | Directory the backup guardrail reads freshness markers from when deciding whether backups have gone stale. |
| `ROBOTHOR_CAPABILITIES_MANIFEST` | str | _(empty)_ | yes | no | legacy | Explicit path to the agent capabilities manifest. Empty searches <workspace>/agent_capabilities.json and then the packaged copy. |
| `ROBOTHOR_CODEX_BIN` | str | `codex` | yes | no | legacy | Name or path of the codex binary the codex provider executes. |
| `ROBOTHOR_CODEX_HOME` | str | _(empty)_ | yes | no | legacy | CODEX_HOME for the codex subscription provider — the directory holding its auth state. Empty falls back to CODEX_HOME. Also read from `CODEX_HOME`. |
| `ROBOTHOR_DROPIN_DIR` | str | _(empty)_ | yes | no | legacy | Directory of systemd drop-in fragments the instance-env reader reconciles against the running units. |
| `ROBOTHOR_LIVENESS_STATE_DIR` | str | `/run/robothor/fleet-guard` | yes | no | legacy | Directory the fleet liveness guard counts consecutive failures in. |
| `ROBOTHOR_LOG_DIR` | str | `/var/log/robothor` | yes | no | legacy | Directory the service units write log files to. |
| `ROBOTHOR_MANIFEST_DIR` | str | _(empty)_ | yes | no | legacy | Directory of agent manifests (docs/agents/*.yaml). These are the source of truth for the fleet. Empty means <workspace>/docs/agents. |
| `ROBOTHOR_MANIFEST_GUARD_STATE` | str | `/run/robothor/manifest-guard-alerts.json` | yes | no | legacy | File the manifest guard stores alert-dedup timestamps in, so a broken manifest does not page once per scheduler tick. |
| `ROBOTHOR_MEMORY_DIR` | str | _(empty)_ | yes | no | legacy | Directory holding memory artefacts (vision mode marker, projections, eval corpora). Empty means <workspace>/memory. |
| `ROBOTHOR_MODEL_BREAKER_STATE` | str | `/run/robothor/model-breaker-alerts.json` | yes | no | legacy | File the model circuit breaker stores alert-dedup timestamps in. |
| `ROBOTHOR_OWNER_CONFIG` | str | _(empty)_ | yes | no | legacy | Explicit override for the operator identity file robothor.owner_config.load_owner_config() reads when called with no explicit path. Empty means the hardcoded ~/.robothor/owner.yaml. Read directly in robothor/settings/sources.py (owner_config_override_path()), not through this model -- declared here only so the name is documented and the env-read-site ratchet sees it as accounted for. |
| `ROBOTHOR_RLM_LOG_DIR` | str | _(empty)_ | yes | no | legacy | Directory the recursive language model tool writes per-call logs to. Empty means <workspace>/logs/rlm. |
| `ROBOTHOR_SERVICES_MANIFEST` | str | _(empty)_ | yes | no | legacy | Explicit path to robothor-services.json, the service/port registry shared by the engine and the dashboard. |
| `ROBOTHOR_SLO_STATE_DIR` | str | _(empty)_ | yes | no | legacy | Directory the SLO watcher keeps its per-check state in, so a breach is reported once rather than every poll. |
| `ROBOTHOR_TEMPLATE_DIR` | str | _(empty)_ | yes | no | legacy | Explicit override for the scaffold templates `genus init` copies. Empty falls back to the packaged templates/ directory. |
| `ROBOTHOR_WAL_ARCHIVE_DIR` | str | `/var/lib/robothor/wal_archive` | yes | no | legacy | Directory PostgreSQL WAL segments are archived to before the offsite sync picks them up. |
| `ROBOTHOR_WATCHDOG_TRACE_FILE` | str | _(empty)_ | yes | no | legacy | When set, the stall watchdog appends a stack trace of the stalled run to this file before it acts. Empty disables tracing. |
| `ROBOTHOR_WORKFLOW_DIR` | str | _(empty)_ | yes | no | legacy | Directory of workflow definitions. Empty means <workspace>/docs/workflows. |
| `ROBOTHOR_WORKSPACE` | str | _(empty)_ | yes | no | legacy | Root of the instance workspace: agent manifests, workflows, brain, memory and the .robothor config directory all resolve under it. Empty means ~/robothor. |

## database

PostgreSQL connection, tenancy and row-level security.

| Variable | Type | Default | Restart | Secret | Since | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `ROBOTHOR_BENCHMARK_TENANT` | str | _(empty)_ | yes | no | legacy | Tenant benchmark runs are confined to, so a harness can never touch production CRM rows. Empty means the built-in sandbox tenant. |
| `ROBOTHOR_DB_CONNECT_TIMEOUT` | int | `5` | yes | no | legacy | Seconds to wait for a connection before failing, so a wedged database stalls one call rather than the process. |
| `ROBOTHOR_DB_HOST` | str | _(empty)_ | yes | no | legacy | PostgreSQL host. An empty value means a Unix socket with peer authentication, which is the default on a single-box install. |
| `ROBOTHOR_DB_NAME` | str | `robothor_memory` | yes | no | legacy | Database holding CRM, memory and engine tables. |
| `ROBOTHOR_DB_PASSWORD` | str | _(unset)_ | yes | yes | legacy | PostgreSQL password. Unset with a Unix-socket host (peer auth). |
| `ROBOTHOR_DB_PORT` | int | `5432` | yes | no | legacy | PostgreSQL TCP port. |
| `ROBOTHOR_DB_SSLMODE` | str | _(empty)_ | yes | no | legacy | libpq sslmode for the connection (disable/allow/prefer/require/verify-ca/verify-full). Empty leaves libpq's own default. |
| `ROBOTHOR_DB_USER` | str | _(empty)_ | yes | no | legacy | PostgreSQL role. Empty falls back to $USER, then 'robothor'. Must be a non-superuser for row-level security to actually apply. |
| `ROBOTHOR_DEFAULT_TENANT` | str | `default` | yes | no | legacy | The tenant DAL calls tag rows with when the caller names none. Read at import time into robothor.constants.DEFAULT_TENANT. |
| `ROBOTHOR_PLATFORM_TENANT` | str | _(empty)_ | yes | no | legacy | Tenant the bridge treats as the platform operator for owner-only endpoints. Empty means the default tenant. |
| `ROBOTHOR_RLS_ENABLED` | bool | `false` | yes | no | legacy | **governed.** Bind every connection to a tenant so PostgreSQL row-level security applies. Inert unless the DB user is a non-superuser; federation refuses to activate a link while it is off. |
| `ROBOTHOR_SOAK_TEMPLATE` | str | `robothor_test` | yes | no | legacy | Template database the federation soak clones per instance. |
| `ROBOTHOR_TENANT_ID` | str | _(empty)_ | yes | no | legacy | The tenant this process operates AS — what the RLS connection binds to. Must agree with ROBOTHOR_DEFAULT_TENANT or every default-tenant write is refused by the RLS WITH CHECK and the caller gets None. |
| `ROBOTHOR_TEST_ADMIN_DSN` | str | _(unset)_ | yes | yes | legacy | Full libpq DSN for the administrative role CI creates and drops test databases with. |
| `ROBOTHOR_TEST_DB_ALLOW` | str | _(empty)_ | yes | no | legacy | Exact database name allowed under pytest besides a *_test database. The release gate legitimately runs against one; nothing else should. |
| `ROBOTHOR_TEST_DB_DSN` | str | _(unset)_ | yes | yes | legacy | Full libpq DSN CI points the suites at. Carries a password, so it is supplied by the workflow rather than committed anywhere. |

## redis

Redis connection and the event-bus streams it carries.

| Variable | Type | Default | Restart | Secret | Since | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `ROBOTHOR_EXTRA_STREAMS` | str | _(empty)_ | yes | no | legacy | Comma-separated extra event-bus stream names to create and consume alongside the built-in ones. |
| `ROBOTHOR_REDIS_DB` | int | `0` | yes | no | legacy | Redis logical database. 0 is production; the test suite pins its own so a plain pytest run cannot XADD onto live streams. |
| `ROBOTHOR_REDIS_HOST` | str | `127.0.0.1` | yes | no | legacy | Redis host. |
| `ROBOTHOR_REDIS_MAXMEMORY` | str | `2gb` | yes | no | legacy | maxmemory the provisioning scripts configure the Redis server with. |
| `ROBOTHOR_REDIS_PASSWORD` | str | _(unset)_ | yes | yes | legacy | Redis password, if the server requires one. |
| `ROBOTHOR_REDIS_PORT` | int | `6379` | yes | no | legacy | Redis TCP port. |

## ollama

The local Ollama endpoint and the models served from it.

| Variable | Type | Default | Restart | Secret | Since | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `ROBOTHOR_EMBEDDING_MODEL` | str | `qwen3-embedding:0.6b` | yes | no | legacy | Model used to embed memory and CRM text for vector search. |
| `ROBOTHOR_FACE_MODEL` | str | `buffalo_l` | yes | no | legacy | InsightFace model pack used for face detection and enrolment. |
| `ROBOTHOR_GENERATION_MODEL` | str | `qwen3:8b` | yes | no | legacy | Local model used for short generation tasks that never leave the box. |
| `ROBOTHOR_OLLAMA_HOST` | str | `127.0.0.1` | yes | no | legacy | Ollama host, used only when no full URL is set. |
| `ROBOTHOR_OLLAMA_NUM_CTX` | int | `0` | no | no | legacy | Per-request context-window clamp sent to Ollama. 0 leaves the model's own default; a non-positive or non-integer value is ignored with a warning. |
| `ROBOTHOR_OLLAMA_PORT` | int | `11434` | yes | no | legacy | Ollama port, used only when no full URL is set. |
| `ROBOTHOR_OLLAMA_URL` | str | _(empty)_ | yes | no | legacy | Full base URL of the Ollama server. Canonical form; it supersedes the host/port pair. Empty falls back to host and port. Also read from `OLLAMA_URL`. |
| `ROBOTHOR_RERANKER_MODEL` | str | `Qwen3-Reranker-0.6B:F16` | yes | no | legacy | Cross-encoder that reorders retrieval hits before they reach a prompt. |
| `ROBOTHOR_VISION_MODEL` | str | `llama3.2-vision:11b` | yes | no | legacy | Vision-language model used to describe camera frames and images. |
| `ROBOTHOR_VLM_MODEL` | str | `llama3.2-vision:11b` | yes | no | legacy | Vision-language model the vision service loads for scene captioning. |
| `ROBOTHOR_YOLO_MODEL` | str | `yolov8n` | yes | no | legacy | YOLO weights the vision service runs object detection with. |

## providers

Cloud model routing, budgets and the failure controls around them.

| Variable | Type | Default | Restart | Secret | Since | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `ROBOTHOR_COMPACTION_TRIGGER_TOKENS` | int | `80000` | no | no | legacy | Absolute prompt-token budget above which a run compacts its context. |
| `ROBOTHOR_DEFERRED_TOOLS_THRESHOLD` | int | `40` | no | no | legacy | Number of tools above which schemas are deferred behind tool search rather than sent in full on every request. |
| `ROBOTHOR_EAGER_TOOL_COMPRESSION` | bool | `false` | yes | no | legacy | Fleet default for compressing tool results as soon as they land rather than at the next compaction. A manifest setting wins over it. |
| `ROBOTHOR_HOURLY_COST_CAP_USD` | float | `5.0` | no | no | legacy | Fleet-wide spend ceiling per hour. The engine stops dispatching new runs once the rolling hour exceeds it. |
| `ROBOTHOR_LAST_RESORT_MODEL` | str | _(empty)_ | yes | no | legacy | Model appended to every agent's fallback chain so a run always has somewhere to land when the cloud providers are down. Manifest validation checks each chain ends here. |
| `ROBOTHOR_MODEL_BREAKER_ALERT_DEDUP` | int | `21600` | yes | no | legacy | Seconds between repeat alerts for the same tripped model. |
| `ROBOTHOR_MODEL_BREAKER_COOLDOWN` | int | `600` | yes | no | legacy | Seconds an opened model breaker stays open before a trial request. |
| `ROBOTHOR_MODEL_BREAKER_THRESHOLD` | int | `3` | yes | no | legacy | Consecutive provider failures on one model before the circuit breaker opens and traffic moves to the fallback chain. |
| `ROBOTHOR_PERIODIC_QUOTA_COOLDOWN_SECONDS` | int | `21600` | yes | no | legacy | How long a credential that hit a periodic (daily/weekly) quota stays parked. Too short and a WEEKLY cap is retried every few minutes for days, which is how one capped key stalled the whole fleet. |
| `ROBOTHOR_REAL_TOKENIZER_ENABLED` | bool | `false` | yes | no | legacy | Count context with litellm's real tokenizer instead of the cheap character estimate. More accurate, measurably slower. |
| `ROBOTHOR_RLM_MAX_BUDGET` | float | `2.0` | yes | no | legacy | USD ceiling for a single recursive language model invocation. |
| `ROBOTHOR_RLM_MAX_DEPTH` | int | `1` | yes | no | legacy | How deep the recursive language model may nest sub-calls. |
| `ROBOTHOR_RLM_MAX_ITERATIONS` | int | `30` | yes | no | legacy | Tool-call iterations allowed inside one recursive language model run. |
| `ROBOTHOR_RLM_MAX_TIMEOUT` | int | `240` | yes | no | legacy | Wall-clock seconds a single recursive language model call may take. |
| `ROBOTHOR_RLM_ROOT_MODEL` | str | `openrouter/anthropic/claude-sonnet-4.6` | yes | no | legacy | Model the recursive language model tool runs its root call on. |
| `ROBOTHOR_RLM_SUB_MODEL` | str | `openrouter/anthropic/claude-haiku-4.5` | yes | no | legacy | Model the recursive language model tool runs child calls on. |

## engine

The agent execution layer: bind address, concurrency, pacing, sandbox.

| Variable | Type | Default | Restart | Secret | Since | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `ROBOTHOR_ALERT_COOLDOWN_SECONDS` | int | `3600` | yes | no | legacy | Minimum seconds between repeats of the same unit-failure alert. |
| `ROBOTHOR_ALERT_SELFTEST` | bool | `false` | yes | no | legacy | Fire one info-level alert shortly after boot to prove the alert path delivers. Leave off outside a delivery test. |
| `ROBOTHOR_ALERT_SPOOL_CAP` | int | `50` | yes | no | legacy | Most alerts held on the retry spool; beyond it the oldest are dropped. |
| `ROBOTHOR_ALERT_SPOOL_MAX_AGE_SECONDS` | int | `86400` | yes | no | legacy | Age at which a spooled alert is discarded rather than delivered late. |
| `ROBOTHOR_ALERT_SPOOL_MAX_ATTEMPTS` | int | `48` | yes | no | legacy | Delivery attempts for one spooled alert before it is given up on. |
| `ROBOTHOR_ALERT_WEBHOOK_URL` | str | _(unset)_ | yes | yes | legacy | Webhook alerts are POSTed to. Empty skips webhook delivery entirely. Held as a secret: these URLs routinely carry the bearer token in the path or the query string. |
| `ROBOTHOR_ALLOW_EMPTY_FLEET` | bool | `true` | yes | no | legacy | Let the engine start with no agent manifests at all. True suits a fresh install; false makes an emptied manifest directory fatal. |
| `ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB` | float | `24.0` | yes | no | legacy | Free VRAM (GiB) below which the autodream pass unloads local models rather than competing with live agent work. |
| `ROBOTHOR_BUDDY_GRADER_DRYRUN` | bool | `false` | yes | no | legacy | Run the verification grader without writing its verdicts, for checking a grading change against live runs. |
| `ROBOTHOR_DAEMON_START_TS` | str | _(empty)_ | yes | no | legacy | ISO timestamp the daemon sets on itself at boot and child processes read to report uptime. Set by the engine, not by an operator. |
| `ROBOTHOR_DEFAULT_CHAT_AGENT` | str | `main` | yes | no | legacy | Agent an inbound chat is routed to when nothing names one. |
| `ROBOTHOR_ENGINE_HOST` | str | `127.0.0.1` | yes | no | legacy | Address the engine's HTTP surface binds to. Loopback by default; the auth guard refuses the insecure dev mode on any other address. |
| `ROBOTHOR_ENGINE_PORT` | int | `18800` | yes | no | legacy | Engine HTTP port. |
| `ROBOTHOR_ENGINE_URL` | str | `http://127.0.0.1:18800` | yes | no | legacy | Base URL the dashboard's server-side client calls the engine on. |
| `ROBOTHOR_EXECUTION_MODE` | str | `auto` | yes | no | legacy | Which tier runs agents: auto, cloud, or local. 'local' switches the budgets as well as the model — cloud-era wall-clock budgets were 78% of local-tier failures. |
| `ROBOTHOR_IMPORTANCE_THRESHOLD` | float | `0.3` | yes | no | legacy | Minimum importance score for an extracted fact to be stored. |
| `ROBOTHOR_LOCAL_GATE_WAIT_SECONDS` | int | `120` | no | no | legacy | How long a local-tier run waits for a GPU slot before giving up. |
| `ROBOTHOR_LOCAL_MAX_CONCURRENT` | int | `0` | no | no | legacy | Concurrency ceiling while on the local tier. 0 lets the host profile derive one from VRAM and core count. |
| `ROBOTHOR_LOCAL_PACE_ALL_C` | int | `85` | no | no | legacy | GPU temperature (C) at which all local work is paced down. |
| `ROBOTHOR_LOCAL_PACE_BACKGROUND_C` | int | `80` | no | no | legacy | GPU temperature (C) at which background local work is paced down. |
| `ROBOTHOR_LOCAL_PACE_RESUME_C` | int | `75` | no | no | legacy | GPU temperature (C) local work resumes full pace below. |
| `ROBOTHOR_LOG_FORMAT` | str | _(empty)_ | yes | no | legacy | 'json' or 'console'. An explicit value always wins; empty picks json under systemd and console on a terminal. |
| `ROBOTHOR_MAIN_SESSION_KEY` | str | `agent:main:primary` | yes | no | legacy | Session key of the operator's primary conversation, which channel deliveries are dual-written into. |
| `ROBOTHOR_MANIFEST_ALERT_DEDUP` | int | `3600` | yes | no | legacy | Seconds between repeat alerts about the same broken manifest. |
| `ROBOTHOR_MAX_CONCURRENT_AGENTS` | int | `3` | no | no | legacy | How many agent runs may execute at once. |
| `ROBOTHOR_MAX_CONCURRENT_SPAWNS` | int | `10` | no | no | legacy | How many sub-agent spawns may be in flight at once. |
| `ROBOTHOR_MAX_ITERATIONS` | int | `20` | no | no | legacy | Default ceiling on tool-call iterations within one agent run. |
| `ROBOTHOR_MAX_SPAWN_BATCH` | int | `10` | no | no | legacy | Largest number of sub-agents one parent may spawn in a single batch. |
| `ROBOTHOR_MAX_WALLCLOCK_SECONDS` | int | `0` | no | no | legacy | Hard wall-clock ceiling for one run before the stall watchdog ends it. 0 disables the ceiling. |
| `ROBOTHOR_MEMORY_BLOCK_MAX_CHARS` | int | `5000` | yes | no | legacy | Character ceiling on one agent memory block before it is compacted. |
| `ROBOTHOR_MEMORY_TTL_HOURS` | int | `48` | yes | no | legacy | How long short-term memory rows survive before decay considers them. |
| `ROBOTHOR_MIN_AGENT_COUNT` | int | `1` | yes | no | legacy | Fewest loaded agents the fleet guard accepts before /ready reports unhealthy — the tripwire for a manifest directory that went missing. |
| `ROBOTHOR_OPERATOR_NAME` | str | _(empty)_ | yes | no | legacy | Display name the engine uses for the operator in prompts. Prefer ~/.robothor/owner.yaml, which is the canonical identity source. |
| `ROBOTHOR_RECORD_ASSISTANT_TURNS` | bool | `false` | yes | no | legacy | Persist assistant turns into the session transcript as well as user turns. Larger transcripts, fuller replay. |
| `ROBOTHOR_REQUIRED_AGENT_IDS` | str | _(empty)_ | yes | no | legacy | Comma-separated agent ids that must be loaded, or /ready returns 503. Names a broken manifest instead of letting the fleet run short. |
| `ROBOTHOR_RESERVED_INTERACTIVE_SLOTS` | int | `1` | no | no | legacy | Concurrency slots held back for interactive chat so background work cannot starve the operator's own conversation. |
| `ROBOTHOR_RESUME_IN_FLIGHT` | bool | `false` | yes | no | legacy | **governed.** Resume runs that were in flight when the engine restarted. Verify it from a recovered run, never from the log line it prints itself. |
| `ROBOTHOR_RIP_1_AGENTS` | str | _(empty)_ | yes | no | legacy | Comma-separated soak allowlist for the background-review rip: when set, only these agents take the new path. |
| `ROBOTHOR_SANDBOX_BINARY` | str | _(empty)_ | yes | no | legacy | Container runtime used for sandboxed exec. Empty prefers rootless podman, then docker. |
| `ROBOTHOR_SANDBOX_DEFAULT_MODE` | str | _(empty)_ | yes | no | legacy | **governed.** Fleet default sandbox mode for agents whose manifest names none. Empty leaves exec unrouted, which is sandboxing in name only. |
| `ROBOTHOR_SANDBOX_IMAGE` | str | `robothor-sandbox:latest` | yes | no | legacy | Image sandboxed exec runs agent commands inside. |
| `ROBOTHOR_SANDBOX_NETWORK` | str | `none` | yes | no | legacy | Container network for sandboxed exec. 'none' is the default; only 'bridge' gives sandboxed commands network access. |
| `ROBOTHOR_SANDBOX_START_RETRIES` | int | `1` | yes | no | legacy | Retries when the sandbox container fails to start. |
| `ROBOTHOR_SANDBOX_START_RETRY_SECONDS` | int | `3` | yes | no | legacy | Seconds between sandbox container start retries. |
| `ROBOTHOR_TIMEZONE` | str | `America/New_York` | yes | no | legacy | IANA timezone schedules and human-facing timestamps are rendered in. |
| `ROBOTHOR_TRAJECTORY_SAMPLE` | float | `0.0` | no | no | legacy | Fraction of runs (0.0-1.0) whose full trajectory is recorded for later analysis. Clamped into range. |
| `ROBOTHOR_WEB_FETCH_USER_AGENT` | str | _(empty)_ | no | no | legacy | User-Agent web_fetch sends. Empty uses the built-in string; set it when a site blocks the default. |
| `ROBOTHOR_WORKFLOW_STREAK_WINDOW_DAYS` | int | `14` | yes | no | legacy | Window the workflow detector counts repeated action streaks over when proposing a new workflow. |

## channels

How the instance reaches people, and who it says it is.

| Variable | Type | Default | Restart | Secret | Since | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `ROBOTHOR_AI_DOMAIN` | str | _(empty)_ | yes | no | legacy | Domain the per-dashboard hostnames are derived from. |
| `ROBOTHOR_AI_EMAIL` | str | _(empty)_ | yes | no | legacy | The bot's own sending address. Distinct from the operator's address: mail tools use this as the sender, never the operator identity. |
| `ROBOTHOR_AI_NAME` | str | `Genus` | yes | no | legacy | Name the assistant introduces itself with in channels and on the dashboard. |
| `ROBOTHOR_AI_PHONE` | str | _(empty)_ | yes | no | legacy | Phone number shown on the assistant's public contact card. |
| `ROBOTHOR_BRAND_NAME` | str | `Genus OS` | yes | no | legacy | Product name shown in dashboard chrome. |
| `ROBOTHOR_DOMAIN` | str | _(empty)_ | yes | no | legacy | Public domain the tunnel generator issues ingress hostnames under. |
| `ROBOTHOR_OWNER_EMAIL` | str | _(empty)_ | yes | no | legacy | DEPRECATED operator email. Operator identity belongs in ~/.robothor/owner.yaml; this is read only as a legacy fallback. |
| `ROBOTHOR_OWNER_NAME` | str | _(empty)_ | yes | no | legacy | DEPRECATED operator display name. Operator identity belongs in ~/.robothor/owner.yaml; this is read only as a legacy fallback. |
| `ROBOTHOR_SLACK_ALLOWED_CHANNELS` | str | _(empty)_ | yes | no | legacy | Comma-separated Slack channel ids the bot will respond in. |
| `ROBOTHOR_SLACK_ALLOWED_USERS` | str | _(empty)_ | yes | no | legacy | Comma-separated Slack user ids allowed to talk to the bot. Empty means no allowlist, which the channel warns about at start. |
| `ROBOTHOR_SLACK_APP_TOKEN` | str | _(unset)_ | yes | yes | legacy | Slack app-level token for socket mode. |
| `ROBOTHOR_SLACK_BOT_TOKEN` | str | _(unset)_ | yes | yes | legacy | Slack bot token. The Slack channel starts only when it and the app token are both set. |
| `ROBOTHOR_TELEGRAM_BOT_NAME` | str | _(empty)_ | yes | no | legacy | @name of the Telegram bot, shown on the dashboard so an operator can find the right conversation. |
| `ROBOTHOR_TELEGRAM_BOT_TOKEN` | str | _(unset)_ | yes | yes | legacy | Bot token for the Telegram channel. Empty disables Telegram. Also read from `TELEGRAM_BOT_TOKEN`. |
| `ROBOTHOR_TELEGRAM_CHAT_ID` | str | _(empty)_ | yes | no | legacy | Default Telegram chat deliveries go to when an agent names none. Also read from `TELEGRAM_CHAT_ID`. |
| `ROBOTHOR_VOICE_NOTES_ENABLED` | bool | `false` | yes | no | legacy | Transcribe inbound voice notes. Off unless a speech-to-text provider is actually configured. |

## auth

Who may reach the bridge and the dashboard, and how that is proven.

| Variable | Type | Default | Restart | Secret | Since | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `GENUS_AUTH_ENFORCE` | bool | `false` | yes | no | legacy | **governed.** One-way compatibility switch that turns identity checks on. It never relaxes the existing role_permissions policy. |
| `GENUS_AUTH_SIGNING_KEY` | str | _(unset)_ | yes | yes | legacy | Key session tokens are signed with; at least 32 bytes. Required in production, where startup fails without it. |
| `GENUS_BRIDGE_SSO_SECRET` | str | _(unset)_ | yes | yes | legacy | Shared secret the dashboard and bridge exchange SSO assertions with. The two must match or every sign-in is refused. |
| `GENUS_ENVIRONMENT` | str | _(empty)_ | yes | no | legacy | Deployment environment. 'production' makes the auth preconditions hard requirements instead of warnings. Also read from `ROBOTHOR_ENVIRONMENT`. |
| `GENUS_INSECURE_DEV_MODE` | bool | `false` | yes | no | legacy | Skip authentication for local development. Rejected outright in a production environment or on any non-loopback bind address. |
| `GENUS_OIDC_ISSUERS` | str | _(empty)_ | yes | no | legacy | Comma-separated OIDC issuer URLs whose tokens the bridge accepts. |
| `ROBOTHOR_BRIDGE_HOST` | str | `127.0.0.1` | yes | no | legacy | Address the bridge binds to. Anything but loopback requires real authentication to be configured. |
| `ROBOTHOR_BRIDGE_PORT` | int | `9100` | yes | no | legacy | Bridge HTTP port. |
| `ROBOTHOR_DEFAULT_SERVICE_ROLE` | str | `service` | yes | no | legacy | Role assigned to internal service callers. A fresh install with this unseeded denies every agent every tool. |

## flags

Guardrails and feature gates. Ones marked governed are inventoried in `infra/flags.yaml` with an owner and a promotion date.

| Variable | Type | Default | Restart | Secret | Since | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `ROBOTHOR_ACCRETION_ENABLED` | bool | `false` | yes | no | legacy | **governed.** Let agents accrete durable notes from their runs into the workspace. |
| `ROBOTHOR_ADMISSION_ENABLED` | bool | `false` | yes | no | legacy | **governed.** Run the admission check that refuses work an agent is not equipped to do rather than letting it fail late. |
| `ROBOTHOR_ADMISSION_MODE` | str | `observe` | yes | no | legacy | **governed.** Admission ladder position: observe logs refusals, enforce applies them. |
| `ROBOTHOR_CURATOR_APPLY` | bool | `false` | yes | no | legacy | **governed.** Let the memory curator write its proposed edits. Off, it only proposes — the prompt-trust boundary for accreted content. |
| `ROBOTHOR_DELIVERABLE_CONTRACT_ENABLED` | bool | `false` | yes | no | legacy | **governed.** Hold a run to the deliverable it promised, so 'done' means the artefact exists. |
| `ROBOTHOR_DELIVERABLE_CONTRACT_MODE` | str | `observe` | yes | no | legacy | **governed.** Deliverable-contract ladder position: observe records breaches, enforce fails the run. |
| `ROBOTHOR_DETECTORS_ENABLED` | bool | `true` | yes | no | legacy | Run the background detectors that propose workflows and surface anomalies. Set 0 to disable them all. |
| `ROBOTHOR_DISABLE_ALL_GUARDRAILS` | bool | `false` | yes | no | legacy | **governed.** Master off switch for every guardrail. A break-glass control: an instance running with this set has no safety checks at all. |
| `ROBOTHOR_DNC_MODE` | str | `observe` | yes | no | legacy | **governed.** Do-not-contact ladder position: observe logs an attempted contact of a suppressed person, enforce blocks it. |
| `ROBOTHOR_FEDERATION_ALLOW_INERT_RLS` | bool | `false` | yes | no | legacy | **governed.** Let a federation link activate while row-level security is inert. A deliberate escape hatch: the gate exists because a child could otherwise reach its parent's data. |
| `ROBOTHOR_HA_DEDUP_ENABLED` | bool | `false` | yes | no | legacy | Deduplicate work across engine replicas through Redis instead of in-process only. Off is the correct single-node default. |
| `ROBOTHOR_HA_LEADER_ENABLED` | bool | `false` | yes | no | legacy | Elect a leader among engine replicas so scheduled work runs once. Unset means single-node, where every process is the leader. |
| `ROBOTHOR_PLANNER_ENABLED` | bool | `true` | yes | no | legacy | **governed.** Let the forward planner turn a thread into structured CRM tasks. Set 0 to fall back to stage-3 behaviour. |
| `ROBOTHOR_PLUGIN_MANIFEST_ENABLED` | bool | `true` | yes | no | legacy | **governed.** Validate plugin manifests before a plugin is allowed to load. |
| `ROBOTHOR_PLUGIN_MANIFEST_MODE` | str | `observe` | yes | no | legacy | **governed.** Plugin-manifest ladder position: observe logs violations, enforce refuses to load the plugin. |
| `ROBOTHOR_SANDBOX_ENFORCE_OVERRIDES_MANIFEST` | bool | `false` | yes | no | legacy | **governed.** Make sandbox 'enforce' outrank a manifest's per-agent opt-out. Without it an agent can decline the sandbox it is enforced under. |
| `ROBOTHOR_TODO_ESCALATE_ENABLED` | bool | `true` | yes | no | legacy | Escalate a run's unfinished todos into CRM tasks when it ends, so leftovers are tracked rather than lost with the transcript. |
| `ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED` | bool | `false` | yes | no | legacy | **governed.** Promote a todo's subtasks into their own CRM tasks. |
| `ROBOTHOR_WEB_SEARCH_BROWSER_FALLBACK` | str | _(empty)_ | yes | no | legacy | **governed.** 'on' lets web_search fall back to a real browser when the scraped engines block the host IP — the failure mode behind a whole day of 'can't even web search'. |

## services

Side services the instance runs: ports, endpoints and their knobs.

| Variable | Type | Default | Restart | Secret | Since | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `ROBOTHOR_API_PORT` | int | `9099` | yes | no | legacy | RAG orchestrator / API server port. |
| `ROBOTHOR_CAMERA_HEIGHT` | int | `480` | yes | no | legacy | Capture height in pixels. |
| `ROBOTHOR_CAMERA_HLS_PORT` | int | `8890` | yes | no | legacy | HLS port for browser camera playback. |
| `ROBOTHOR_CAMERA_RTSP_PORT` | int | `8554` | yes | no | legacy | RTSP port the media server publishes the camera on. 0 disables the tunnel entry for it. |
| `ROBOTHOR_CAMERA_SOURCE` | str | `/dev/video0` | yes | no | legacy | Capture device or stream URL. |
| `ROBOTHOR_CAMERA_WIDTH` | int | `640` | yes | no | legacy | Capture width in pixels. |
| `ROBOTHOR_DESKTOP_DISPLAY` | str | `:99` | yes | no | legacy | X display the headless desktop and browser tools attach to. |
| `ROBOTHOR_HELM_PORT` | int | `3004` | yes | no | legacy | Helm control-centre dashboard port. |
| `ROBOTHOR_MONITORING_PORT` | int | `3010` | yes | no | legacy | Monitoring dashboard port. |
| `ROBOTHOR_ORCHESTRATOR_PORT` | int | `9099` | yes | no | legacy | Orchestrator service port. |
| `ROBOTHOR_SEARXNG_PORT` | int | `8888` | yes | no | legacy | SearXNG metasearch port. |
| `ROBOTHOR_SEARXNG_URL` | str | _(empty)_ | yes | no | legacy | Full SearXNG base URL for web search. Empty derives one from the port. |
| `ROBOTHOR_SIEM_SYSLOG_HOST` | str | _(empty)_ | yes | no | legacy | Host audit events are sent to as RFC5424 syslog over UDP. Empty disables syslog forwarding. |
| `ROBOTHOR_SIEM_SYSLOG_PORT` | int | `514` | yes | no | legacy | UDP port for syslog audit forwarding. |
| `ROBOTHOR_SIEM_WEBHOOK_URL` | str | _(unset)_ | yes | yes | legacy | Audit events are POSTed here as JSON (Splunk HEC, Datadog, generic). Empty disables webhook forwarding. Held as a secret: an HEC or Datadog collector URL carries its ingest token. |
| `ROBOTHOR_TTS_PORT` | int | `8880` | yes | no | legacy | Local text-to-speech service port. |
| `ROBOTHOR_TTS_VOICE` | str | `am_michael` | yes | no | legacy | Voice the local TTS service speaks with. |
| `ROBOTHOR_TUNNEL_PROVIDER` | str | `none` | yes | no | legacy | Ingress provider for port-bearing services ('cloudflare' or 'none'). |
| `ROBOTHOR_VISION_MODE` | str | `disarmed` | yes | no | legacy | Whether the camera pipeline is armed. 'disarmed' is the default: the service runs but does not look. |
| `ROBOTHOR_VISION_PORT` | int | `8600` | yes | no | legacy | Vision service port (object and face detection). |
| `ROBOTHOR_VISION_SNAPSHOT_RETENTION_DAYS` | int | `14` | yes | no | legacy | How long vision snapshots are kept before deletion. |
| `ROBOTHOR_VOICE_PORT` | int | `8765` | yes | no | legacy | Voice service port. |

## secrets

Secret material that belongs to no single service.

| Variable | Type | Default | Restart | Secret | Since | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `ROBOTHOR_ENV_FILE` | str | `/etc/robothor/robothor.env` | yes | no | legacy | The EnvironmentFile systemd loads secrets from. It is instance data, never in git; the path is what the platform declares. |
| `ROBOTHOR_INTENT_HMAC_SECRET` | str | _(unset)_ | yes | yes | legacy | Key that signs memory intents so a stored intent cannot be forged. Signing raises rather than falling back when it is unset. |

## substrate

Where and how the instance runs: host accounts, federation, backups.

| Variable | Type | Default | Restart | Secret | Since | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `GENUS_ALLOW_DEPLOYMENT_LAG` | bool | `false` | yes | no | legacy | Let the version-consistency check pass while the deployed version trails the released one. An escape hatch for a deliberate hold, not a way to stop noticing that a promotion was lost. |
| `GENUS_OS_DEPLOYED_AT` | str | _(empty)_ | yes | no | legacy | Timestamp the Helm chart stamps onto every pod, so a running container can say when it was deployed rather than when it booted. |
| `GENUS_OS_DEPLOYED_FROM_PR` | str | _(empty)_ | yes | no | legacy | PR number a staging deployment came from, stamped by the Helm chart. Empty on a production release, which comes from a tag. |
| `GENUS_OS_IMAGE_TAG` | str | _(empty)_ | yes | no | legacy | Container image tag the Helm chart stamped onto the pod. Production pins an exact vX.Y.Z; staging pins pr-N-sha-<short>. |
| `GENUS_PRODUCTION_URL` | str | _(empty)_ | yes | no | legacy | Repository variable supplying the production base URL the release workflow smoke-tests. Empty falls back to the workflow's default. |
| `GENUS_SNAPSHOT_REPOSITORY` | str | _(empty)_ | yes | no | legacy | Default repository `genus snapshot` reads and writes when --repository is not given. |
| `GENUS_URL` | str | _(empty)_ | yes | no | legacy | Base URL the release workflow smoke-tests after a deploy, calling /api/live and /api/ready on it. |
| `ROBOTHOR_BACKUP_GROUP` | str | _(empty)_ | yes | no | legacy | Which backup group a templated base-backup unit instance handles. |
| `ROBOTHOR_BOOT_LOOP_LIMIT` | int | `3` | yes | no | legacy | Restarts within the boot-loop window before the guard stops restarting and pages instead. |
| `ROBOTHOR_BOOT_LOOP_WINDOW` | int | `900` | yes | no | legacy | Seconds the boot-loop counter spans. |
| `ROBOTHOR_DEV` | bool | `false` | yes | no | legacy | Set inside the development container image. Marks a build that carries dev tooling and must not be what production runs. |
| `ROBOTHOR_GPU_CLOCK_CAP_MHZ` | int | `0` | yes | no | legacy | Upper GPU clock cap applied by the thermal guard. 0 leaves it alone. |
| `ROBOTHOR_GPU_CLOCK_MIN_MHZ` | int | `0` | yes | no | legacy | Lower GPU clock bound the guard will not throttle below. 0 leaves it alone. |
| `ROBOTHOR_INSTANCE_ID` | str | _(empty)_ | yes | no | legacy | Stable id of this instance in a federation. Both sides must agree or a link mints two different connection ids and carries no messages. |
| `ROBOTHOR_INSTANCE_NAME` | str | _(empty)_ | yes | no | legacy | Human-readable name of this instance, shown to federated peers. |
| `ROBOTHOR_LIVENESS_FAILURE_THRESHOLD` | int | `3` | yes | no | legacy | Consecutive failed probes before the guard acts. |
| `ROBOTHOR_LIVENESS_TIMEOUT` | int | `10` | yes | no | legacy | Seconds one liveness probe may take. |
| `ROBOTHOR_LIVENESS_UNIT` | str | `robothor-engine.service` | yes | no | legacy | systemd unit the liveness guard restarts when the probe keeps failing. |
| `ROBOTHOR_LIVENESS_URL` | str | `http://127.0.0.1:18800/live` | yes | no | legacy | URL the liveness guard polls to decide the engine is alive. |
| `ROBOTHOR_NATS_CONFIG` | str | `/etc/nats/nats-server.conf` | yes | no | legacy | Path to the NATS server config federation provisioning edits. Note that subjects containing a dash must be quoted there or the account silently fails to load. |
| `ROBOTHOR_NATS_ENABLED` | bool | `false` | yes | no | legacy | Connect to the NATS broker that carries federation traffic. |
| `ROBOTHOR_NATS_PASSWORD` | str | _(unset)_ | yes | yes | legacy | NATS account password. |
| `ROBOTHOR_NATS_URL` | str | _(unset)_ | yes | yes | legacy | NATS broker URL. Empty leaves federation transport unconfigured. Held as a secret: the nats:// form accepts inline user:pass@ credentials, and instances do set it that way. |
| `ROBOTHOR_NATS_USER` | str | _(empty)_ | yes | no | legacy | NATS account user. |
| `ROBOTHOR_OFFSITE_KEEP` | int | `7` | yes | no | legacy | Offsite backup generations kept. This is the recovery window: set it deliberately rather than inheriting the default. |
| `ROBOTHOR_OFFSITE_REMOTE` | str | _(empty)_ | yes | no | legacy | rclone remote backups are synced to. Empty means no offsite copy. |
| `ROBOTHOR_OFFSITE_VERIFY_ONLY` | bool | `false` | yes | no | legacy | Only byte-compare the local dumps against the remote instead of uploading — what the verify and restore-drill units run. |
| `ROBOTHOR_PUBLIC_ENDPOINT` | str | _(empty)_ | yes | no | legacy | Address federated peers reach this instance on. |
| `ROBOTHOR_SERVICE_GROUP` | str | _(empty)_ | yes | no | legacy | OS group for the service account. Empty means the same name as the service user. |
| `ROBOTHOR_SERVICE_HOME` | str | _(empty)_ | yes | no | legacy | Home directory of the service account, as referenced by the units. |
| `ROBOTHOR_SERVICE_USER` | str | `robothor` | yes | no | legacy | OS account the systemd units run as. |
| `ROBOTHOR_SHED_POLL` | int | `2` | yes | no | legacy | Seconds between load-shedding temperature polls. |
| `ROBOTHOR_SHED_RECOVER_C` | int | `78` | yes | no | legacy | Temperature (C) shed work is restored below. |
| `ROBOTHOR_SHED_STAGE1_C` | int | `82` | yes | no | legacy | Temperature (C) that begins stage-1 load shedding. |
| `ROBOTHOR_SHED_STAGE1_SUSTAIN` | int | `6` | yes | no | legacy | Consecutive polls above the stage-1 threshold before shedding starts. |
| `ROBOTHOR_SHED_STAGE2_C` | int | `86` | yes | no | legacy | Temperature (C) that begins stage-2 load shedding. |
| `ROBOTHOR_SLO_HEARTBEAT_AGENT` | str | `main` | yes | no | legacy | Agent whose heartbeat freshness the SLO watcher treats as the instance's pulse. |
| `ROBOTHOR_SLO_JOURNALCTL_CMD` | str | `journalctl` | yes | no | legacy | Command the SLO watcher reads the journal with — overridable so the checks can be exercised off a real systemd host. |
| `ROBOTHOR_SLO_KEY_POOL_CMD` | str | _(empty)_ | yes | no | legacy | Command that reports credential-pool health. Empty skips the check, which is how a capped key stayed invisible for a day. |
| `ROBOTHOR_SLO_OS_USER` | str | _(empty)_ | yes | no | legacy | OS account the SLO watcher runs its database checks as, so PGUSER resolves to a real role. |
| `ROBOTHOR_SOAK_ARGS` | str | _(empty)_ | yes | no | legacy | JSON blob of arguments the federation soak passes to its child processes. Set by the harness, not by an operator. |
| `ROBOTHOR_THERMAL_CRIT_C` | int | `94` | yes | no | legacy | Temperature (C) treated as critical, where work is shed immediately. |
| `ROBOTHOR_THERMAL_NORMAL_PCT` | int | `65` | yes | no | legacy | CPU frequency cap (percent) the guard holds during normal operation. |
| `ROBOTHOR_THERMAL_RESTORE_C` | int | `75` | yes | no | legacy | Temperature (C) normal operation resumes below. |
| `ROBOTHOR_THERMAL_THROTTLE_C` | int | `85` | yes | no | legacy | Temperature (C) the guard starts throttling at. |
| `ROBOTHOR_THERMAL_WARN_C` | int | `90` | yes | no | legacy | Temperature (C) the thermal guard warns at. |
| `ROBOTHOR_USER` | str | `robothor` | yes | no | legacy | Account infra/setup.sh chowns the workspace and log directory to during provisioning. The same account the units later run as. |

## ops

Backups, restores, SLO probes, alert delivery and the volume guard — read by shell, not by Python, which is why they were the last thing anyone declared.

| Variable | Type | Default | Restart | Secret | Since | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `ROBOTHOR_ALERT_FALLBACK_STATE_DIR` | str | _(empty)_ | yes | no | legacy | Cooldown directory used when the primary one is not writable -- a read-only /run must not silently disable deduplication. |
| `ROBOTHOR_ALERT_JOURNAL_CMD` | str | `journalctl` | yes | no | legacy | Command the alert sender reads a failing unit's recent log from, to put context in the page. |
| `ROBOTHOR_ALERT_JOURNAL_TAIL_BYTES` | int | `500` | yes | no | legacy | Bytes of journal tail included in an alert body. |
| `ROBOTHOR_ALERT_MAX_ATTEMPTS` | int | `10` | yes | no | legacy | Delivery attempts the alert sender makes before giving up. |
| `ROBOTHOR_ALERT_RETRY_DELAY` | int | `30` | yes | no | legacy | Seconds between alert delivery attempts. |
| `ROBOTHOR_ALERT_STATE_DIR` | str | `/run/robothor/alert-cooldown` | yes | no | legacy | Directory the alert sender keeps per-unit cooldown markers in, so a flapping unit pages once rather than once per failure. |
| `ROBOTHOR_ALERT_SUPPRESS` | str | _(empty)_ | yes | no | legacy | Any non-empty value makes the alert sender drop the alert and say so on stderr. For a planned maintenance window only -- an instance left with this set pages for nothing. |
| `ROBOTHOR_BACKUP_LOG` | str | _(empty)_ | yes | no | legacy | Log file the SSD backup writes to. Empty picks a default under the log directory. |
| `ROBOTHOR_BACKUP_MOUNT` | str | `/mnt/robothor-backup` | yes | no | legacy | Mount point of the backup volume. Every backup job refuses to run when this is not a real, separate mount. |
| `ROBOTHOR_BASEBACKUP_DIR` | str | `/mnt/robothor-backup/robothor/basebackup` | yes | no | legacy | Directory pg_basebackup writes to and the WAL offsite job reads. |
| `ROBOTHOR_BASEBACKUP_KEEP` | int | `3` | yes | no | legacy | Base backup generations kept on the local volume. |
| `ROBOTHOR_BOOT_HISTORY` | str | `/var/lib/robothor/boot-history` | yes | no | legacy | File the boot guard records recent boots in to detect a boot loop. |
| `ROBOTHOR_CRON_ALERT_MAX_ATTEMPTS` | int | `2` | yes | no | legacy | Delivery attempts for an alert raised by the cron wrapper. Lower than the default: a cron job must not sit retrying a page. |
| `ROBOTHOR_CRON_ALERT_RETRY_DELAY` | int | `15` | yes | no | legacy | Seconds between cron-wrapper alert delivery attempts. |
| `ROBOTHOR_CRYPTTAB` | str | `/etc/crypttab` | yes | no | legacy | crypttab the volume guard resolves the backup container's UUID from. |
| `ROBOTHOR_EXTRA_PATH` | str | _(empty)_ | yes | no | legacy | Directory prepended to PATH before the guardrail scripts pin their own. A test seam: the suites point it at stub binaries. |
| `ROBOTHOR_FIXED_PATH` | str | _(empty)_ | yes | no | legacy | PATH the cron wrapper captured at start and restores for the job it runs, so a cron entry does not inherit cron's near-empty PATH. |
| `ROBOTHOR_INHIBIT_FLAG` | str | `/run/robothor/INHIBIT_INFERENCE` | yes | no | legacy | Marker whose presence stops the box taking on inference work -- what the boot guard and the thermal shedder drop to halt the fleet. |
| `ROBOTHOR_INSTANCE_ENV` | str | `/etc/robothor/robothor.env` | yes | no | legacy | EnvironmentFile the cron wrapper sources so a cron job sees the same configuration the systemd units do. |
| `ROBOTHOR_LIVENESS_ALERT_CMD` | str | _(empty)_ | yes | no | legacy | Pager the liveness probe invokes. Empty means send_failure_alert.sh. |
| `ROBOTHOR_LIVENESS_PROBE_CMD` | str | _(empty)_ | yes | no | legacy | Command that decides whether the engine is alive. Empty uses the built-in curl probe. |
| `ROBOTHOR_LIVENESS_STUCK_AGE_SECONDS` | int | `1800` | yes | no | legacy | How long a .stuck marker may stand before the probe treats it as a failure in its own right, so a wedged restart cannot look healthy. |
| `ROBOTHOR_OFFSITE_DROPIN_DIR` | str | _(empty)_ | yes | no | legacy | systemd drop-in directory the offsite job preserves alongside the dumps, so a restore brings back the unit configuration too. |
| `ROBOTHOR_OFFSITE_LOG` | str | _(empty)_ | yes | no | legacy | Log file the offsite sync writes to. Empty picks a default under the log directory. |
| `ROBOTHOR_OFFSITE_SOURCE` | str | `/mnt/robothor-backup/robothor/db` | yes | no | legacy | Local dump directory the offsite sync uploads from. |
| `ROBOTHOR_OFFSITE_VOLUMES` | str | `/mnt/robothor-backup/robothor/docker-volumes` | yes | no | legacy | Local docker-volume dump directory the offsite sync uploads. |
| `ROBOTHOR_PYTHON` | str | _(empty)_ | yes | no | legacy | Interpreter the restore drill runs its built-in notifier with. Empty means the repository's own venv. |
| `ROBOTHOR_RESTART_LEGACY_REQUEST` | str | `/run/robothor/restart-request` | yes | no | legacy | Single-file restart request the handler still honours, from before requests became one file per unit. |
| `ROBOTHOR_RESTART_REQUEST_DIR` | str | `/run/robothor/restart-requests` | yes | no | legacy | Directory the restart handler watches for per-unit restart requests. |
| `ROBOTHOR_RESTORE_DRILL_CREATEDB` | str | `createdb` | yes | no | legacy | createdb the drill makes the scratch database with. |
| `ROBOTHOR_RESTORE_DRILL_DB` | str | `robothor_restore_drill` | yes | no | legacy | Scratch database the restore drill restores into. Never the live one: the drill drops it afterwards. |
| `ROBOTHOR_RESTORE_DRILL_DROPDB` | str | `dropdb` | yes | no | legacy | dropdb the drill cleans the scratch database up with. |
| `ROBOTHOR_RESTORE_DRILL_DROP_TIMEOUT` | int | `300` | yes | no | legacy | Seconds a dropdb of the scratch database may block before the drill gives up, so a stuck connection cannot hang the drill forever. |
| `ROBOTHOR_RESTORE_DRILL_LOCAL_DIR` | str | `/mnt/robothor-backup/robothor/db` | yes | no | legacy | Local dump directory the drill restores from when no offsite remote is configured. |
| `ROBOTHOR_RESTORE_DRILL_NOTIFY_CMD` | str | _(empty)_ | yes | no | legacy | Command that reports the drill's result. Empty uses the built-in notifier. |
| `ROBOTHOR_RESTORE_DRILL_PSQL` | str | `psql` | yes | no | legacy | psql the drill restores with. |
| `ROBOTHOR_RESTORE_DRILL_RCLONE_CMD` | str | `rclone` | yes | no | legacy | rclone the drill fetches an offsite dump with. |
| `ROBOTHOR_RESTORE_DRILL_WORK_DIR` | str | _(empty)_ | yes | no | legacy | Directory an offsite dump is fetched into for the drill. Empty uses a temporary directory. |
| `ROBOTHOR_SECRETS_FILE` | str | `/run/robothor/secrets.env` | yes | no | legacy | Decrypted secrets file the cron wrapper and the alert sender source. It lives on tmpfs; a process that starts before it exists comes up with no credentials at all and fails closed. |
| `ROBOTHOR_SLO_ALERT_CMD` | str | _(empty)_ | yes | no | legacy | Pager the SLO probe invokes. Empty means send_failure_alert.sh. |
| `ROBOTHOR_SLO_BACKUP_COOLDOWN_SECONDS` | int | `43200` | yes | no | legacy | Quiet period between repeat pages about the backup SLO. The probe runs hourly, so without a cooldown one breach pages 24 times a day. |
| `ROBOTHOR_SLO_BASEBACKUP_DIR` | str | _(empty)_ | yes | no | legacy | Base backup directory the SLO probe checks. Empty falls back to ROBOTHOR_BASEBACKUP_DIR. |
| `ROBOTHOR_SLO_BASEBACKUP_MAX_HOURS` | int | `192` | yes | no | legacy | Age budget for the newest base backup (192h = 8 days). |
| `ROBOTHOR_SLO_DB` | str | _(empty)_ | yes | no | legacy | Database the DB-backed SLOs query. Empty falls back to PGDATABASE, then ROBOTHOR_DB_NAME. |
| `ROBOTHOR_SLO_DB_CHECKS` | bool | `true` | yes | no | legacy | Run the DB-backed SLOs (heartbeat delivery and LLM availability). Off leaves both UNMEASURED and is for tests only -- the probe says so loudly on every run. |
| `ROBOTHOR_SLO_GETENT_CMD` | str | `getent` | yes | no | legacy | Command the probe proves the database account exists with, so a missing account is reported rather than read as a passing check. |
| `ROBOTHOR_SLO_GUARDRAIL_COOLDOWN_SECONDS` | int | `43200` | yes | no | legacy | Quiet period between repeat pages about the guardrail-watch SLO. |
| `ROBOTHOR_SLO_GUARDRAIL_WATCH_MAX_HOURS` | int | `26` | yes | no | legacy | How stale the guardrail watcher's last run may be before breaching. |
| `ROBOTHOR_SLO_HEARTBEAT_COOLDOWN_SECONDS` | int | `43200` | yes | no | legacy | Quiet period between repeat pages about the heartbeat SLO. |
| `ROBOTHOR_SLO_ID_CMD` | str | `id` | yes | no | legacy | Command the probe checks its own identity with, deciding whether it needs the runuser hop at all. |
| `ROBOTHOR_SLO_LIVENESS_COOLDOWN_SECONDS` | int | `43200` | yes | no | legacy | Quiet period between repeat pages about the liveness SLO. |
| `ROBOTHOR_SLO_LIVENESS_MAX_HOURS` | int | `1` | yes | no | legacy | How stale the liveness probe's last run may be before it counts as not running at all. |
| `ROBOTHOR_SLO_LLM_COOLDOWN_SECONDS` | int | `21600` | yes | no | legacy | Quiet period between repeat pages about LLM availability. |
| `ROBOTHOR_SLO_LOCAL_DUMP_DIR` | str | `/mnt/robothor-backup/robothor/db` | yes | no | legacy | Nightly dump directory the SLO probe checks the freshness of. |
| `ROBOTHOR_SLO_LOCAL_DUMP_MAX_HOURS` | int | `26` | yes | no | legacy | Age budget for the nightly local dump before the SLO breaches. |
| `ROBOTHOR_SLO_OFFSITE_MAX_HOURS` | int | `26` | yes | no | legacy | Age budget for the offsite copy before the SLO breaches. |
| `ROBOTHOR_SLO_PROBE_TIMEOUT` | int | `20` | yes | no | legacy | Seconds one disk step of the probe may take. A dropped mount hangs rather than erroring, so every step is bounded. |
| `ROBOTHOR_SLO_PSQL_CMD` | str | _(empty)_ | yes | no | legacy | psql the DB-backed SLOs run through. Empty uses the database hop. |
| `ROBOTHOR_SLO_RCLONE_CMD` | str | `rclone` | yes | no | legacy | rclone the probe lists the offsite copy with. |
| `ROBOTHOR_SLO_RUNUSER_CMD` | str | `runuser` | yes | no | legacy | Command the probe hops to the database account with. |
| `ROBOTHOR_SLO_SYSTEMCTL_CMD` | str | `systemctl` | yes | no | legacy | systemctl the probe reads unit timestamps from. |
| `ROBOTHOR_SLO_UPTIME_FILE` | str | `/proc/uptime` | yes | no | legacy | Where the probe reads host uptime from, so it does not breach an SLO for a window the box spent powered off. |
| `ROBOTHOR_SLO_VOLUME_CHECK_CMD` | str | _(empty)_ | yes | no | legacy | Volume probe the SLO check runs; the dump directory is appended to it. Empty means backup-volume-check.sh --ro. |
| `ROBOTHOR_SYSTEMCTL` | str | _(empty)_ | yes | no | legacy | systemctl the instance doctor interrogates for unit enabled/active state. Empty means the doctor skips the systemd checks. |
| `ROBOTHOR_TELEGRAM_API_BASE` | str | `https://api.telegram.org` | yes | no | legacy | Telegram API base the shell alert sender posts to. Overridable so the delivery path can be tested without sending a real message. |
| `ROBOTHOR_THERMAL_THROTTLE_PCT` | int | `50` | yes | no | legacy | CPU frequency cap (percent) the thermal guard applies once the throttle threshold is crossed. |
| `ROBOTHOR_VOLUME_CHECK` | str | _(empty)_ | yes | no | legacy | Volume probe the backup jobs run before writing. Empty means the backup-volume-check.sh next to them. |
| `ROBOTHOR_VOLUME_GUARD_ALERT_CMD` | str | _(empty)_ | yes | no | legacy | Pager the guard invokes. Empty means send_failure_alert.sh. |
| `ROBOTHOR_VOLUME_GUARD_CHECK_CMD` | str | _(empty)_ | yes | no | legacy | Volume probe the guard runs. Empty means backup-volume-check.sh. |
| `ROBOTHOR_VOLUME_GUARD_DEV_DIR` | str | `/dev/disk/by-uuid` | yes | no | legacy | by-uuid directory the guard resolves the backing device through, so a drive that came back on a different USB path is still found. |
| `ROBOTHOR_VOLUME_GUARD_HEAL` | bool | `true` | yes | no | legacy | Let the guard reopen and remount a dropped backup volume. Off, it pages and leaves the volume down. |
| `ROBOTHOR_VOLUME_GUARD_MAPPER` | str | `robothor-backup` | yes | no | legacy | crypttab name of the encrypted backup container the guard reopens. |
| `ROBOTHOR_VOLUME_GUARD_MAPPER_DIR` | str | `/dev/mapper` | yes | no | legacy | Device-mapper directory the guard looks for the container in. |
| `ROBOTHOR_VOLUME_GUARD_REPAGE_SECONDS` | int | `86400` | yes | no | legacy | Quiet period before the guard pages again about a volume that is still down. |
| `ROBOTHOR_VOLUME_GUARD_STATE_DIR` | str | `/run/robothor/volume-guard` | yes | no | legacy | Directory the volume guard records what it has already paged about. |
| `ROBOTHOR_VOLUME_PROBE_TIMEOUT` | int | `20` | yes | no | legacy | Seconds one volume-probe step may take. A wedged mount answers nothing rather than answering 'no', so every step is bounded. |
| `ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT` | bool | `true` | yes | no | legacy | Require the backup path to be its own mount. Off, a dropped drive means backups quietly land on the root filesystem instead. |
| `ROBOTHOR_WAL_KEEP_DAYS` | int | `8` | yes | no | legacy | Days of archived WAL kept before pruning. Must outlast the oldest base backup or that backup cannot be replayed forward. |
| `ROBOTHOR_WAL_MIN_FREE_MB` | int | `5120` | yes | no | legacy | Free megabytes the WAL archiver requires before accepting a segment. Below it the archive command fails, which is what stops PostgreSQL filling the disk. |

## Deprecated names

These older names are still read, but each emits one `DeprecationWarning` per
process naming its replacement. They stop being read after two minor releases.

| Deprecated name | Use instead |
| --- | --- |
| `CODEX_HOME` | `ROBOTHOR_CODEX_HOME` |
| `OLLAMA_URL` | `ROBOTHOR_OLLAMA_URL` |
| `ROBOTHOR_ENVIRONMENT` | `GENUS_ENVIRONMENT` |
| `ROBOTHOR_OWNER_EMAIL` | `~/.robothor/owner.yaml` |
| `ROBOTHOR_OWNER_NAME` | `~/.robothor/owner.yaml` |
| `TELEGRAM_BOT_TOKEN` | `ROBOTHOR_TELEGRAM_BOT_TOKEN` |
| `TELEGRAM_CHAT_ID` | `ROBOTHOR_TELEGRAM_CHAT_ID` |
