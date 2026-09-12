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
``flags``, ``services``, ``secrets``, ``substrate``, ``ops``). Groups are
plain
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

from typing import Any, ClassVar

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
    restart_units: tuple[str, ...] | None = None,
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
        restart_units: the systemd units that have to be restarted for a
            change to this setting to take effect. Left unset, the field
            inherits its group's :attr:`SettingsGroup.restart_units` -- the
            usual case, because a group exists precisely because one service
            reads it. Name the units on the field when a setting is read by a
            service its neighbours are not: naming too few is how a change
            reports applied and is not.
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
            "restart_units": None if restart_units is None else list(restart_units),
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

    ``restart_units`` is the group's answer to "what do I restart?", stamped
    onto every field that did not name its own. It lives here rather than in a
    table beside the CLI because a table keyed on group names silently gives
    its default to the next group somebody adds, and the default -- restart
    everything -- is the one answer that is never wrong and never useful.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: Restarted for any ``restart_required`` field in this group. Overridden
    #: per group below; the base value is the conservative answer for a group
    #: whose reader is not pinned down.
    restart_units: ClassVar[tuple[str, ...]] = ("robothor-engine", "robothor-bridge")

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Give every field of this group its units, once the fields exist.

        pydantic calls this after the model is built, so ``model_fields`` is
        populated; ``__init_subclass__`` runs too early to see them.
        """
        super().__pydantic_init_subclass__(**kwargs)
        for field in cls.model_fields.values():
            extra = field.json_schema_extra
            if isinstance(extra, dict) and extra.get("restart_units") is None:
                extra["restart_units"] = list(cls.restart_units)


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
    owner_config_path: str = declare(
        "",
        "ROBOTHOR_OWNER_CONFIG",
        "Explicit override for the operator identity file "
        "robothor.owner_config.load_owner_config() reads when called with no "
        "explicit path. Empty means the hardcoded ~/.robothor/owner.yaml. "
        "Read directly in robothor/settings/sources.py "
        "(owner_config_override_path()), not through this model -- declared "
        "here only so the name is documented and the env-read-site ratchet "
        "sees it as accounted for.",
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
    test_db_dsn: str = declare(
        "",
        "ROBOTHOR_TEST_DB_DSN",
        "Full libpq DSN CI points the suites at. Carries a password, so it is "
        "supplied by the workflow rather than committed anywhere.",
        secret=True,
    )
    test_admin_dsn: str = declare(
        "",
        "ROBOTHOR_TEST_ADMIN_DSN",
        "Full libpq DSN for the administrative role CI creates and drops test databases with.",
        secret=True,
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

    restart_units: ClassVar[tuple[str, ...]] = ("robothor-engine",)

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

    restart_units: ClassVar[tuple[str, ...]] = ("robothor-engine",)

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

    restart_units: ClassVar[tuple[str, ...]] = ("robothor-engine",)

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
        "Webhook alerts are POSTed to. Empty skips webhook delivery entirely. "
        "Held as a secret: these URLs routinely carry the bearer token in the "
        "path or the query string.",
        secret=True,
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

    restart_units: ClassVar[tuple[str, ...]] = ("robothor-engine",)

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

    restart_units: ClassVar[tuple[str, ...]] = ("robothor-bridge", "robothor-app")

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
    local_login: bool = declare(
        False,
        "GENUS_LOCAL_LOGIN",
        "Offer local email + password sign-in (bridge routes and the dashboard "
        "form). Off by default: a fresh instance must opt in. Owner MFA becomes "
        "mandatory when this is the only sign-in method.",
        since="1.69.0",
        restart_required=True,
    )
    owner_mfa_required: bool = declare(
        True,
        "GENUS_OWNER_MFA_REQUIRED",
        "Tell an owner account with no second factor to enrol one while local "
        "login is on. Setting it false gives up the only compensating control "
        "for a public password endpoint: one argon2-verified password then "
        "becomes the entire authentication for the instance and its stored "
        "credentials, and the operator is never prompted.",
        since="1.69.0",
    )
    trusted_proxies: str = declare(
        "",
        "GENUS_TRUSTED_PROXIES",
        "Comma-separated peer addresses or CIDR ranges allowed to assert the end "
        "user's address in X-Client-IP (the dashboard pod, a reverse proxy). "
        "Empty trusts nobody, loopback included; the real peer address is used.",
        since="1.69.0",
    )
    cf_access_team_domain: str = declare(
        "",
        "CF_ACCESS_TEAM_DOMAIN",
        "Cloudflare Access team domain that fronts the dashboard; with the "
        "audience, lets the bridge and dashboard treat Access as a sign-in method.",
        since="legacy",
    )
    cf_access_aud: str = declare(
        "",
        "CF_ACCESS_AUD",
        "Cloudflare Access application audience tag that the dashboard verifies "
        "sign-in assertions against.",
        since="legacy",
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

    ``governed=True`` means two things at once, and both must hold:

    * the flag is inventoried in ``infra/flags.yaml`` with an owner, a current
      production mode and a promotion date, and
    * it resolves through ``robothor.flags.store`` -- operator DB row first,
      then the environment -- so the Controls dashboard and ``genus config
      set`` can move it without an edit to ``/etc`` and a restart.

    ``robothor.flags.store.GOVERNED_FLAGS`` derives from exactly this metadata
    (see ``tests/test_flag_registry_single_source.py``), which is why a feature
    gate the platform reads straight from ``os.environ`` is NOT marked
    governed, however important it is: marking it would hand an operator a
    switch that writes a row nothing reads, and an inert control is worse than
    an absent one. Route the reader through
    ``robothor.engine.feature_flags`` first, then mark it.
    """

    restart_units: ClassVar[tuple[str, ...]] = ("robothor-engine",)

    accretion_enabled: bool = declare(
        False,
        "ROBOTHOR_ACCRETION_ENABLED",
        "Let agents accrete durable notes from their runs into the workspace.",
    )
    admission_enabled: bool = declare(
        False,
        "ROBOTHOR_ADMISSION_ENABLED",
        "Run the admission check that refuses work an agent is not equipped "
        "to do rather than letting it fail late.",
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
    )
    deliverable_contract_enabled: bool = declare(
        False,
        "ROBOTHOR_DELIVERABLE_CONTRACT_ENABLED",
        "Hold a run to the deliverable it promised, so 'done' means the artefact exists.",
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
    manifest_schema_mode: str = declare(
        "observe",
        "ROBOTHOR_MANIFEST_SCHEMA_MODE",
        "Agent-manifest schema ladder position: observe logs and counts what "
        "enforcement would refuse, enforce refuses the manifest and reports "
        "the agent broken. off skips validation entirely.",
    )
    planner_enabled: bool = declare(
        True,
        "ROBOTHOR_PLANNER_ENABLED",
        "Let the forward planner turn a thread into structured CRM tasks. "
        "Set 0 to fall back to stage-3 behaviour.",
    )
    plugin_manifest_enabled: bool = declare(
        True,
        "ROBOTHOR_PLUGIN_MANIFEST_ENABLED",
        "Validate plugin manifests before a plugin is allowed to load.",
    )
    plugin_manifest_mode: str = declare(
        "observe",
        "ROBOTHOR_PLUGIN_MANIFEST_MODE",
        "Plugin-manifest ladder position: observe logs violations, enforce "
        "refuses to load the plugin.",
    )
    sandbox_enforce_overrides_manifest: bool = declare(
        False,
        "ROBOTHOR_SANDBOX_ENFORCE_OVERRIDES_MANIFEST",
        "Make sandbox 'enforce' outrank a manifest's per-agent opt-out. "
        "Without it an agent can decline the sandbox it is enforced under.",
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
    )
    web_search_browser_fallback: str = declare(
        "",
        "ROBOTHOR_WEB_SEARCH_BROWSER_FALLBACK",
        "'on' lets web_search fall back to a real browser when the scraped "
        "engines block the host IP — the failure mode behind a whole day of "
        "'can't even web search'.",
    )
    federation_allow_inert_rls: bool = declare(
        False,
        "ROBOTHOR_FEDERATION_ALLOW_INERT_RLS",
        "Let a federation link activate while row-level security is inert. A "
        "deliberate escape hatch: the gate exists because a child could "
        "otherwise reach its parent's data.",
    )
    config_strict_mode: str = declare(
        "observe",
        "ROBOTHOR_CONFIG_STRICT_MODE",
        "What an unknown key in the settings: block of config.yaml does. "
        "'off' ignores it, 'observe' (default for existing installs) logs one "
        "warning naming the key and carries on, 'enforce' refuses to start. "
        "Inventoried in infra/flags.yaml but deliberately NOT governed: it is "
        "read while settings are being resolved, which happens before and "
        "without a database.",
        since="1.66",
    )

    # --- Guardrail ladders ------------------------------------------------
    #
    # Each is an *_ENABLED switch plus a *_MODE rung (see
    # robothor.engine.feature_flags._enforcement_mode): the subsystem does
    # nothing at all until the switch is on, and then the rung decides whether
    # it observes, alerts or acts. Both halves were read by the engine for a
    # year and declared nowhere -- they reach os.environ through a variable
    # name, so the AST scan behind test_every_env_read_is_declared could not
    # see them, and an operator asking "what can I configure?" was told they
    # did not exist. The *_MODE halves are the governed ones: they are what
    # the flag manifest tracks and what an operator promotes.

    rbac_enabled: bool = declare(
        False,
        "ROBOTHOR_RBAC_ENABLED",
        "Switch for role-based access control over system, cron and hook runs. "
        "Off, every run may call every tool its manifest allows.",
    )
    rbac_mode: str = declare(
        "observe",
        "ROBOTHOR_RBAC_MODE",
        "RBAC ladder position: observe records the denials it would have made, "
        "alert also notifies, enforce denies.",
        governed=True,
    )
    injection_scan_enabled: bool = declare(
        False,
        "ROBOTHOR_INJECTION_SCAN_ENABLED",
        "Switch for prompt-injection scanning of assembled system-run prompts.",
    )
    injection_scan_mode: str = declare(
        "observe",
        "ROBOTHOR_INJECTION_SCAN_MODE",
        "Injection-scan ladder position: observe logs a suspect prompt, enforce refuses to run it.",
        governed=True,
    )
    exec_allowlist_strict_enabled: bool = declare(
        False,
        "ROBOTHOR_EXEC_ALLOWLIST_STRICT_ENABLED",
        "Switch for rejecting shell-chaining metacharacters in an allowlisted "
        "exec command, so an allowlisted binary cannot carry a second one.",
    )
    exec_allowlist_strict_mode: str = declare(
        "observe",
        "ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE",
        "Exec-allowlist ladder position: observe logs the chained command, enforce refuses it.",
        governed=True,
    )
    approval_failclosed_enabled: bool = declare(
        False,
        "ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED",
        "Switch for fail-closed human approval: a tool an agent must ask about "
        "is denied when the operator does not answer in time.",
    )
    approval_mode: str = declare(
        "observe",
        "ROBOTHOR_APPROVAL_MODE",
        "Human-approval ladder position: observe records what would have been "
        "escalated, enforce actually asks and denies on timeout.",
        governed=True,
    )
    completion_contracts_enabled: bool = declare(
        False,
        "ROBOTHOR_COMPLETION_CONTRACTS_ENABLED",
        "Switch for evidence-based completion contracts: a run claiming "
        "success must show the tool trace that produced it.",
    )
    completion_contracts_mode: str = declare(
        "observe",
        "ROBOTHOR_COMPLETION_CONTRACTS_MODE",
        "Completion-contract ladder position: observe records unevidenced "
        "claims, enforce fails the run that makes one.",
        governed=True,
    )
    run_verification_enabled: bool = declare(
        False,
        "ROBOTHOR_RUN_VERIFICATION_ENABLED",
        "Switch for verifying a finished run's claims against its tool trace.",
    )
    run_verification_mode: str = declare(
        "observe",
        "ROBOTHOR_RUN_VERIFICATION_MODE",
        "Run-verification ladder position: observe records the verdict, "
        "enforce marks the run failed when its claims are unsupported.",
        governed=True,
    )
    tool_verify_enabled: bool = declare(
        False,
        "ROBOTHOR_TOOL_VERIFY_ENABLED",
        "Switch for tool-level post-condition checks -- did the write the tool "
        "reported actually land?",
    )
    tool_verify_mode: str = declare(
        "observe",
        "ROBOTHOR_TOOL_VERIFY_MODE",
        "Tool-verification ladder position: observe records a failed "
        "post-condition, enforce reports the tool call as failed.",
        governed=True,
    )
    benchmark_decontamination_enabled: bool = declare(
        False,
        "ROBOTHOR_BENCHMARK_DECONTAMINATION_ENABLED",
        "Switch for separating benchmark-harness traffic from production "
        "metrics, so a nightly suite does not read as fleet activity.",
    )
    benchmark_decontamination_mode: str = declare(
        "observe",
        "ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE",
        "Decontamination ladder position: observe reports benchmark runs and "
        "cost separately, enforce excludes them from production surfaces.",
        governed=True,
    )
    benchmark_sandbox_enabled: bool = declare(
        False,
        "ROBOTHOR_BENCHMARK_SANDBOX_ENABLED",
        "Switch for seeded benchmark fixtures and sandboxed CRM writes, so a "
        "graded task can act instead of only reading.",
    )
    benchmark_sandbox_mode: str = declare(
        "observe",
        "ROBOTHOR_BENCHMARK_SANDBOX_MODE",
        "Benchmark-sandbox ladder position: observe seeds fixtures and records "
        "read-backs without grading them, enforce folds them into the score.",
        governed=True,
    )
    honesty_suite_mode: str = declare(
        "observe",
        "ROBOTHOR_HONESTY_SUITE_MODE",
        "Honesty cases in every benchmark suite: 'off' omits them, 'observe' "
        "(default) runs and reports them outside the weighted aggregate, "
        "'enforce' counts them toward the grade. A grader, not a guardrail, so "
        "it has no 'alert' rung and no separate enabled switch.",
        governed=True,
    )
    judge_enabled: bool = declare(
        False,
        "ROBOTHOR_JUDGE_ENABLED",
        "Let the goal-judge grade recent runs against real outcome signals and "
        "write the agent_reviews rows the achievement score is built on.",
        governed=True,
    )
    disable_all_rips: bool = declare(
        False,
        "ROBOTHOR_DISABLE_ALL_RIPS",
        "Panic switch: forces every rip and every ladder above to 'off' "
        "regardless of its own flag. An instance left with this set runs with "
        "none of the controls it appears to have.",
    )
    rip_1_enabled: bool = declare(
        False,
        "ROBOTHOR_RIP_1_ENABLED",
        "Rip 1: per-agent memory scoping, so an agent reads the blocks it owns "
        "rather than the whole workspace.",
        governed=True,
    )
    rip_4_enabled: bool = declare(
        False,
        "ROBOTHOR_RIP_4_ENABLED",
        "Rip 4: structured tool-result envelopes the runner can check instead "
        "of prose it can only quote.",
        governed=True,
    )
    rip_5_enabled: bool = declare(
        False,
        "ROBOTHOR_RIP_5_ENABLED",
        "Rip 5: the destructive LLM skill-consolidation curator pass. The "
        "non-destructive lifecycle runs either way.",
        governed=True,
    )
    rip_7_enabled: bool = declare(
        False,
        "ROBOTHOR_RIP_7_ENABLED",
        "Rip 7: the drift detector over memory_facts.",
    )
    rip_7_mode: str = declare(
        "observe",
        "ROBOTHOR_RIP_7_MODE",
        "Rip 7 ladder position: observe logs a drifting write, alert also "
        "notifies, enforce snapshots and refuses it.",
        governed=True,
    )
    rip_13_enabled: bool = declare(
        False,
        "ROBOTHOR_RIP_13_ENABLED",
        "Rip 13: symbolic compaction of tool logs into a symbol graph.",
    )
    rip_13_mode: str = declare(
        "observe",
        "ROBOTHOR_RIP_13_MODE",
        "Rip 13 ladder position: observe logs the tokens compaction would have "
        "saved, enforce injects the compact graph instead of raw tool output. "
        "Two rungs only -- there is nothing to alert about.",
        governed=True,
    )


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------


class ServiceSettings(SettingsGroup):
    """Side services the instance runs: ports, endpoints and their knobs."""

    restart_units: ClassVar[tuple[str, ...]] = ("robothor-bridge", "robothor-app")

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
        "Empty disables webhook forwarding. Held as a secret: an HEC or "
        "Datadog collector URL carries its ingest token.",
        secret=True,
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
        "NATS broker URL. Empty leaves federation transport unconfigured. "
        "Held as a secret: the nats:// form accepts inline user:pass@ "
        "credentials, and instances do set it that way.",
        secret=True,
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
    os_user: str = declare(
        "robothor",
        "ROBOTHOR_USER",
        "Account infra/setup.sh chowns the workspace and log directory to "
        "during provisioning. The same account the units later run as.",
    )
    dev_mode: bool = declare(
        False,
        "ROBOTHOR_DEV",
        "Set inside the development container image. Marks a build that "
        "carries dev tooling and must not be what production runs.",
    )
    deployed_at: str = declare(
        "",
        "GENUS_OS_DEPLOYED_AT",
        "Timestamp the Helm chart stamps onto every pod, so a running "
        "container can say when it was deployed rather than when it booted.",
    )
    deployed_from_pr: str = declare(
        "",
        "GENUS_OS_DEPLOYED_FROM_PR",
        "PR number a staging deployment came from, stamped by the Helm "
        "chart. Empty on a production release, which comes from a tag.",
    )
    image_tag: str = declare(
        "",
        "GENUS_OS_IMAGE_TAG",
        "Container image tag the Helm chart stamped onto the pod. Production "
        "pins an exact vX.Y.Z; staging pins pr-N-sha-<short>.",
    )
    deploy_url: str = declare(
        "",
        "GENUS_URL",
        "Base URL the release workflow smoke-tests after a deploy, calling "
        "/api/live and /api/ready on it.",
    )
    production_url: str = declare(
        "",
        "GENUS_PRODUCTION_URL",
        "Repository variable supplying the production base URL the release "
        "workflow smoke-tests. Empty falls back to the workflow's default.",
    )
    allow_deployment_lag: bool = declare(
        False,
        "GENUS_ALLOW_DEPLOYMENT_LAG",
        "Let the version-consistency check pass while the deployed version "
        "trails the released one. An escape hatch for a deliberate hold, not "
        "a way to stop noticing that a promotion was lost.",
    )


# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------


class OpsSettings(SettingsGroup):
    """Backups, restores, SLO probes, alert delivery and the volume guard.

    These are read by shell, not by Python. That is why they were the last
    thing anyone declared, and it is also why they matter: the controls that
    decide whether a backup exists, whether a restore works and whether anyone
    is paged all live in here. An operator configuring one has exactly the same
    problem as an operator configuring the engine.

    Most of the ``*_CMD`` entries are seams the test suites substitute so a
    guardrail can be exercised off a real systemd host; they are documented
    because an operator on an unusual host may legitimately need them.

    Two programs watch the SLOs and their variables look alike. The
    ``ROBOTHOR_SLO_*`` names here belong to ``scripts/slo_probe.sh``; the ones
    in ``substrate`` (``ROBOTHOR_SLO_OS_USER``, ``..._HEARTBEAT_AGENT``,
    ``..._JOURNALCTL_CMD``, ``..._KEY_POOL_CMD``) belong to the Python
    ``scripts/guardrail_watch.py``. Each variable is declared exactly once, in
    the group matching the reader that actually consumes it.
    """

    #: Nothing holds these: a timer or a shell script reads them when it runs,
    #: so the next invocation picks a change up without a restart.
    restart_units: ClassVar[tuple[str, ...]] = ()

    # --- PATH and interpreter plumbing ------------------------------------
    extra_path: str = declare(
        "",
        "ROBOTHOR_EXTRA_PATH",
        "Directory prepended to PATH before the guardrail scripts pin their "
        "own. A test seam: the suites point it at stub binaries.",
    )
    fixed_path: str = declare(
        "",
        "ROBOTHOR_FIXED_PATH",
        "PATH the cron wrapper captured at start and restores for the job it "
        "runs, so a cron entry does not inherit cron's near-empty PATH.",
    )
    python: str = declare(
        "",
        "ROBOTHOR_PYTHON",
        "Interpreter the restore drill runs its built-in notifier with. Empty "
        "means the repository's own venv.",
    )
    systemctl: str = declare(
        "",
        "ROBOTHOR_SYSTEMCTL",
        "systemctl the instance doctor interrogates for unit enabled/active "
        "state. Empty means the doctor skips the systemd checks.",
    )
    secrets_file: str = declare(
        "/run/robothor/secrets.env",
        "ROBOTHOR_SECRETS_FILE",
        "Decrypted secrets file the cron wrapper and the alert sender source. "
        "It lives on tmpfs; a process that starts before it exists comes up "
        "with no credentials at all and fails closed.",
    )
    instance_env: str = declare(
        "/etc/robothor/robothor.env",
        "ROBOTHOR_INSTANCE_ENV",
        "EnvironmentFile the cron wrapper sources so a cron job sees the same "
        "configuration the systemd units do.",
    )
    restart_request_dir: str = declare(
        "/run/robothor/restart-requests",
        "ROBOTHOR_RESTART_REQUEST_DIR",
        "Directory the restart handler watches for per-unit restart requests.",
    )
    restart_legacy_request: str = declare(
        "/run/robothor/restart-request",
        "ROBOTHOR_RESTART_LEGACY_REQUEST",
        "Single-file restart request the handler still honours, from before "
        "requests became one file per unit.",
    )

    # --- alert delivery ---------------------------------------------------
    alert_state_dir: str = declare(
        "/run/robothor/alert-cooldown",
        "ROBOTHOR_ALERT_STATE_DIR",
        "Directory the alert sender keeps per-unit cooldown markers in, so a "
        "flapping unit pages once rather than once per failure.",
    )
    alert_fallback_state_dir: str = declare(
        "",
        "ROBOTHOR_ALERT_FALLBACK_STATE_DIR",
        "Cooldown directory used when the primary one is not writable -- a "
        "read-only /run must not silently disable deduplication.",
    )
    alert_max_attempts: int = declare(
        10,
        "ROBOTHOR_ALERT_MAX_ATTEMPTS",
        "Delivery attempts the alert sender makes before giving up.",
    )
    alert_retry_delay: int = declare(
        30,
        "ROBOTHOR_ALERT_RETRY_DELAY",
        "Seconds between alert delivery attempts.",
    )
    cron_alert_max_attempts: int = declare(
        2,
        "ROBOTHOR_CRON_ALERT_MAX_ATTEMPTS",
        "Delivery attempts for an alert raised by the cron wrapper. Lower "
        "than the default: a cron job must not sit retrying a page.",
    )
    cron_alert_retry_delay: int = declare(
        15,
        "ROBOTHOR_CRON_ALERT_RETRY_DELAY",
        "Seconds between cron-wrapper alert delivery attempts.",
    )
    alert_suppress: str = declare(
        "",
        "ROBOTHOR_ALERT_SUPPRESS",
        "Any non-empty value makes the alert sender drop the alert and say so "
        "on stderr. For a planned maintenance window only -- an instance left "
        "with this set pages for nothing.",
    )
    alert_journal_cmd: str = declare(
        "journalctl",
        "ROBOTHOR_ALERT_JOURNAL_CMD",
        "Command the alert sender reads a failing unit's recent log from, to "
        "put context in the page.",
    )
    alert_journal_tail_bytes: int = declare(
        500,
        "ROBOTHOR_ALERT_JOURNAL_TAIL_BYTES",
        "Bytes of journal tail included in an alert body.",
    )
    telegram_api_base: str = declare(
        "https://api.telegram.org",
        "ROBOTHOR_TELEGRAM_API_BASE",
        "Telegram API base the shell alert sender posts to. Overridable so "
        "the delivery path can be tested without sending a real message.",
    )

    # --- local backups ----------------------------------------------------
    backup_mount: str = declare(
        "/mnt/robothor-backup",
        "ROBOTHOR_BACKUP_MOUNT",
        "Mount point of the backup volume. Every backup job refuses to run "
        "when this is not a real, separate mount.",
    )
    backup_log: str = declare(
        "",
        "ROBOTHOR_BACKUP_LOG",
        "Log file the SSD backup writes to. Empty picks a default under the log directory.",
    )
    basebackup_dir: str = declare(
        "/mnt/robothor-backup/robothor/basebackup",
        "ROBOTHOR_BASEBACKUP_DIR",
        "Directory pg_basebackup writes to and the WAL offsite job reads.",
    )
    basebackup_keep: int = declare(
        3,
        "ROBOTHOR_BASEBACKUP_KEEP",
        "Base backup generations kept on the local volume.",
    )
    wal_keep_days: int = declare(
        8,
        "ROBOTHOR_WAL_KEEP_DAYS",
        "Days of archived WAL kept before pruning. Must outlast the oldest "
        "base backup or that backup cannot be replayed forward.",
    )
    wal_min_free_mb: int = declare(
        5120,
        "ROBOTHOR_WAL_MIN_FREE_MB",
        "Free megabytes the WAL archiver requires before accepting a segment. "
        "Below it the archive command fails, which is what stops PostgreSQL "
        "filling the disk.",
    )
    volume_check: str = declare(
        "",
        "ROBOTHOR_VOLUME_CHECK",
        "Volume probe the backup jobs run before writing. Empty means the "
        "backup-volume-check.sh next to them.",
    )
    volume_probe_timeout: int = declare(
        20,
        "ROBOTHOR_VOLUME_PROBE_TIMEOUT",
        "Seconds one volume-probe step may take. A wedged mount answers "
        "nothing rather than answering 'no', so every step is bounded.",
    )
    volume_require_separate_mount: bool = declare(
        True,
        "ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT",
        "Require the backup path to be its own mount. Off, a dropped drive "
        "means backups quietly land on the root filesystem instead.",
    )

    # --- backup volume guard ----------------------------------------------
    crypttab: str = declare(
        "/etc/crypttab",
        "ROBOTHOR_CRYPTTAB",
        "crypttab the volume guard resolves the backup container's UUID from.",
    )
    volume_guard_mapper: str = declare(
        "robothor-backup",
        "ROBOTHOR_VOLUME_GUARD_MAPPER",
        "crypttab name of the encrypted backup container the guard reopens.",
    )
    volume_guard_mapper_dir: str = declare(
        "/dev/mapper",
        "ROBOTHOR_VOLUME_GUARD_MAPPER_DIR",
        "Device-mapper directory the guard looks for the container in.",
    )
    volume_guard_dev_dir: str = declare(
        "/dev/disk/by-uuid",
        "ROBOTHOR_VOLUME_GUARD_DEV_DIR",
        "by-uuid directory the guard resolves the backing device through, so "
        "a drive that came back on a different USB path is still found.",
    )
    volume_guard_state_dir: str = declare(
        "/run/robothor/volume-guard",
        "ROBOTHOR_VOLUME_GUARD_STATE_DIR",
        "Directory the volume guard records what it has already paged about.",
    )
    volume_guard_heal: bool = declare(
        True,
        "ROBOTHOR_VOLUME_GUARD_HEAL",
        "Let the guard reopen and remount a dropped backup volume. Off, it "
        "pages and leaves the volume down.",
    )
    volume_guard_repage_seconds: int = declare(
        86400,
        "ROBOTHOR_VOLUME_GUARD_REPAGE_SECONDS",
        "Quiet period before the guard pages again about a volume that is still down.",
    )
    volume_guard_check_cmd: str = declare(
        "",
        "ROBOTHOR_VOLUME_GUARD_CHECK_CMD",
        "Volume probe the guard runs. Empty means backup-volume-check.sh.",
    )
    volume_guard_alert_cmd: str = declare(
        "",
        "ROBOTHOR_VOLUME_GUARD_ALERT_CMD",
        "Pager the guard invokes. Empty means send_failure_alert.sh.",
    )

    # --- offsite ----------------------------------------------------------
    offsite_source: str = declare(
        "/mnt/robothor-backup/robothor/db",
        "ROBOTHOR_OFFSITE_SOURCE",
        "Local dump directory the offsite sync uploads from.",
    )
    offsite_volumes: str = declare(
        "/mnt/robothor-backup/robothor/docker-volumes",
        "ROBOTHOR_OFFSITE_VOLUMES",
        "Local docker-volume dump directory the offsite sync uploads.",
    )
    offsite_dropin_dir: str = declare(
        "",
        "ROBOTHOR_OFFSITE_DROPIN_DIR",
        "systemd drop-in directory the offsite job preserves alongside the "
        "dumps, so a restore brings back the unit configuration too.",
    )
    offsite_log: str = declare(
        "",
        "ROBOTHOR_OFFSITE_LOG",
        "Log file the offsite sync writes to. Empty picks a default under the log directory.",
    )

    # --- restore drill ----------------------------------------------------
    restore_drill_db: str = declare(
        "robothor_restore_drill",
        "ROBOTHOR_RESTORE_DRILL_DB",
        "Scratch database the restore drill restores into. Never the live "
        "one: the drill drops it afterwards.",
    )
    restore_drill_local_dir: str = declare(
        "/mnt/robothor-backup/robothor/db",
        "ROBOTHOR_RESTORE_DRILL_LOCAL_DIR",
        "Local dump directory the drill restores from when no offsite remote is configured.",
    )
    restore_drill_work_dir: str = declare(
        "",
        "ROBOTHOR_RESTORE_DRILL_WORK_DIR",
        "Directory an offsite dump is fetched into for the drill. Empty uses "
        "a temporary directory.",
    )
    restore_drill_drop_timeout: int = declare(
        300,
        "ROBOTHOR_RESTORE_DRILL_DROP_TIMEOUT",
        "Seconds a dropdb of the scratch database may block before the drill "
        "gives up, so a stuck connection cannot hang the drill forever.",
    )
    restore_drill_psql: str = declare(
        "psql",
        "ROBOTHOR_RESTORE_DRILL_PSQL",
        "psql the drill restores with.",
    )
    restore_drill_createdb: str = declare(
        "createdb",
        "ROBOTHOR_RESTORE_DRILL_CREATEDB",
        "createdb the drill makes the scratch database with.",
    )
    restore_drill_dropdb: str = declare(
        "dropdb",
        "ROBOTHOR_RESTORE_DRILL_DROPDB",
        "dropdb the drill cleans the scratch database up with.",
    )
    restore_drill_rclone_cmd: str = declare(
        "rclone",
        "ROBOTHOR_RESTORE_DRILL_RCLONE_CMD",
        "rclone the drill fetches an offsite dump with.",
    )
    restore_drill_notify_cmd: str = declare(
        "",
        "ROBOTHOR_RESTORE_DRILL_NOTIFY_CMD",
        "Command that reports the drill's result. Empty uses the built-in notifier.",
    )

    # --- liveness probe (shell) -------------------------------------------
    liveness_probe_cmd: str = declare(
        "",
        "ROBOTHOR_LIVENESS_PROBE_CMD",
        "Command that decides whether the engine is alive. Empty uses the built-in curl probe.",
    )
    liveness_alert_cmd: str = declare(
        "",
        "ROBOTHOR_LIVENESS_ALERT_CMD",
        "Pager the liveness probe invokes. Empty means send_failure_alert.sh.",
    )
    liveness_stuck_age_seconds: int = declare(
        1800,
        "ROBOTHOR_LIVENESS_STUCK_AGE_SECONDS",
        "How long a .stuck marker may stand before the probe treats it as a "
        "failure in its own right, so a wedged restart cannot look healthy.",
    )

    # --- boot guard -------------------------------------------------------
    boot_history: str = declare(
        "/var/lib/robothor/boot-history",
        "ROBOTHOR_BOOT_HISTORY",
        "File the boot guard records recent boots in to detect a boot loop.",
    )
    inhibit_flag: str = declare(
        "/run/robothor/INHIBIT_INFERENCE",
        "ROBOTHOR_INHIBIT_FLAG",
        "Marker whose presence stops the box taking on inference work -- what "
        "the boot guard and the thermal shedder drop to halt the fleet.",
    )

    # --- thermal ----------------------------------------------------------
    thermal_throttle_pct: int = declare(
        50,
        "ROBOTHOR_THERMAL_THROTTLE_PCT",
        "CPU frequency cap (percent) the thermal guard applies once the "
        "throttle threshold is crossed.",
    )

    # --- SLO probe --------------------------------------------------------
    slo_local_dump_dir: str = declare(
        "/mnt/robothor-backup/robothor/db",
        "ROBOTHOR_SLO_LOCAL_DUMP_DIR",
        "Nightly dump directory the SLO probe checks the freshness of.",
    )
    slo_local_dump_max_hours: int = declare(
        26,
        "ROBOTHOR_SLO_LOCAL_DUMP_MAX_HOURS",
        "Age budget for the nightly local dump before the SLO breaches.",
    )
    slo_offsite_max_hours: int = declare(
        26,
        "ROBOTHOR_SLO_OFFSITE_MAX_HOURS",
        "Age budget for the offsite copy before the SLO breaches.",
    )
    slo_basebackup_dir: str = declare(
        "",
        "ROBOTHOR_SLO_BASEBACKUP_DIR",
        "Base backup directory the SLO probe checks. Empty falls back to ROBOTHOR_BASEBACKUP_DIR.",
    )
    slo_basebackup_max_hours: int = declare(
        192,
        "ROBOTHOR_SLO_BASEBACKUP_MAX_HOURS",
        "Age budget for the newest base backup (192h = 8 days).",
    )
    slo_liveness_max_hours: int = declare(
        1,
        "ROBOTHOR_SLO_LIVENESS_MAX_HOURS",
        "How stale the liveness probe's last run may be before it counts as not running at all.",
    )
    slo_guardrail_watch_max_hours: int = declare(
        26,
        "ROBOTHOR_SLO_GUARDRAIL_WATCH_MAX_HOURS",
        "How stale the guardrail watcher's last run may be before breaching.",
    )
    slo_backup_cooldown_seconds: int = declare(
        43200,
        "ROBOTHOR_SLO_BACKUP_COOLDOWN_SECONDS",
        "Quiet period between repeat pages about the backup SLO. The probe "
        "runs hourly, so without a cooldown one breach pages 24 times a day.",
    )
    slo_heartbeat_cooldown_seconds: int = declare(
        43200,
        "ROBOTHOR_SLO_HEARTBEAT_COOLDOWN_SECONDS",
        "Quiet period between repeat pages about the heartbeat SLO.",
    )
    slo_llm_cooldown_seconds: int = declare(
        21600,
        "ROBOTHOR_SLO_LLM_COOLDOWN_SECONDS",
        "Quiet period between repeat pages about LLM availability.",
    )
    slo_guardrail_cooldown_seconds: int = declare(
        43200,
        "ROBOTHOR_SLO_GUARDRAIL_COOLDOWN_SECONDS",
        "Quiet period between repeat pages about the guardrail-watch SLO.",
    )
    slo_liveness_cooldown_seconds: int = declare(
        43200,
        "ROBOTHOR_SLO_LIVENESS_COOLDOWN_SECONDS",
        "Quiet period between repeat pages about the liveness SLO.",
    )
    slo_db: str = declare(
        "",
        "ROBOTHOR_SLO_DB",
        "Database the DB-backed SLOs query. Empty falls back to PGDATABASE, then ROBOTHOR_DB_NAME.",
    )
    slo_db_checks: bool = declare(
        True,
        "ROBOTHOR_SLO_DB_CHECKS",
        "Run the DB-backed SLOs (heartbeat delivery and LLM availability). "
        "Off leaves both UNMEASURED and is for tests only -- the probe says "
        "so loudly on every run.",
    )
    slo_probe_timeout: int = declare(
        20,
        "ROBOTHOR_SLO_PROBE_TIMEOUT",
        "Seconds one disk step of the probe may take. A dropped mount hangs "
        "rather than erroring, so every step is bounded.",
    )
    slo_uptime_file: str = declare(
        "/proc/uptime",
        "ROBOTHOR_SLO_UPTIME_FILE",
        "Where the probe reads host uptime from, so it does not breach an SLO "
        "for a window the box spent powered off.",
    )
    slo_alert_cmd: str = declare(
        "",
        "ROBOTHOR_SLO_ALERT_CMD",
        "Pager the SLO probe invokes. Empty means send_failure_alert.sh.",
    )
    slo_psql_cmd: str = declare(
        "",
        "ROBOTHOR_SLO_PSQL_CMD",
        "psql the DB-backed SLOs run through. Empty uses the database hop.",
    )
    slo_rclone_cmd: str = declare(
        "rclone",
        "ROBOTHOR_SLO_RCLONE_CMD",
        "rclone the probe lists the offsite copy with.",
    )
    slo_systemctl_cmd: str = declare(
        "systemctl",
        "ROBOTHOR_SLO_SYSTEMCTL_CMD",
        "systemctl the probe reads unit timestamps from.",
    )
    slo_runuser_cmd: str = declare(
        "runuser",
        "ROBOTHOR_SLO_RUNUSER_CMD",
        "Command the probe hops to the database account with.",
    )
    slo_getent_cmd: str = declare(
        "getent",
        "ROBOTHOR_SLO_GETENT_CMD",
        "Command the probe proves the database account exists with, so a "
        "missing account is reported rather than read as a passing check.",
    )
    slo_id_cmd: str = declare(
        "id",
        "ROBOTHOR_SLO_ID_CMD",
        "Command the probe checks its own identity with, deciding whether it "
        "needs the runuser hop at all.",
    )
    slo_volume_check_cmd: str = declare(
        "",
        "ROBOTHOR_SLO_VOLUME_CHECK_CMD",
        "Volume probe the SLO check runs; the dump directory is appended to "
        "it. Empty means backup-volume-check.sh --ro.",
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
    ops: OpsSettings = Field(default_factory=OpsSettings)

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
