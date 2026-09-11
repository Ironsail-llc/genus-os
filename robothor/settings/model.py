"""``GenusSettings`` — the typed declaration of every Genus OS setting.

This module is the answer to "what can I configure?". Every environment
variable the platform reads is a field here, with the exact variable name, a
description of what reading it does, whether a change needs a restart, whether
it holds a credential, and when it appeared. ``tests/test_settings_registry.py``
fails if a reader exists for a name that is not declared, so the list cannot
drift out of date the way a hand-written ``configuration.md`` did.

Structure
---------
``GenusSettings`` is a shallow tree of groups (``paths``, ``database``,
``redis``, ``ollama``, ``providers``, ``engine``, ``channels``, ``auth``,
``flags``, ``services``, ``secrets``, ``substrate``). Groups are plain
``BaseModel``s, populated by the sources in :mod:`robothor.settings.sources`,
which resolve each leaf by its declared environment name rather than by
deriving one from the field name -- deriving is how ``ROBOTHOR_DB_SSLMODE``
and a field called ``ssl_mode`` stop agreeing.

Precedence, lowest to highest: field defaults < the ``settings:`` block of
``<workspace>/.robothor/config.yaml`` < environment < runtime overrides passed
to ``get_settings(**overrides)``.

Scope
-----
Declaring a setting here does NOT yet change who reads it. Every existing
``os.environ`` reader still works exactly as before; the ratchet in
``tests/test_settings_registry.py`` is what moves them, one PR at a time.
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["GenusSettings", "SettingsGroup", "declare"]


def declare(
    default: Any,
    env: str,
    description: str,
    *,
    aliases: tuple[str, ...] = (),
    restart_required: bool = True,
    secret: bool = False,
    since: str = "legacy",
    governed: bool = False,
) -> Any:
    """Declare one setting.

    Args:
        default: the value used when nothing configures this setting. Must be
            empty for anything marked ``secret`` -- a default credential is a
            credential in git.
        env: the EXACT environment variable name, written out rather than
            derived from the field name.
        description: what reading this setting does, in terms of the behaviour
            an operator would observe. Never "TODO".
        aliases: older names still honoured. Deprecated ones additionally
            appear in :mod:`robothor.settings.aliases` so reading them warns.
        restart_required: True when a change only takes effect on restart.
            False marks the hot-reloadable settings (log level, cost caps,
            concurrency) that a later ``genus config set`` applies live.
        secret: True for credentials -- redacted by the CLI and the doc
            generator, and never given a default.
        since: the release that introduced the setting, or ``"legacy"`` for
            everything that predates this registry.
        governed: True when the setting is a guardrail tracked in
            ``infra/flags.yaml`` and promoted through a mode ladder.
    """
    return Field(
        default=default,
        description=description,
        validation_alias=AliasChoices(env, *aliases),
        json_schema_extra={
            "env": env,
            "restart_required": restart_required,
            "secret": secret,
            "since": since,
            "aliases": list(aliases),
            "governed": governed,
        },
    )


class SettingsGroup(BaseModel):
    """Base for every group.

    ``extra="forbid"`` is what turns a typo in ``config.yaml`` into an error
    naming the key instead of a setting that silently never applies.
    ``populate_by_name`` lets the sources address fields by their Python name
    while ``validation_alias`` stays pure declaration.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


class PathsSettings(SettingsGroup):
    """Where the instance keeps its files."""

    workspace: str = declare(
        "",
        "ROBOTHOR_WORKSPACE",
        "Root of the instance workspace: agent manifests, workflows, brain, "
        "memory and the .robothor config directory all resolve under it. "
        "Empty means ~/robothor.",
    )
    memory_dir: str = declare(
        "",
        "ROBOTHOR_MEMORY_DIR",
        "Directory holding memory artefacts (vision mode marker, projections, "
        "eval corpora). Empty means <workspace>/memory.",
    )
    log_dir: str = declare(
        "/var/log/robothor",
        "ROBOTHOR_LOG_DIR",
        "Directory the service units write log files to.",
    )
    adapter_dir: str = declare(
        "",
        "ROBOTHOR_ADAPTER_DIR",
        "Directory scanned for LoRA/model adapters. Empty means ~/.config/robothor/adapters.",
    )
    manifest_dir: str = declare(
        "",
        "ROBOTHOR_MANIFEST_DIR",
        "Directory of agent manifests (docs/agents/*.yaml). These are the "
        "source of truth for the fleet. Empty means <workspace>/docs/agents.",
    )
    workflow_dir: str = declare(
        "",
        "ROBOTHOR_WORKFLOW_DIR",
        "Directory of workflow definitions. Empty means <workspace>/docs/workflows.",
    )
    agents_dir: str = declare(
        "",
        "ROBOTHOR_AGENTS_DIR",
        "Directory the bridge's fleet API lists agent manifests from. Empty "
        "means the manifest directory under the workspace.",
    )
    template_dir: str = declare(
        "",
        "ROBOTHOR_TEMPLATE_DIR",
        "Explicit override for the scaffold templates `genus init` copies. "
        "Empty falls back to the packaged templates/ directory.",
    )
    dropin_dir: str = declare(
        "",
        "ROBOTHOR_DROPIN_DIR",
        "Directory of systemd drop-in fragments the instance-env reader "
        "reconciles against the running units.",
    )
    capabilities_manifest: str = declare(
        "",
        "ROBOTHOR_CAPABILITIES_MANIFEST",
        "Explicit path to the agent capabilities manifest. Empty searches "
        "<workspace>/agent_capabilities.json and then the packaged copy.",
    )
    services_manifest: str = declare(
        "",
        "ROBOTHOR_SERVICES_MANIFEST",
        "Explicit path to robothor-services.json, the service/port registry "
        "shared by the engine and the dashboard.",
    )
    manifest_guard_state: str = declare(
        "/run/robothor/manifest-guard-alerts.json",
        "ROBOTHOR_MANIFEST_GUARD_STATE",
        "File the manifest guard stores alert-dedup timestamps in, so a "
        "broken manifest does not page once per scheduler tick.",
    )
    model_breaker_state: str = declare(
        "/run/robothor/model-breaker-alerts.json",
        "ROBOTHOR_MODEL_BREAKER_STATE",
        "File the model circuit breaker stores alert-dedup timestamps in.",
    )
    watchdog_trace_file: str = declare(
        "",
        "ROBOTHOR_WATCHDOG_TRACE_FILE",
        "When set, the stall watchdog appends a stack trace of the stalled "
        "run to this file before it acts. Empty disables tracing.",
    )
    rlm_log_dir: str = declare(
        "",
        "ROBOTHOR_RLM_LOG_DIR",
        "Directory the recursive language model tool writes per-call logs to. "
        "Empty means <workspace>/logs/rlm.",
    )
    backup_state_dir: str = declare(
        "",
        "ROBOTHOR_BACKUP_STATE_DIR",
        "Directory the backup guardrail reads freshness markers from when "
        "deciding whether backups have gone stale.",
    )
    slo_state_dir: str = declare(
        "",
        "ROBOTHOR_SLO_STATE_DIR",
        "Directory the SLO watcher keeps its per-check state in, so a breach "
        "is reported once rather than every poll.",
    )
    liveness_state_dir: str = declare(
        "/run/robothor/fleet-guard",
        "ROBOTHOR_LIVENESS_STATE_DIR",
        "Directory the fleet liveness guard counts consecutive failures in.",
    )
    alert_spool_dir: str = declare(
        "/var/lib/robothor/alert-spool",
        "ROBOTHOR_ALERT_SPOOL_DIR",
        "Directory alerts are spooled to when delivery fails, so an outage of "
        "the notification channel does not lose the alert.",
    )
    wal_archive_dir: str = declare(
        "/var/lib/robothor/wal_archive",
        "ROBOTHOR_WAL_ARCHIVE_DIR",
        "Directory PostgreSQL WAL segments are archived to before the offsite sync picks them up.",
    )
    snapshot_staging_dir: str = declare(
        "",
        "GENUS_SNAPSHOT_STAGING_DIR",
        "Directory `genus snapshot` stages an export in before it is packed. "
        "Must already exist; empty uses a temporary directory.",
    )
    codex_home: str = declare(
        "",
        "ROBOTHOR_CODEX_HOME",
        "CODEX_HOME for the codex subscription provider — the directory "
        "holding its auth state. Empty falls back to CODEX_HOME.",
        aliases=("CODEX_HOME",),
    )
    codex_bin: str = declare(
        "codex",
        "ROBOTHOR_CODEX_BIN",
        "Name or path of the codex binary the codex provider executes.",
    )


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------


class DatabaseSettings(SettingsGroup):
    """PostgreSQL connection, tenancy and row-level security."""

    host: str = declare(
        "",
        "ROBOTHOR_DB_HOST",
        "PostgreSQL host. An empty value means a Unix socket with peer "
        "authentication, which is the default on a single-box install.",
    )
    port: int = declare(5432, "ROBOTHOR_DB_PORT", "PostgreSQL TCP port.")
    name: str = declare(
        "robothor_memory",
        "ROBOTHOR_DB_NAME",
        "Database holding CRM, memory and engine tables.",
    )
    user: str = declare(
        "",
        "ROBOTHOR_DB_USER",
        "PostgreSQL role. Empty falls back to $USER, then 'robothor'. Must be "
        "a non-superuser for row-level security to actually apply.",
    )
    password: str = declare(
        "",
        "ROBOTHOR_DB_PASSWORD",
        "PostgreSQL password. Unset with a Unix-socket host (peer auth).",
        secret=True,
    )
    ssl_mode: str = declare(
        "",
        "ROBOTHOR_DB_SSLMODE",
        "libpq sslmode for the connection (disable/allow/prefer/require/"
        "verify-ca/verify-full). Empty leaves libpq's own default.",
    )
    connect_timeout: int = declare(
        5,
        "ROBOTHOR_DB_CONNECT_TIMEOUT",
        "Seconds to wait for a connection before failing, so a wedged "
        "database stalls one call rather than the process.",
    )
    rls_enabled: bool = declare(
        False,
        "ROBOTHOR_RLS_ENABLED",
        "Bind every connection to a tenant so PostgreSQL row-level security "
        "applies. Inert unless the DB user is a non-superuser; federation "
        "refuses to activate a link while it is off.",
        governed=True,
    )
    tenant_id: str = declare(
        "",
        "ROBOTHOR_TENANT_ID",
        "The tenant this process operates AS — what the RLS connection binds "
        "to. Must agree with ROBOTHOR_DEFAULT_TENANT or every default-tenant "
        "write is refused by the RLS WITH CHECK and the caller gets None.",
    )
    default_tenant: str = declare(
        "default",
        "ROBOTHOR_DEFAULT_TENANT",
        "The tenant DAL calls tag rows with when the caller names none. Read "
        "at import time into robothor.constants.DEFAULT_TENANT.",
    )
    platform_tenant: str = declare(
        "",
        "ROBOTHOR_PLATFORM_TENANT",
        "Tenant the bridge treats as the platform operator for owner-only "
        "endpoints. Empty means the default tenant.",
    )
    benchmark_tenant: str = declare(
        "",
        "ROBOTHOR_BENCHMARK_TENANT",
        "Tenant benchmark runs are confined to, so a harness can never touch "
        "production CRM rows. Empty means the built-in sandbox tenant.",
    )
    test_db_allow: str = declare(
        "",
        "ROBOTHOR_TEST_DB_ALLOW",
        "Exact database name allowed under pytest besides a *_test database. "
        "The release gate legitimately runs against one; nothing else should.",
    )
    soak_template: str = declare(
        "robothor_test",
        "ROBOTHOR_SOAK_TEMPLATE",
        "Template database the federation soak clones per instance.",
    )


# ---------------------------------------------------------------------------
# redis
# ---------------------------------------------------------------------------


class RedisSettings(SettingsGroup):
    """Redis connection and the event-bus streams it carries."""

    host: str = declare("127.0.0.1", "ROBOTHOR_REDIS_HOST", "Redis host.")
    port: int = declare(6379, "ROBOTHOR_REDIS_PORT", "Redis TCP port.")
    db: int = declare(
        0,
        "ROBOTHOR_REDIS_DB",
        "Redis logical database. 0 is production; the test suite pins its own "
        "so a plain pytest run cannot XADD onto live streams.",
    )
    password: str = declare(
        "", "ROBOTHOR_REDIS_PASSWORD", "Redis password, if the server requires one.", secret=True
    )
    maxmemory: str = declare(
        "2gb",
        "ROBOTHOR_REDIS_MAXMEMORY",
        "maxmemory the provisioning scripts configure the Redis server with.",
    )
    extra_streams: str = declare(
        "",
        "ROBOTHOR_EXTRA_STREAMS",
        "Comma-separated extra event-bus stream names to create and consume "
        "alongside the built-in ones.",
    )


# ---------------------------------------------------------------------------
# ollama / local models
# ---------------------------------------------------------------------------


class OllamaSettings(SettingsGroup):
    """The local Ollama endpoint and the models served from it."""

    url: str = declare(
        "",
        "ROBOTHOR_OLLAMA_URL",
        "Full base URL of the Ollama server. Canonical form; it supersedes "
        "the host/port pair. Empty falls back to host and port.",
        aliases=("OLLAMA_URL",),
    )
    host: str = declare(
        "127.0.0.1",
        "ROBOTHOR_OLLAMA_HOST",
        "Ollama host, used only when no full URL is set.",
    )
    port: int = declare(
        11434,
        "ROBOTHOR_OLLAMA_PORT",
        "Ollama port, used only when no full URL is set.",
    )
    num_ctx: int = declare(
        0,
        "ROBOTHOR_OLLAMA_NUM_CTX",
        "Per-request context-window clamp sent to Ollama. 0 leaves the "
        "model's own default; a non-positive or non-integer value is ignored "
        "with a warning.",
        restart_required=False,
    )
    embedding_model: str = declare(
        "qwen3-embedding:0.6b",
        "ROBOTHOR_EMBEDDING_MODEL",
        "Model used to embed memory and CRM text for vector search.",
    )
    generation_model: str = declare(
        "qwen3:8b",
        "ROBOTHOR_GENERATION_MODEL",
        "Local model used for short generation tasks that never leave the box.",
    )
    reranker_model: str = declare(
        "Qwen3-Reranker-0.6B:F16",
        "ROBOTHOR_RERANKER_MODEL",
        "Cross-encoder that reorders retrieval hits before they reach a prompt.",
    )
    vision_model: str = declare(
        "llama3.2-vision:11b",
        "ROBOTHOR_VISION_MODEL",
        "Vision-language model used to describe camera frames and images.",
    )
    vlm_model: str = declare(
        "llama3.2-vision:11b",
        "ROBOTHOR_VLM_MODEL",
        "Vision-language model the vision service loads for scene captioning.",
    )
    face_model: str = declare(
        "buffalo_l",
        "ROBOTHOR_FACE_MODEL",
        "InsightFace model pack used for face detection and enrolment.",
    )
    yolo_model: str = declare(
        "yolov8n",
        "ROBOTHOR_YOLO_MODEL",
        "YOLO weights the vision service runs object detection with.",
    )


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


class ProviderSettings(SettingsGroup):
    """Cloud model routing, budgets and the failure controls around them."""

    last_resort_model: str = declare(
        "",
        "ROBOTHOR_LAST_RESORT_MODEL",
        "Model appended to every agent's fallback chain so a run always has "
        "somewhere to land when the cloud providers are down. Manifest "
        "validation checks each chain ends here.",
    )
    hourly_cost_cap_usd: float = declare(
        5.0,
        "ROBOTHOR_HOURLY_COST_CAP_USD",
        "Fleet-wide spend ceiling per hour. The engine stops dispatching new "
        "runs once the rolling hour exceeds it.",
        restart_required=False,
    )
    model_breaker_threshold: int = declare(
        3,
        "ROBOTHOR_MODEL_BREAKER_THRESHOLD",
        "Consecutive provider failures on one model before the circuit "
        "breaker opens and traffic moves to the fallback chain.",
    )
    model_breaker_cooldown: int = declare(
        600,
        "ROBOTHOR_MODEL_BREAKER_COOLDOWN",
        "Seconds an opened model breaker stays open before a trial request.",
    )
    model_breaker_alert_dedup: int = declare(
        21600,
        "ROBOTHOR_MODEL_BREAKER_ALERT_DEDUP",
        "Seconds between repeat alerts for the same tripped model.",
    )
    periodic_quota_cooldown_seconds: int = declare(
        21600,
        "ROBOTHOR_PERIODIC_QUOTA_COOLDOWN_SECONDS",
        "How long a credential that hit a periodic (daily/weekly) quota stays "
        "parked. Too short and a WEEKLY cap is retried every few minutes for "
        "days, which is how one capped key stalled the whole fleet.",
    )
    compaction_trigger_tokens: int = declare(
        80000,
        "ROBOTHOR_COMPACTION_TRIGGER_TOKENS",
        "Absolute prompt-token budget above which a run compacts its context.",
        restart_required=False,
    )
    deferred_tools_threshold: int = declare(
        40,
        "ROBOTHOR_DEFERRED_TOOLS_THRESHOLD",
        "Number of tools above which schemas are deferred behind tool search "
        "rather than sent in full on every request.",
        restart_required=False,
    )
    real_tokenizer_enabled: bool = declare(
        False,
        "ROBOTHOR_REAL_TOKENIZER_ENABLED",
        "Count context with litellm's real tokenizer instead of the cheap "
        "character estimate. More accurate, measurably slower.",
    )
    eager_tool_compression: bool = declare(
        False,
        "ROBOTHOR_EAGER_TOOL_COMPRESSION",
        "Fleet default for compressing tool results as soon as they land "
        "rather than at the next compaction. A manifest setting wins over it.",
    )
    rlm_root_model: str = declare(
        "openrouter/anthropic/claude-sonnet-4.6",
        "ROBOTHOR_RLM_ROOT_MODEL",
        "Model the recursive language model tool runs its root call on.",
    )
    rlm_sub_model: str = declare(
        "openrouter/anthropic/claude-haiku-4.5",
        "ROBOTHOR_RLM_SUB_MODEL",
        "Model the recursive language model tool runs child calls on.",
    )
    rlm_max_budget: float = declare(
        2.0,
        "ROBOTHOR_RLM_MAX_BUDGET",
        "USD ceiling for a single recursive language model invocation.",
    )
    rlm_max_timeout: int = declare(
        240,
        "ROBOTHOR_RLM_MAX_TIMEOUT",
        "Wall-clock seconds a single recursive language model call may take.",
    )
    rlm_max_iterations: int = declare(
        30,
        "ROBOTHOR_RLM_MAX_ITERATIONS",
        "Tool-call iterations allowed inside one recursive language model run.",
    )
    rlm_max_depth: int = declare(
        1,
        "ROBOTHOR_RLM_MAX_DEPTH",
        "How deep the recursive language model may nest sub-calls.",
    )


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------


class EngineSettings(SettingsGroup):
    """The agent execution layer: bind address, concurrency, pacing, sandbox."""

    host: str = declare(
        "127.0.0.1",
        "ROBOTHOR_ENGINE_HOST",
        "Address the engine's HTTP surface binds to. Loopback by default; the "
        "auth guard refuses the insecure dev mode on any other address.",
    )
    port: int = declare(18800, "ROBOTHOR_ENGINE_PORT", "Engine HTTP port.")
    url: str = declare(
        "http://127.0.0.1:18800",
        "ROBOTHOR_ENGINE_URL",
        "Base URL the dashboard's server-side client calls the engine on.",
    )
    max_concurrent_agents: int = declare(
        3,
        "ROBOTHOR_MAX_CONCURRENT_AGENTS",
        "How many agent runs may execute at once.",
        restart_required=False,
    )
    max_iterations: int = declare(
        20,
        "ROBOTHOR_MAX_ITERATIONS",
        "Default ceiling on tool-call iterations within one agent run.",
        restart_required=False,
    )
    max_concurrent_spawns: int = declare(
        10,
        "ROBOTHOR_MAX_CONCURRENT_SPAWNS",
        "How many sub-agent spawns may be in flight at once.",
        restart_required=False,
    )
    max_spawn_batch: int = declare(
        10,
        "ROBOTHOR_MAX_SPAWN_BATCH",
        "Largest number of sub-agents one parent may spawn in a single batch.",
        restart_required=False,
    )
    max_wallclock_seconds: int = declare(
        0,
        "ROBOTHOR_MAX_WALLCLOCK_SECONDS",
        "Hard wall-clock ceiling for one run before the stall watchdog ends "
        "it. 0 disables the ceiling.",
        restart_required=False,
    )
    min_agent_count: int = declare(
        1,
        "ROBOTHOR_MIN_AGENT_COUNT",
        "Fewest loaded agents the fleet guard accepts before /ready reports "
        "unhealthy — the tripwire for a manifest directory that went missing.",
    )
    required_agent_ids: str = declare(
        "",
        "ROBOTHOR_REQUIRED_AGENT_IDS",
        "Comma-separated agent ids that must be loaded, or /ready returns 503. "
        "Names a broken manifest instead of letting the fleet run short.",
    )
    allow_empty_fleet: bool = declare(
        True,
        "ROBOTHOR_ALLOW_EMPTY_FLEET",
        "Let the engine start with no agent manifests at all. True suits a "
        "fresh install; false makes an emptied manifest directory fatal.",
    )
    default_chat_agent: str = declare(
        "main",
        "ROBOTHOR_DEFAULT_CHAT_AGENT",
        "Agent an inbound chat is routed to when nothing names one.",
    )
    main_session_key: str = declare(
        "agent:main:primary",
        "ROBOTHOR_MAIN_SESSION_KEY",
        "Session key of the operator's primary conversation, which channel "
        "deliveries are dual-written into.",
    )
    timezone: str = declare(
        "America/New_York",
        "ROBOTHOR_TIMEZONE",
        "IANA timezone schedules and human-facing timestamps are rendered in.",
    )
    operator_name: str = declare(
        "",
        "ROBOTHOR_OPERATOR_NAME",
        "Display name the engine uses for the operator in prompts. Prefer "
        "~/.robothor/owner.yaml, which is the canonical identity source.",
    )
    execution_mode: str = declare(
        "auto",
        "ROBOTHOR_EXECUTION_MODE",
        "Which tier runs agents: auto, cloud, or local. 'local' switches the "
        "budgets as well as the model — cloud-era wall-clock budgets were 78% "
        "of local-tier failures.",
    )
    local_max_concurrent: int = declare(
        0,
        "ROBOTHOR_LOCAL_MAX_CONCURRENT",
        "Concurrency ceiling while on the local tier. 0 lets the host profile "
        "derive one from VRAM and core count.",
        restart_required=False,
    )
    local_gate_wait_seconds: int = declare(
        120,
        "ROBOTHOR_LOCAL_GATE_WAIT_SECONDS",
        "How long a local-tier run waits for a GPU slot before giving up.",
        restart_required=False,
    )
    local_pace_all_c: int = declare(
        85,
        "ROBOTHOR_LOCAL_PACE_ALL_C",
        "GPU temperature (C) at which all local work is paced down.",
        restart_required=False,
    )
    local_pace_background_c: int = declare(
        80,
        "ROBOTHOR_LOCAL_PACE_BACKGROUND_C",
        "GPU temperature (C) at which background local work is paced down.",
        restart_required=False,
    )
    local_pace_resume_c: int = declare(
        75,
        "ROBOTHOR_LOCAL_PACE_RESUME_C",
        "GPU temperature (C) local work resumes full pace below.",
        restart_required=False,
    )
    reserved_interactive_slots: int = declare(
        1,
        "ROBOTHOR_RESERVED_INTERACTIVE_SLOTS",
        "Concurrency slots held back for interactive chat so background work "
        "cannot starve the operator's own conversation.",
        restart_required=False,
    )
    resume_in_flight: bool = declare(
        False,
        "ROBOTHOR_RESUME_IN_FLIGHT",
        "Resume runs that were in flight when the engine restarted. Verify it "
        "from a recovered run, never from the log line it prints itself.",
        governed=True,
    )
    daemon_start_ts: str = declare(
        "",
        "ROBOTHOR_DAEMON_START_TS",
        "ISO timestamp the daemon sets on itself at boot and child processes "
        "read to report uptime. Set by the engine, not by an operator.",
    )
    log_format: str = declare(
        "",
        "ROBOTHOR_LOG_FORMAT",
        "'json' or 'console'. An explicit value always wins; empty picks json "
        "under systemd and console on a terminal.",
    )
    alert_selftest: bool = declare(
        False,
        "ROBOTHOR_ALERT_SELFTEST",
        "Fire one info-level alert shortly after boot to prove the alert path "
        "delivers. Leave off outside a delivery test.",
    )
    alert_webhook_url: str = declare(
        "",
        "ROBOTHOR_ALERT_WEBHOOK_URL",
        "Webhook alerts are POSTed to. Empty skips webhook delivery entirely.",
    )
    alert_cooldown_seconds: int = declare(
        3600,
        "ROBOTHOR_ALERT_COOLDOWN_SECONDS",
        "Minimum seconds between repeats of the same unit-failure alert.",
    )
    alert_spool_cap: int = declare(
        50,
        "ROBOTHOR_ALERT_SPOOL_CAP",
        "Most alerts held on the retry spool; beyond it the oldest are dropped.",
    )
    alert_spool_max_age_seconds: int = declare(
        86400,
        "ROBOTHOR_ALERT_SPOOL_MAX_AGE_SECONDS",
        "Age at which a spooled alert is discarded rather than delivered late.",
    )
    alert_spool_max_attempts: int = declare(
        48,
        "ROBOTHOR_ALERT_SPOOL_MAX_ATTEMPTS",
        "Delivery attempts for one spooled alert before it is given up on.",
    )
    manifest_alert_dedup: int = declare(
        3600,
        "ROBOTHOR_MANIFEST_ALERT_DEDUP",
        "Seconds between repeat alerts about the same broken manifest.",
    )
    sandbox_binary: str = declare(
        "",
        "ROBOTHOR_SANDBOX_BINARY",
        "Container runtime used for sandboxed exec. Empty prefers rootless podman, then docker.",
    )
    sandbox_image: str = declare(
        "robothor-sandbox:latest",
        "ROBOTHOR_SANDBOX_IMAGE",
        "Image sandboxed exec runs agent commands inside.",
    )
    sandbox_network: str = declare(
        "none",
        "ROBOTHOR_SANDBOX_NETWORK",
        "Container network for sandboxed exec. 'none' is the default; only "
        "'bridge' gives sandboxed commands network access.",
    )
    sandbox_default_mode: str = declare(
        "",
        "ROBOTHOR_SANDBOX_DEFAULT_MODE",
        "Fleet default sandbox mode for agents whose manifest names none. "
        "Empty leaves exec unrouted, which is sandboxing in name only.",
        governed=True,
    )
    sandbox_start_retries: int = declare(
        1,
        "ROBOTHOR_SANDBOX_START_RETRIES",
        "Retries when the sandbox container fails to start.",
    )
    sandbox_start_retry_seconds: int = declare(
        3,
        "ROBOTHOR_SANDBOX_START_RETRY_SECONDS",
        "Seconds between sandbox container start retries.",
    )
    memory_ttl_hours: int = declare(
        48,
        "ROBOTHOR_MEMORY_TTL_HOURS",
        "How long short-term memory rows survive before decay considers them.",
    )
    memory_block_max_chars: int = declare(
        5000,
        "ROBOTHOR_MEMORY_BLOCK_MAX_CHARS",
        "Character ceiling on one agent memory block before it is compacted.",
    )
    importance_threshold: float = declare(
        0.3,
        "ROBOTHOR_IMPORTANCE_THRESHOLD",
        "Minimum importance score for an extracted fact to be stored.",
    )
    autodream_unload_below_gb: float = declare(
        24.0,
        "ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB",
        "Free VRAM (GiB) below which the autodream pass unloads local models "
        "rather than competing with live agent work.",
    )
    workflow_streak_window_days: int = declare(
        14,
        "ROBOTHOR_WORKFLOW_STREAK_WINDOW_DAYS",
        "Window the workflow detector counts repeated action streaks over "
        "when proposing a new workflow.",
    )
    trajectory_sample: float = declare(
        0.0,
        "ROBOTHOR_TRAJECTORY_SAMPLE",
        "Fraction of runs (0.0-1.0) whose full trajectory is recorded for "
        "later analysis. Clamped into range.",
        restart_required=False,
    )
    record_assistant_turns: bool = declare(
        False,
        "ROBOTHOR_RECORD_ASSISTANT_TURNS",
        "Persist assistant turns into the session transcript as well as user "
        "turns. Larger transcripts, fuller replay.",
    )
    buddy_grader_dryrun: bool = declare(
        False,
        "ROBOTHOR_BUDDY_GRADER_DRYRUN",
        "Run the verification grader without writing its verdicts, for "
        "checking a grading change against live runs.",
    )
    rip_1_agents: str = declare(
        "",
        "ROBOTHOR_RIP_1_AGENTS",
        "Comma-separated soak allowlist for the background-review rip: when "
        "set, only these agents take the new path.",
    )
    web_fetch_user_agent: str = declare(
        "",
        "ROBOTHOR_WEB_FETCH_USER_AGENT",
        "User-Agent web_fetch sends. Empty uses the built-in string; set it "
        "when a site blocks the default.",
        restart_required=False,
    )


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------


class ChannelSettings(SettingsGroup):
    """How the instance reaches people, and who it says it is."""

    telegram_bot_token: str = declare(
        "",
        "ROBOTHOR_TELEGRAM_BOT_TOKEN",
        "Bot token for the Telegram channel. Empty disables Telegram.",
        aliases=("TELEGRAM_BOT_TOKEN",),
        secret=True,
    )
    telegram_chat_id: str = declare(
        "",
        "ROBOTHOR_TELEGRAM_CHAT_ID",
        "Default Telegram chat deliveries go to when an agent names none.",
        aliases=("TELEGRAM_CHAT_ID",),
    )
    telegram_bot_name: str = declare(
        "",
        "ROBOTHOR_TELEGRAM_BOT_NAME",
        "@name of the Telegram bot, shown on the dashboard so an operator can "
        "find the right conversation.",
    )
    slack_bot_token: str = declare(
        "",
        "ROBOTHOR_SLACK_BOT_TOKEN",
        "Slack bot token. The Slack channel starts only when it and the app token are both set.",
        secret=True,
    )
    slack_app_token: str = declare(
        "",
        "ROBOTHOR_SLACK_APP_TOKEN",
        "Slack app-level token for socket mode.",
        secret=True,
    )
    slack_allowed_users: str = declare(
        "",
        "ROBOTHOR_SLACK_ALLOWED_USERS",
        "Comma-separated Slack user ids allowed to talk to the bot. Empty "
        "means no allowlist, which the channel warns about at start.",
    )
    slack_allowed_channels: str = declare(
        "",
        "ROBOTHOR_SLACK_ALLOWED_CHANNELS",
        "Comma-separated Slack channel ids the bot will respond in.",
    )
    voice_notes_enabled: bool = declare(
        False,
        "ROBOTHOR_VOICE_NOTES_ENABLED",
        "Transcribe inbound voice notes. Off unless a speech-to-text provider "
        "is actually configured.",
    )
    ai_name: str = declare(
        "Genus",
        "ROBOTHOR_AI_NAME",
        "Name the assistant introduces itself with in channels and on the dashboard.",
    )
    ai_email: str = declare(
        "",
        "ROBOTHOR_AI_EMAIL",
        "The bot's own sending address. Distinct from the operator's address: "
        "mail tools use this as the sender, never the operator identity.",
    )
    ai_phone: str = declare(
        "",
        "ROBOTHOR_AI_PHONE",
        "Phone number shown on the assistant's public contact card.",
    )
    ai_domain: str = declare(
        "",
        "ROBOTHOR_AI_DOMAIN",
        "Domain the per-dashboard hostnames are derived from.",
    )
    brand_name: str = declare(
        "Genus OS",
        "ROBOTHOR_BRAND_NAME",
        "Product name shown in dashboard chrome.",
    )
    domain: str = declare(
        "",
        "ROBOTHOR_DOMAIN",
        "Public domain the tunnel generator issues ingress hostnames under.",
    )
    owner_name: str = declare(
        "",
        "ROBOTHOR_OWNER_NAME",
        "DEPRECATED operator display name. Operator identity belongs in "
        "~/.robothor/owner.yaml; this is read only as a legacy fallback.",
    )
    owner_email: str = declare(
        "",
        "ROBOTHOR_OWNER_EMAIL",
        "DEPRECATED operator email. Operator identity belongs in "
        "~/.robothor/owner.yaml; this is read only as a legacy fallback.",
    )


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


class AuthSettings(SettingsGroup):
    """Who may reach the bridge and the dashboard, and how that is proven."""

    environment: str = declare(
        "",
        "GENUS_ENVIRONMENT",
        "Deployment environment. 'production' makes the auth preconditions "
        "hard requirements instead of warnings.",
        aliases=("ROBOTHOR_ENVIRONMENT",),
    )
    auth_enforce: bool = declare(
        False,
        "GENUS_AUTH_ENFORCE",
        "One-way compatibility switch that turns identity checks on. It never "
        "relaxes the existing role_permissions policy.",
        governed=True,
    )
    insecure_dev_mode: bool = declare(
        False,
        "GENUS_INSECURE_DEV_MODE",
        "Skip authentication for local development. Rejected outright in a "
        "production environment or on any non-loopback bind address.",
    )
    signing_key: str = declare(
        "",
        "GENUS_AUTH_SIGNING_KEY",
        "Key session tokens are signed with; at least 32 bytes. Required in "
        "production, where startup fails without it.",
        secret=True,
    )
    bridge_sso_secret: str = declare(
        "",
        "GENUS_BRIDGE_SSO_SECRET",
        "Shared secret the dashboard and bridge exchange SSO assertions with. "
        "The two must match or every sign-in is refused.",
        secret=True,
    )
    oidc_issuers: str = declare(
        "",
        "GENUS_OIDC_ISSUERS",
        "Comma-separated OIDC issuer URLs whose tokens the bridge accepts.",
    )
    bridge_host: str = declare(
        "127.0.0.1",
        "ROBOTHOR_BRIDGE_HOST",
        "Address the bridge binds to. Anything but loopback requires real "
        "authentication to be configured.",
    )
    bridge_port: int = declare(9100, "ROBOTHOR_BRIDGE_PORT", "Bridge HTTP port.")
    default_service_role: str = declare(
        "service",
        "ROBOTHOR_DEFAULT_SERVICE_ROLE",
        "Role assigned to internal service callers. A fresh install with this "
        "unseeded denies every agent every tool.",
    )


# ---------------------------------------------------------------------------
# flags
# ---------------------------------------------------------------------------


class FlagSettings(SettingsGroup):
    """Guardrails and feature gates, most on an off/observe/enforce ladder.

    Governed flags are inventoried in ``infra/flags.yaml`` with an owner, a
    current mode and a promotion date.
    """

    accretion_enabled: bool = declare(
        False,
        "ROBOTHOR_ACCRETION_ENABLED",
        "Let agents accrete durable notes from their runs into the workspace.",
        governed=True,
    )
    admission_enabled: bool = declare(
        False,
        "ROBOTHOR_ADMISSION_ENABLED",
        "Run the admission check that refuses work an agent is not equipped "
        "to do rather than letting it fail late.",
        governed=True,
    )
    admission_mode: str = declare(
        "observe",
        "ROBOTHOR_ADMISSION_MODE",
        "Admission ladder position: observe logs refusals, enforce applies them.",
        governed=True,
    )
    curator_apply: bool = declare(
        False,
        "ROBOTHOR_CURATOR_APPLY",
        "Let the memory curator write its proposed edits. Off, it only "
        "proposes — the prompt-trust boundary for accreted content.",
        governed=True,
    )
    deliverable_contract_enabled: bool = declare(
        False,
        "ROBOTHOR_DELIVERABLE_CONTRACT_ENABLED",
        "Hold a run to the deliverable it promised, so 'done' means the artefact exists.",
        governed=True,
    )
    deliverable_contract_mode: str = declare(
        "observe",
        "ROBOTHOR_DELIVERABLE_CONTRACT_MODE",
        "Deliverable-contract ladder position: observe records breaches, enforce fails the run.",
        governed=True,
    )
    detectors_enabled: bool = declare(
        True,
        "ROBOTHOR_DETECTORS_ENABLED",
        "Run the background detectors that propose workflows and surface "
        "anomalies. Set 0 to disable them all.",
    )
    disable_all_guardrails: bool = declare(
        False,
        "ROBOTHOR_DISABLE_ALL_GUARDRAILS",
        "Master off switch for every guardrail. A break-glass control: an "
        "instance running with this set has no safety checks at all.",
        governed=True,
    )
    dnc_mode: str = declare(
        "observe",
        "ROBOTHOR_DNC_MODE",
        "Do-not-contact ladder position: observe logs an attempted contact of "
        "a suppressed person, enforce blocks it.",
        governed=True,
    )
    ha_dedup_enabled: bool = declare(
        False,
        "ROBOTHOR_HA_DEDUP_ENABLED",
        "Deduplicate work across engine replicas through Redis instead of "
        "in-process only. Off is the correct single-node default.",
    )
    ha_leader_enabled: bool = declare(
        False,
        "ROBOTHOR_HA_LEADER_ENABLED",
        "Elect a leader among engine replicas so scheduled work runs once. "
        "Unset means single-node, where every process is the leader.",
    )
    planner_enabled: bool = declare(
        True,
        "ROBOTHOR_PLANNER_ENABLED",
        "Let the forward planner turn a thread into structured CRM tasks. "
        "Set 0 to fall back to stage-3 behaviour.",
        governed=True,
    )
    plugin_manifest_enabled: bool = declare(
        True,
        "ROBOTHOR_PLUGIN_MANIFEST_ENABLED",
        "Validate plugin manifests before a plugin is allowed to load.",
        governed=True,
    )
    plugin_manifest_mode: str = declare(
        "observe",
        "ROBOTHOR_PLUGIN_MANIFEST_MODE",
        "Plugin-manifest ladder position: observe logs violations, enforce "
        "refuses to load the plugin.",
        governed=True,
    )
    sandbox_enforce_overrides_manifest: bool = declare(
        False,
        "ROBOTHOR_SANDBOX_ENFORCE_OVERRIDES_MANIFEST",
        "Make sandbox 'enforce' outrank a manifest's per-agent opt-out. "
        "Without it an agent can decline the sandbox it is enforced under.",
        governed=True,
    )
    todo_escalate_enabled: bool = declare(
        True,
        "ROBOTHOR_TODO_ESCALATE_ENABLED",
        "Escalate a run's unfinished todos into CRM tasks when it ends, so "
        "leftovers are tracked rather than lost with the transcript.",
    )
    todo_promote_subtasks_enabled: bool = declare(
        False,
        "ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED",
        "Promote a todo's subtasks into their own CRM tasks.",
        governed=True,
    )
    web_search_browser_fallback: str = declare(
        "",
        "ROBOTHOR_WEB_SEARCH_BROWSER_FALLBACK",
        "'on' lets web_search fall back to a real browser when the scraped "
        "engines block the host IP — the failure mode behind a whole day of "
        "'can't even web search'.",
        governed=True,
    )
    federation_allow_inert_rls: bool = declare(
        False,
        "ROBOTHOR_FEDERATION_ALLOW_INERT_RLS",
        "Let a federation link activate while row-level security is inert. A "
        "deliberate escape hatch: the gate exists because a child could "
        "otherwise reach its parent's data.",
        governed=True,
    )


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------


class ServiceSettings(SettingsGroup):
    """Side services the instance runs: ports, endpoints and their knobs."""

    api_port: int = declare(9099, "ROBOTHOR_API_PORT", "RAG orchestrator / API server port.")
    orchestrator_port: int = declare(
        9099, "ROBOTHOR_ORCHESTRATOR_PORT", "Orchestrator service port."
    )
    monitoring_port: int = declare(3010, "ROBOTHOR_MONITORING_PORT", "Monitoring dashboard port.")
    helm_port: int = declare(3004, "ROBOTHOR_HELM_PORT", "Helm control-centre dashboard port.")
    vision_port: int = declare(
        8600, "ROBOTHOR_VISION_PORT", "Vision service port (object and face detection)."
    )
    voice_port: int = declare(8765, "ROBOTHOR_VOICE_PORT", "Voice service port.")
    tts_port: int = declare(8880, "ROBOTHOR_TTS_PORT", "Local text-to-speech service port.")
    tts_voice: str = declare(
        "am_michael", "ROBOTHOR_TTS_VOICE", "Voice the local TTS service speaks with."
    )
    searxng_port: int = declare(8888, "ROBOTHOR_SEARXNG_PORT", "SearXNG metasearch port.")
    searxng_url: str = declare(
        "",
        "ROBOTHOR_SEARXNG_URL",
        "Full SearXNG base URL for web search. Empty derives one from the port.",
    )
    camera_source: str = declare(
        "/dev/video0", "ROBOTHOR_CAMERA_SOURCE", "Capture device or stream URL."
    )
    camera_width: int = declare(640, "ROBOTHOR_CAMERA_WIDTH", "Capture width in pixels.")
    camera_height: int = declare(480, "ROBOTHOR_CAMERA_HEIGHT", "Capture height in pixels.")
    camera_rtsp_port: int = declare(
        8554,
        "ROBOTHOR_CAMERA_RTSP_PORT",
        "RTSP port the media server publishes the camera on. 0 disables the tunnel entry for it.",
    )
    camera_hls_port: int = declare(
        8890, "ROBOTHOR_CAMERA_HLS_PORT", "HLS port for browser camera playback."
    )
    vision_mode: str = declare(
        "disarmed",
        "ROBOTHOR_VISION_MODE",
        "Whether the camera pipeline is armed. 'disarmed' is the default: the "
        "service runs but does not look.",
    )
    vision_snapshot_retention_days: int = declare(
        14,
        "ROBOTHOR_VISION_SNAPSHOT_RETENTION_DAYS",
        "How long vision snapshots are kept before deletion.",
    )
    desktop_display: str = declare(
        ":99",
        "ROBOTHOR_DESKTOP_DISPLAY",
        "X display the headless desktop and browser tools attach to.",
    )
    tunnel_provider: str = declare(
        "none",
        "ROBOTHOR_TUNNEL_PROVIDER",
        "Ingress provider for port-bearing services ('cloudflare' or 'none').",
    )
    siem_webhook_url: str = declare(
        "",
        "ROBOTHOR_SIEM_WEBHOOK_URL",
        "Audit events are POSTed here as JSON (Splunk HEC, Datadog, generic). "
        "Empty disables webhook forwarding.",
    )
    siem_syslog_host: str = declare(
        "",
        "ROBOTHOR_SIEM_SYSLOG_HOST",
        "Host audit events are sent to as RFC5424 syslog over UDP. Empty "
        "disables syslog forwarding.",
    )
    siem_syslog_port: int = declare(
        514, "ROBOTHOR_SIEM_SYSLOG_PORT", "UDP port for syslog audit forwarding."
    )


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------


class SecretSettings(SettingsGroup):
    """Secret material that belongs to no single service.

    Per-service credentials stay with their service (``database.password``,
    ``auth.signing_key``) and are marked ``secret`` there.
    """

    intent_hmac_secret: str = declare(
        "",
        "ROBOTHOR_INTENT_HMAC_SECRET",
        "Key that signs memory intents so a stored intent cannot be forged. "
        "Signing raises rather than falling back when it is unset.",
        secret=True,
    )
    env_file: str = declare(
        "/etc/robothor/robothor.env",
        "ROBOTHOR_ENV_FILE",
        "The EnvironmentFile systemd loads secrets from. It is instance data, "
        "never in git; the path is what the platform declares.",
    )


# ---------------------------------------------------------------------------
# substrate
# ---------------------------------------------------------------------------


class SubstrateSettings(SettingsGroup):
    """Where and how the instance runs: host accounts, federation, backups."""

    service_user: str = declare(
        "robothor",
        "ROBOTHOR_SERVICE_USER",
        "OS account the systemd units run as.",
    )
    service_group: str = declare(
        "",
        "ROBOTHOR_SERVICE_GROUP",
        "OS group for the service account. Empty means the same name as the service user.",
    )
    service_home: str = declare(
        "",
        "ROBOTHOR_SERVICE_HOME",
        "Home directory of the service account, as referenced by the units.",
    )
    instance_id: str = declare(
        "",
        "ROBOTHOR_INSTANCE_ID",
        "Stable id of this instance in a federation. Both sides must agree or "
        "a link mints two different connection ids and carries no messages.",
    )
    instance_name: str = declare(
        "",
        "ROBOTHOR_INSTANCE_NAME",
        "Human-readable name of this instance, shown to federated peers.",
    )
    public_endpoint: str = declare(
        "",
        "ROBOTHOR_PUBLIC_ENDPOINT",
        "Address federated peers reach this instance on.",
    )
    nats_enabled: bool = declare(
        False,
        "ROBOTHOR_NATS_ENABLED",
        "Connect to the NATS broker that carries federation traffic.",
    )
    nats_url: str = declare(
        "",
        "ROBOTHOR_NATS_URL",
        "NATS broker URL. Empty leaves federation transport unconfigured.",
    )
    nats_user: str = declare("", "ROBOTHOR_NATS_USER", "NATS account user.")
    nats_password: str = declare(
        "", "ROBOTHOR_NATS_PASSWORD", "NATS account password.", secret=True
    )
    nats_config: str = declare(
        "/etc/nats/nats-server.conf",
        "ROBOTHOR_NATS_CONFIG",
        "Path to the NATS server config federation provisioning edits. Note "
        "that subjects containing a dash must be quoted there or the account "
        "silently fails to load.",
    )
    offsite_remote: str = declare(
        "",
        "ROBOTHOR_OFFSITE_REMOTE",
        "rclone remote backups are synced to. Empty means no offsite copy.",
    )
    offsite_keep: int = declare(
        7,
        "ROBOTHOR_OFFSITE_KEEP",
        "Offsite backup generations kept. This is the recovery window: set it "
        "deliberately rather than inheriting the default.",
    )
    offsite_verify_only: bool = declare(
        False,
        "ROBOTHOR_OFFSITE_VERIFY_ONLY",
        "Only byte-compare the local dumps against the remote instead of "
        "uploading — what the verify and restore-drill units run.",
    )
    backup_group: str = declare(
        "",
        "ROBOTHOR_BACKUP_GROUP",
        "Which backup group a templated base-backup unit instance handles.",
    )
    snapshot_repository: str = declare(
        "",
        "GENUS_SNAPSHOT_REPOSITORY",
        "Default repository `genus snapshot` reads and writes when --repository is not given.",
    )
    liveness_url: str = declare(
        "http://127.0.0.1:18800/live",
        "ROBOTHOR_LIVENESS_URL",
        "URL the liveness guard polls to decide the engine is alive.",
    )
    liveness_unit: str = declare(
        "robothor-engine.service",
        "ROBOTHOR_LIVENESS_UNIT",
        "systemd unit the liveness guard restarts when the probe keeps failing.",
    )
    liveness_timeout: int = declare(
        10, "ROBOTHOR_LIVENESS_TIMEOUT", "Seconds one liveness probe may take."
    )
    liveness_failure_threshold: int = declare(
        3,
        "ROBOTHOR_LIVENESS_FAILURE_THRESHOLD",
        "Consecutive failed probes before the guard acts.",
    )
    boot_loop_limit: int = declare(
        3,
        "ROBOTHOR_BOOT_LOOP_LIMIT",
        "Restarts within the boot-loop window before the guard stops restarting and pages instead.",
    )
    boot_loop_window: int = declare(
        900, "ROBOTHOR_BOOT_LOOP_WINDOW", "Seconds the boot-loop counter spans."
    )
    thermal_warn_c: int = declare(
        90, "ROBOTHOR_THERMAL_WARN_C", "Temperature (C) the thermal guard warns at."
    )
    thermal_throttle_c: int = declare(
        85, "ROBOTHOR_THERMAL_THROTTLE_C", "Temperature (C) the guard starts throttling at."
    )
    thermal_crit_c: int = declare(
        94,
        "ROBOTHOR_THERMAL_CRIT_C",
        "Temperature (C) treated as critical, where work is shed immediately.",
    )
    thermal_restore_c: int = declare(
        75, "ROBOTHOR_THERMAL_RESTORE_C", "Temperature (C) normal operation resumes below."
    )
    thermal_normal_pct: int = declare(
        65,
        "ROBOTHOR_THERMAL_NORMAL_PCT",
        "CPU frequency cap (percent) the guard holds during normal operation.",
    )
    shed_poll: int = declare(
        2, "ROBOTHOR_SHED_POLL", "Seconds between load-shedding temperature polls."
    )
    shed_stage1_c: int = declare(
        82, "ROBOTHOR_SHED_STAGE1_C", "Temperature (C) that begins stage-1 load shedding."
    )
    shed_stage2_c: int = declare(
        86, "ROBOTHOR_SHED_STAGE2_C", "Temperature (C) that begins stage-2 load shedding."
    )
    shed_stage1_sustain: int = declare(
        6,
        "ROBOTHOR_SHED_STAGE1_SUSTAIN",
        "Consecutive polls above the stage-1 threshold before shedding starts.",
    )
    shed_recover_c: int = declare(
        78, "ROBOTHOR_SHED_RECOVER_C", "Temperature (C) shed work is restored below."
    )
    gpu_clock_cap_mhz: int = declare(
        0,
        "ROBOTHOR_GPU_CLOCK_CAP_MHZ",
        "Upper GPU clock cap applied by the thermal guard. 0 leaves it alone.",
    )
    gpu_clock_min_mhz: int = declare(
        0,
        "ROBOTHOR_GPU_CLOCK_MIN_MHZ",
        "Lower GPU clock bound the guard will not throttle below. 0 leaves it alone.",
    )
    slo_os_user: str = declare(
        "",
        "ROBOTHOR_SLO_OS_USER",
        "OS account the SLO watcher runs its database checks as, so PGUSER "
        "resolves to a real role.",
    )
    slo_heartbeat_agent: str = declare(
        "main",
        "ROBOTHOR_SLO_HEARTBEAT_AGENT",
        "Agent whose heartbeat freshness the SLO watcher treats as the instance's pulse.",
    )
    slo_journalctl_cmd: str = declare(
        "journalctl",
        "ROBOTHOR_SLO_JOURNALCTL_CMD",
        "Command the SLO watcher reads the journal with — overridable so the "
        "checks can be exercised off a real systemd host.",
    )
    slo_key_pool_cmd: str = declare(
        "",
        "ROBOTHOR_SLO_KEY_POOL_CMD",
        "Command that reports credential-pool health. Empty skips the check, "
        "which is how a capped key stayed invisible for a day.",
    )
    soak_args: str = declare(
        "",
        "ROBOTHOR_SOAK_ARGS",
        "JSON blob of arguments the federation soak passes to its child "
        "processes. Set by the harness, not by an operator.",
    )


# ---------------------------------------------------------------------------
# root
# ---------------------------------------------------------------------------


class GenusSettings(BaseSettings):
    """Every Genus OS setting, grouped.

    Build one with :func:`robothor.settings.get_settings`, which caches and
    applies the source precedence. Constructing this class directly bypasses
    the config.yaml and environment sources.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="forbid",
        env_nested_delimiter=None,
        populate_by_name=True,
    )

    paths: PathsSettings = Field(default_factory=PathsSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    channels: ChannelSettings = Field(default_factory=ChannelSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    flags: FlagSettings = Field(default_factory=FlagSettings)
    services: ServiceSettings = Field(default_factory=ServiceSettings)
    secrets: SecretSettings = Field(default_factory=SecretSettings)
    substrate: SubstrateSettings = Field(default_factory=SubstrateSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        """Highest priority first: overrides, environment, config.yaml, defaults.

        The stock ``env_settings`` is replaced because it derives variable
        names from field names; ours reads the ``env`` each field declares,
        which is the only way a name can be both exact and documented.
        """
        from robothor.settings.sources import ConfigYamlSource, DeclaredEnvSource

        return (
            init_settings,
            DeclaredEnvSource(settings_cls),
            ConfigYamlSource(settings_cls),
        )
