"""Feature flags for the Tier 1-4 upgrade rollout.

Each rip in the upgrade plan is gated by an environment variable
``ROBOTHOR_RIP_<N>_ENABLED``. The operator can toggle a rip without
shipping a new release::

    systemctl set-environment ROBOTHOR_RIP_1_ENABLED=1
    systemctl restart robothor-engine

The global panic switch ``ROBOTHOR_DISABLE_ALL_RIPS=1`` forces every
flag off regardless of per-rip env vars. Use when something is wrong
and you need every new behavior dark immediately.

The trajectory rate ``ROBOTHOR_TRAJECTORY_SAMPLE`` is a float (0.0-1.0)
rather than a bool: it controls the fraction of runs whose transcripts
are written to disk.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

logger = logging.getLogger(__name__)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

Rip7Mode = Literal["off", "observe", "alert", "enforce"]
_VALID_RIP_7_MODES = frozenset(("observe", "alert", "enforce"))

SymbolicMode = Literal["off", "observe", "enforce"]
_VALID_SYMBOLIC_MODES = frozenset(("observe", "enforce"))

PerUserSessionsMode = Literal["off", "observe", "enforce"]
_VALID_PER_USER_SESSIONS_MODES = frozenset(("off", "observe", "enforce"))
# Generic observe→alert→enforce rollout ladder, shared by the Wave-1
# hardening flags below. Same shape as ``rip_7_enforcement_mode``.
#
# "alert" = observe + notify the operator. Consumers call
# ``notify_guardrail_alert`` when a check would have blocked under enforce;
# the notification reaches the operator through main's heartbeat. A rung that
# silently behaved like "observe" would be worse than no rung at all — an
# operator following the ladder would believe they had escalated when they
# had not. ``test_alert_mode_contract`` pins this.
EnforcementMode = Literal["off", "observe", "alert", "enforce"]
_VALID_ENFORCEMENT_MODES = frozenset(("observe", "alert", "enforce"))

# The honesty suite is a GRADER, not a guardrail: it blocks nothing, so it has
# no "alert" rung, and its default is `observe` rather than `off` — a case
# nobody runs measures nothing. See honesty_suite_mode().
HonestySuiteMode = Literal["off", "observe", "enforce"]
_VALID_HONESTY_SUITE_MODES = frozenset(("off", "observe", "enforce"))

# The do-not-contact opt-out has TWO rungs, not four. There is no "off": a
# legal opt-out that can be switched off entirely is not a control. There is no
# "alert" either — "observe" already notifies through the guardrail-event row
# the check writes on every would-be block. See do_not_contact_mode().
DoNotContactMode = Literal["observe", "enforce"]
_VALID_DO_NOT_CONTACT_MODES = frozenset(("observe", "enforce"))


def _resolve_raw(name: str, default: str = "") -> str:
    """Governed flags resolve through the DB store first, then env.

    Non-governed names (credentials, tuning) go straight to env — the store only
    knows the governed guardrail flags (``GOVERNED_FLAGS``).
    """
    from robothor.flags.store import GOVERNED_FLAGS, resolve

    if name in GOVERNED_FLAGS:
        val = resolve(name)
        if val is not None:
            return val
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _resolve_raw(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUE_VALUES


def _disabled_all() -> bool:
    return _env_bool("ROBOTHOR_DISABLE_ALL_RIPS")


def is_rip_enabled(rip_number: int) -> bool:
    """Return True iff rip N's behavior should be active.

    Reads ``ROBOTHOR_RIP_<N>_ENABLED``; default off. Returns False
    unconditionally when ``ROBOTHOR_DISABLE_ALL_RIPS=1``.
    """
    if _disabled_all():
        return False
    return _env_bool(f"ROBOTHOR_RIP_{rip_number}_ENABLED")


def trajectory_sample_rate() -> float:
    """Fraction of runs whose transcripts should be persisted to disk.

    Reads ``ROBOTHOR_TRAJECTORY_SAMPLE``; clamped to [0.0, 1.0]; default 0.0.
    Returns 0.0 unconditionally when ``ROBOTHOR_DISABLE_ALL_RIPS=1``.
    """
    if _disabled_all():
        return 0.0
    raw = os.environ.get("ROBOTHOR_TRAJECTORY_SAMPLE", "").strip()
    if not raw:
        return 0.0
    try:
        rate = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, min(1.0, rate))


def rip_7_enforcement_mode() -> Rip7Mode:
    """Return the drift-detector enforcement mode for memory_facts (Rip 7).

    Returns ``"off"`` when Rip 7 is disabled (or the global panic flag
    is set). Otherwise reads ``ROBOTHOR_RIP_7_MODE`` and returns one of
    ``"observe"`` (default — log only, allow write), ``"alert"`` (log
    + notify operator, allow write), or ``"enforce"`` (audit-snapshot
    and refuse the write).

    The plan rolls this out as observe → alert → enforce, with operator
    inspection of the audit table at each boundary.
    """
    if _disabled_all() or not _env_bool("ROBOTHOR_RIP_7_ENABLED"):
        return "off"
    raw = _resolve_raw("ROBOTHOR_RIP_7_MODE", "observe").strip().lower()
    if raw in _VALID_RIP_7_MODES:
        return raw  # type: ignore[return-value]
    return "observe"


def deferred_tools_enabled() -> bool:
    """Return True iff deferred/searchable tool loading (Rip 16 / G4) is active.

    When on, broad-access agents are advertised only a small CORE_TOOLS set plus
    the tool_search/tool_describe/tool_call meta-tools; the rest of their allowed
    tools load on demand. Off by default; gated by ``ROBOTHOR_RIP_16_ENABLED``.
    """
    return is_rip_enabled(16)


def cron_warmup_recall_enabled() -> bool:
    """Give cron/scheduled runs entity-aware recall seeded from the agent goal.

    Interactive (Telegram) warmup already pulls entity facts from the user's
    message; autonomous cron runs do not, so the fleet starts a turn with no
    query-relevant recall and re-derives known context (R2). When on,
    ``build_warmth_preamble`` adds a goal-seeded entity-recall section. Default
    OFF; gated by ``MEMORY_CRON_WARMUP_RECALL``.
    """
    return _env_bool("MEMORY_CRON_WARMUP_RECALL")


def narrow_memory_search_enabled() -> bool:
    """Make the default search_memory tool path fact-only (no auto fan-out).

    The default (RIP-15-off) path hard-codes expand_entities/include_insights/
    include_episodes=True on every call, so a narrow lookup pays for a full
    fan-out (R2, token waste). When on, those default to False (facts only).
    Default OFF (observe token delta first); gated by ``MEMORY_NARROW_SEARCH``.
    """
    return _env_bool("MEMORY_NARROW_SEARCH")


def compaction_hardening_enabled() -> bool:
    """Return True iff compaction hardening (Rip 18 / G7) is active.

    When on, compaction runs a cheap LLM-free pre-pass before summarizing:
    dedup identical tool results (keep the newest full copy) and strip
    historical media (base64 images) outside the protected recent tail — both
    pure token wins with no information loss the agent can't re-fetch. Off by
    default; gated by ``ROBOTHOR_RIP_18_ENABLED``.
    """
    return is_rip_enabled(18)


def catalog_backed_models_enabled() -> bool:
    """Return True iff the catalog-backed model registry (Rip 17 / G6) is active.

    When on: (1) litellm pricing is registered from the single ``_MODEL_REGISTRY``
    source instead of a separate hand-maintained dict (ends drift), and (2)
    unknown models fall back to litellm's bundled catalog (accurate window +
    pricing for hundreds of models) instead of a flat 128K/8K guess. Off by
    default; gated by ``ROBOTHOR_RIP_17_ENABLED``.
    """
    return is_rip_enabled(17)


def goal_judge_enabled() -> bool:
    """Return True iff the goal-judge (self-improvement Phase 1) is active.

    When on, the ``judge_run`` tool grades an agent's recent runs against real
    outcome signals (declared goal, run trace, operator words, obstacles) and
    writes ``agent_reviews`` rows with ``reviewer_type='judge'`` that become the
    spine of the achievement score. Off by default; gated by
    ``ROBOTHOR_JUDGE_ENABLED`` (not a numbered rip — a distinct subsystem).
    Forced off by ``ROBOTHOR_DISABLE_ALL_RIPS=1``.
    """
    if _disabled_all():
        return False
    return _env_bool("ROBOTHOR_JUDGE_ENABLED")


def curator_enabled() -> bool:
    """True iff the destructive LLM skill-consolidation curator (Rip 5) is active.

    Gates ONLY the LLM consolidation pass (merge/archive). The non-destructive
    lifecycle (apply_skill_lifecycle: time-based stale/archived state) runs
    unconditionally in the daemon loop. Off by default; gated by
    ``ROBOTHOR_RIP_5_ENABLED``. Forced off by ``ROBOTHOR_DISABLE_ALL_RIPS=1``.
    """
    return is_rip_enabled(5)


def deferred_tools_threshold() -> int:
    """Min advertised-tool count above which an agent's set is deferred.

    Agents with curated small toolsets stay fully advertised (no extra
    tool_search round-trip); only broad-access agents (e.g. main) defer.
    Reads ``ROBOTHOR_DEFERRED_TOOLS_THRESHOLD`` (default 40).
    """
    raw = os.environ.get("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD", "").strip()
    if not raw:
        return 40
    try:
        return max(1, int(raw))
    except ValueError:
        return 40


def symbolic_memory_mode() -> SymbolicMode:
    """Return the symbolic-compaction mode for tool logs (Rip 13).

    ``"off"`` when Rip 13 is disabled (or the global panic flag is set).
    Otherwise reads ``ROBOTHOR_RIP_13_MODE``: ``"observe"`` (default — build
    the symbol graph and log the would-be token savings, but leave injected
    context unchanged) or ``"enforce"`` (inject the compact graph in place of
    raw tool output). Rolls out observe → enforce.
    """
    if _disabled_all() or not _env_bool("ROBOTHOR_RIP_13_ENABLED"):
        return "off"
    raw = _resolve_raw("ROBOTHOR_RIP_13_MODE", "observe").strip().lower()
    if raw in _VALID_SYMBOLIC_MODES:
        return raw  # type: ignore[return-value]
    return "observe"


def per_user_sessions_mode() -> PerUserSessionsMode:
    """Return the webchat per-user session-key derivation mode (Task 3, Unified
    Identity Context).

    Unlike the two-var ``*_ENABLED`` + ``*_MODE`` ladders above, this is a
    single env var, ``ROBOTHOR_PER_USER_SESSIONS``, since there's no separate
    subsystem-enabled gate to flip independently of rollout stage. Returns
    ``"off"`` (default — every caller gets the requested session key
    unchanged, identical to pre-flag behavior), ``"observe"`` (still return
    the requested key unchanged, but log what would have been derived for
    non-owner/non-service callers), or ``"enforce"`` (member callers are
    isolated onto their own derived session; owner and service callers are
    unaffected in every mode — see ``chat._effective_session_key``).
    """
    raw = _resolve_raw("ROBOTHOR_PER_USER_SESSIONS", "off").strip().lower()
    if raw in _VALID_PER_USER_SESSIONS_MODES:
        return raw  # type: ignore[return-value]
    return "off"


TelegramRoleGatesMode = Literal["off", "observe", "enforce"]
_VALID_TELEGRAM_ROLE_GATES_MODES = frozenset(("off", "observe", "enforce"))


def telegram_role_gates_mode() -> TelegramRoleGatesMode:
    """Return the Telegram owner-gate rollout mode (Task 4, Unified Identity
    Context).

    Single env var ``ROBOTHOR_TELEGRAM_ROLE_GATES``, same single-var ladder
    shape as ``per_user_sessions_mode`` (Task 3): there's no separate
    subsystem-enabled gate to flip independently of rollout stage. Returns
    ``"off"`` (default — every owner-only Telegram surface (``/restart``,
    ``/agents``, ``/steer``, the ``perm:``/``dp:``/``runctl:`` callbacks)
    keeps its original chat_id-equality check only, identical to pre-flag
    behavior — the hole this flag exists to close: a non-owner member
    posting from the operator's own chat_id passes), ``"observe"`` (evaluate
    both the legacy chat_id check and the new per-sender role check, but
    still enforce the OLD chat_id check; log a structured divergence line
    whenever they disagree so an operator can audit what enforce would
    decide before flipping it), or ``"enforce"`` (role check only — chat_id
    is irrelevant to authorization from here on).

    See ``telegram.TelegramBot._check_owner_gate`` for the ladder
    implementation and ``telegram.TelegramBot._sender_is_owner`` for the
    per-sender role resolution.
    """
    raw = _resolve_raw("ROBOTHOR_TELEGRAM_ROLE_GATES", "off").strip().lower()
    if raw in _VALID_TELEGRAM_ROLE_GATES_MODES:
        return raw  # type: ignore[return-value]
    return "off"


DataScopingMode = Literal["off", "observe", "enforce"]
_VALID_DATA_SCOPING_MODES = frozenset(("off", "observe", "enforce"))


def data_scoping_mode() -> DataScopingMode:
    """Return the "own data + shared" row-scoping rollout mode (Task 5,
    Unified Identity Context).

    Single env var ``ROBOTHOR_DATA_SCOPING``, same single-var ladder shape as
    ``per_user_sessions_mode`` (Task 3) and ``telegram_role_gates_mode``
    (Task 4): there's no separate subsystem-enabled gate to flip
    independently of rollout stage. Returns ``"off"`` (default — every data
    read tool queries unrestricted, identical to pre-flag behavior),
    ``"observe"`` (still query unrestricted, but log how many rows a
    restricted caller's query WOULD have dropped under the "own data +
    shared" rule — see ``robothor.identity.scope``), or ``"enforce"``
    (restricted callers — role not in {owner, admin, service} — only see
    their own person-linked rows plus org-general (person_id IS NULL) rows).

    ``identity=None`` callers (system/cron/heartbeat runs that never resolve
    an interactive identity) are unrestricted in every mode — see
    ``robothor.identity.scope.scope_for``.
    """
    raw = _resolve_raw("ROBOTHOR_DATA_SCOPING", "off").strip().lower()
    if raw in _VALID_DATA_SCOPING_MODES:
        return raw  # type: ignore[return-value]
    return "off"


def allow_unregistered_owner_fallback() -> bool:
    """Escape hatch for a fresh install with no ``tenant_users`` rows yet.

    ``ROBOTHOR_TELEGRAM_ROLE_GATES=enforce`` stops fabricating an owner
    identity for an unregistered sender posting in the primary operator
    chat (``_resolve_user``'s old ``default_chat_id`` fallback) — but a
    brand-new instance has no registered owner row at all, so enforce would
    otherwise lock the operator out of their own bot before they've had a
    chance to run ``robothor user add``. Setting
    ``ROBOTHOR_ALLOW_UNREGISTERED_OWNER_FALLBACK=1`` restores the
    fabrication under enforce, for that bootstrap window only. Default off
    — leave off once the owner row exists.
    """
    return _env_bool("ROBOTHOR_ALLOW_UNREGISTERED_OWNER_FALLBACK")


def open_onboarding_enabled() -> bool:
    """Return True iff unknown private Telegram senders get self-service
    onboarding (``onboarding.start_onboarding``, creating a new tenant).

    Default OFF (Task 4, Unified Identity Context, operator decision to
    close self-provisioning): an unrecognized private sender gets a generic
    refusal instead, and the operator is notified (rate-limited) with a
    ``robothor user add`` hint. Gated by ``ROBOTHOR_OPEN_ONBOARDING`` — set
    to a truthy value to restore the old open-signup flow.
    """
    return _env_bool("ROBOTHOR_OPEN_ONBOARDING")


def _enforcement_mode(enabled_var: str, mode_var: str) -> EnforcementMode:
    """Generic observe→alert→enforce ladder gated on two env vars.

    Returns ``"off"`` when the global panic flag is set or ``enabled_var``
    is falsy. Otherwise reads ``mode_var`` and returns ``"observe"``
    (default — compute the verdict, log it, but DO NOT act), ``"alert"``
    (observe + notify the operator), or ``"enforce"`` (apply the verdict).

    The default of ``observe`` plus a behavior-preserving default elsewhere
    means flipping ``enabled_var`` on is a no-op until ``mode_var`` is
    promoted, and rollback is instant.
    """
    if _disabled_all() or not _env_bool(enabled_var):
        return "off"
    raw = _resolve_raw(mode_var, "observe").strip().lower()
    if raw == "off":
        # `off` is advertised by flags.store.valid_values_for for every
        # governed *_MODE flag, and the Controls API accepts, persists and
        # audits it — but it used to fall through to `observe` here, so the
        # operator's de-escalation lever was inert. Reading it as written
        # keeps the API and the engine describing the same ladder.
        return "off"
    if raw in _VALID_ENFORCEMENT_MODES:
        return raw  # type: ignore[return-value]
    return "observe"


def execution_mode_admission_mode() -> EnforcementMode:
    """Rollout mode for fleet admission control (the FleetPool gate).

    Gated on ``ROBOTHOR_ADMISSION_ENABLED`` + ``ROBOTHOR_ADMISSION_MODE``.
    ``observe`` computes the verdict and records the deferral it WOULD have
    made while still running the agent; ``enforce`` actually defers background
    work when the device is full. Default ``off`` reproduces the behaviour
    FleetPool had for its whole existence: none.
    """
    return _enforcement_mode("ROBOTHOR_ADMISSION_ENABLED", "ROBOTHOR_ADMISSION_MODE")


def sandbox_default_mode() -> EnforcementMode:
    """Rollout mode for defaulting exec-holding agents into the Docker sandbox.

    Gated on ``ROBOTHOR_SANDBOX_DEFAULT_ENABLED`` + ``ROBOTHOR_SANDBOX_DEFAULT_MODE``.
    ``observe`` logs which agents/runs WOULD be sandboxed (running on host as
    today); ``enforce`` sets ``sandbox=docker`` for exec-holding agents that
    have not opted out via ``sandbox: host``.
    """
    return _enforcement_mode("ROBOTHOR_SANDBOX_DEFAULT_ENABLED", "ROBOTHOR_SANDBOX_DEFAULT_MODE")


def rbac_enforcement_mode() -> EnforcementMode:
    """Rollout mode for RBAC over system/cron/hook runs.

    Gated on ``ROBOTHOR_RBAC_ENABLED`` + ``ROBOTHOR_RBAC_MODE``. ``observe``
    computes the permission verdict and logs would-denies but returns allow;
    ``enforce`` actually denies. The default ``service`` role is allow-all, so
    observe should surface zero would-denies unless a role was deliberately
    tightened.
    """
    return _enforcement_mode("ROBOTHOR_RBAC_ENABLED", "ROBOTHOR_RBAC_MODE")


def approval_mode() -> EnforcementMode:
    """Rollout mode for fail-closed human-approval escalation.

    Gated on ``ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED`` + ``ROBOTHOR_APPROVAL_MODE``.
    ``observe`` logs escalations that WOULD be denied (no/declining manager) but
    proceeds (auto-approves, as today); ``enforce`` denies the tool call when no
    approver is reachable.
    """
    return _enforcement_mode("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "ROBOTHOR_APPROVAL_MODE")


def exec_allowlist_mode() -> EnforcementMode:
    """Rollout mode for rejecting shell-chaining metacharacters in allowlisted exec.

    Gated on ``ROBOTHOR_EXEC_ALLOWLIST_STRICT_ENABLED`` + ``ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE``.
    ``observe`` logs commands that WOULD be blocked for containing shell control
    characters (``;`` ``|`` ``&`` ``<`` ``>`` ``$(`` backtick) that let an
    attacker chain past an allowlisted prefix, but allows them (legacy behavior);
    ``enforce`` blocks them. Default off preserves today's behavior.
    """
    return _enforcement_mode(
        "ROBOTHOR_EXEC_ALLOWLIST_STRICT_ENABLED", "ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE"
    )


def deliverable_contract_mode() -> EnforcementMode:
    """Rollout mode for task-deliverable verification.

    Gated on ``ROBOTHOR_DELIVERABLE_CONTRACT_ENABLED`` +
    ``ROBOTHOR_DELIVERABLE_CONTRACT_MODE``.

    The complement of ``completion_contract_mode``. That one asks whether the
    agent's *claims* are backed by evidence in the trace; this one asks
    whether the artifact the *task* named actually exists. An agent can pass
    the first and fail the second by doing the work correctly and saving it
    to the wrong path — measured on 2026-08-26 as -0.87 of a -1.04
    competitive gap in which 7 of 10 tasks were at parity.

    ``observe`` logs the verdict, ``alert`` tells the operator, ``enforce``
    records it as a guardrail block. Default off.
    """
    return _enforcement_mode(
        "ROBOTHOR_DELIVERABLE_CONTRACT_ENABLED", "ROBOTHOR_DELIVERABLE_CONTRACT_MODE"
    )


def completion_contract_mode() -> EnforcementMode:
    """Rollout mode for evidence-based completion contracts.

    Gated on ``ROBOTHOR_COMPLETION_CONTRACTS_ENABLED`` + ``ROBOTHOR_COMPLETION_CONTRACTS_MODE``.
    When an agent's final output claims a session goal is done, ``observe``
    logs the completion verdict (satisfied/missing evidence) but never blocks;
    ``enforce`` keeps the task open and writes a next_action describing the
    missing evidence when the claim isn't backed by validated evidence.
    Default off.
    """
    return _enforcement_mode(
        "ROBOTHOR_COMPLETION_CONTRACTS_ENABLED", "ROBOTHOR_COMPLETION_CONTRACTS_MODE"
    )


def benchmark_decontamination_mode() -> EnforcementMode:
    """Rollout mode for keeping benchmark-harness traffic out of production metrics.

    Gated on ``ROBOTHOR_BENCHMARK_DECONTAMINATION_ENABLED`` +
    ``ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE``.

    - ``observe`` (default once enabled): analytics still reports the legacy
      numbers, but measures how much of each surface is benchmark traffic and
      returns it separately (``benchmark_runs`` / ``benchmark_cost_usd``).
    - ``alert``: observe + notify the operator that production metrics are
      contaminated.
    - ``enforce``: exclude benchmark runs from every production surface, and
      spawn benchmark sub-runs with a parent linkage so they stop looking like
      top-level production runs in the first place.

    Default off — the merge posture is a pure no-op.
    """
    return _enforcement_mode(
        "ROBOTHOR_BENCHMARK_DECONTAMINATION_ENABLED",
        "ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE",
    )


def run_verification_mode() -> EnforcementMode:
    """Rollout mode for verifying a finished run's claims against its tool trace.

    Gated on ``ROBOTHOR_RUN_VERIFICATION_ENABLED`` + ``ROBOTHOR_RUN_VERIFICATION_MODE``.
    ``observe`` computes the verdict, stamps ``agent_runs.verified_status`` /
    ``verification`` and records a guardrail event — nothing else changes.
    ``alert`` tells the truth to the operator without changing task state: it
    notifies, appends the honest-failure banner to the delivered message
    (``delivery._verification_banner``), injects the honest-claims prompt rule
    (``prompts.behavioral_rules``) and labels every auto-written resolution
    ``[verified]`` / ``[claimed]``. ``enforce`` acts: an unverified run does
    not resolve its CRM task at all (``runner._update_task_for_run`` writes a
    ``next_action`` naming the unsupported claims instead), and a benchmark
    run never resolves a production task. Default off.

    Unlike ``completion_contract_mode`` this needs no session goal and is not
    limited to "task complete" phrasings — see ``run_verification`` for the
    production run that motivated it.
    """
    return _enforcement_mode("ROBOTHOR_RUN_VERIFICATION_ENABLED", "ROBOTHOR_RUN_VERIFICATION_MODE")


def honesty_suite_mode() -> HonestySuiteMode:
    """Rollout mode for the fleet-wide honesty cases in every benchmark suite.

    Gated on ``ROBOTHOR_HONESTY_SUITE_MODE`` alone — there is no ``_ENABLED``
    companion, because the default here is **observe, not off**. The cases only
    read: they spawn the same sandboxed sub-agent runs the fleet already runs
    nightly, and a case nobody runs measures nothing.

    - ``off``: the shared cases are not merged into any suite at all.
    - ``observe`` (default): the cases run, are graded and are reported
      (``honesty`` in the run record, per-case verdicts in the failures list),
      but stay OUT of the weighted aggregate — the fleet's headline number does
      not move before anyone has read the verdicts.
    - ``enforce``: honesty cases count toward the grade like any other case.

    ``alert`` is deliberately absent: this is a grader, not a guardrail — it
    blocks nothing, so there is no "would have blocked" event to page about.
    """
    if _disabled_all():
        return "off"
    raw = _resolve_raw("ROBOTHOR_HONESTY_SUITE_MODE", "observe").strip().lower()
    if raw in _VALID_HONESTY_SUITE_MODES:
        return raw  # type: ignore[return-value]
    return "observe"


def do_not_contact_mode() -> DoNotContactMode:
    """Rollout mode for the ``crm_people.do_not_contact`` outbound-email guard.

    Governed flag ``ROBOTHOR_DNC_MODE``, so it resolves DB-store-first and
    falls back to the environment. That matters more here than for most: this
    is the one control that can silence itself, and routing it through the
    store is what makes a flip visible in ``/api/controls``, flippable from the
    dashboard, and recorded in the flag audit log instead of being an
    untraceable edit on a box.

    - ``enforce`` (default): a message to anyone flagged ``do_not_contact`` is
      refused, and so is a message whose opt-out lookup could not be read.
    - ``observe``: the same checks run and still write their
      ``agent_guardrail_events`` row (action ``observed``), but the message
      goes out.

    Two deliberate differences from every other flag in this module:

    * **No ``off`` rung.** The ladder here is two rungs. An opt-out that can be
      turned all the way off is not a compliance control, and ``observe``
      already gives an operator everything ``off`` would — mail flows — while
      keeping the evidence.
    * **``ROBOTHOR_DISABLE_ALL_RIPS`` does NOT disable it.** The panic switch
      exists to force new *behaviour* dark; this is a legal obligation, not a
      new behaviour, and a panic state that mails the people who asked not to
      be mailed is not a safe state to panic into.

    Read on every call, never memoised here: the store's own TTL cache holds
    the DB answer, and when there is none the environment is read live, so
    ``systemctl set-environment`` takes effect without a code change.
    Anything unrecognised enforces, loudly — a typo in an env var must not
    switch off a compliance control.
    """
    raw = _resolve_raw("ROBOTHOR_DNC_MODE", "enforce").strip().lower()
    if raw in _VALID_DO_NOT_CONTACT_MODES:
        return raw  # type: ignore[return-value]
    if raw not in ("", "enforce"):
        logger.warning("ROBOTHOR_DNC_MODE=%r is not 'enforce' or 'observe' — enforcing.", raw)
    return "enforce"


def benchmark_sandbox_mode() -> EnforcementMode:
    """Rollout mode for seeded benchmark fixtures + sandbox CRM writes.

    Gated on ``ROBOTHOR_BENCHMARK_SANDBOX_ENABLED`` +
    ``ROBOTHOR_BENCHMARK_SANDBOX_MODE``. ``off`` (default) is today's harness
    exactly: benchmark sub-runs stay read-only, no fixtures are seeded, and no
    state check runs. ``observe`` seeds each task's fixtures into the dedicated
    ``benchmark-sandbox`` tenant, scopes the sub-run to it, re-allows the
    sandbox-safe CRM writes (see ``robothor.engine.benchmark_sandbox``) and
    RECORDS every read-back on the task result without folding it into the
    score. ``alert`` is observe plus an error log per failed read-back.
    ``enforce`` folds the read-backs into the task score.

    Said plainly, because the ladder is unusual here: ``observe`` changes what a
    benchmark sub-agent can *do* — that is the point, since the rubrics grade
    actions the harness denied — but not how the run is *graded*.
    """
    return _enforcement_mode(
        "ROBOTHOR_BENCHMARK_SANDBOX_ENABLED", "ROBOTHOR_BENCHMARK_SANDBOX_MODE"
    )


def tool_verify_mode() -> EnforcementMode:
    """Rollout mode for tool-level post-condition checks.

    Gated on ``ROBOTHOR_TOOL_VERIFY_ENABLED`` + ``ROBOTHOR_TOOL_VERIFY_MODE``.
    After a side-effectful tool reports success, an independent read-back of
    the environment decides whether the write actually landed (see
    ``robothor.engine.tools.verification``).

    ``observe`` (default) records the verdict in the ``agent_run_evidence``
    ledger and changes nothing the model sees; ``alert`` also pages the
    operator on a failed read-back; ``enforce`` injects ``verification_failed``
    plus an actionable message INTO the tool result, so the agent learns
    in-loop that its action did not take effect instead of reporting it as
    done. Default off — flipping ``_ENABLED`` on lands in observe.
    """
    return _enforcement_mode("ROBOTHOR_TOOL_VERIFY_ENABLED", "ROBOTHOR_TOOL_VERIFY_MODE")


def injection_scan_mode() -> EnforcementMode:
    """Rollout mode for prompt-injection scanning of assembled system-run prompts.

    Gated on ``ROBOTHOR_INJECTION_SCAN_ENABLED`` + ``ROBOTHOR_INJECTION_SCAN_MODE``.
    ``observe``/``alert`` log when an assembled cron/hook prompt (incl. recalled
    memory + skills) matches an injection signal but run anyway; ``enforce``
    aborts the run. Default off.
    """
    return _enforcement_mode("ROBOTHOR_INJECTION_SCAN_ENABLED", "ROBOTHOR_INJECTION_SCAN_MODE")


def _post_telegram(text: str) -> bool:
    """Deliver to the operator's actual channel. Best-effort, never raises.

    The DB notification is an audit *record*; delivery is a separate question.
    ``warmup.py`` now reads ``crm_agent_notifications`` and surfaces unread
    ``alert_digest``/``alert_fallback`` rows to the operator-facing agent (see
    ``_build_unread_alerts_section``), so a row is no longer write-only — but it
    only reaches the operator on that agent's *next* run. A guardrail breach is
    not a next-run matter, so it also goes straight to Telegram: the channel the
    operator actually watches, the same one the failure pager and the soak nags
    use.

    (Schema note, since the older version of this comment got it wrong: the
    notification tools are registered — ``send_notification``/``get_inbox``/
    ``ack_notification`` come from ``robothor/api/mcp.py::get_tool_definitions``,
    which ``ToolRegistry._register_all`` folds in alongside ``tools/schemas.py``.
    ``test_alert_digest_reader.py`` pins that parity.)
    """
    import urllib.parse
    import urllib.request

    token = os.environ.get("ROBOTHOR_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("ROBOTHOR_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return bool(resp.status == 200)
    except Exception as exc:
        logger.error("guardrail alert could not be delivered to Telegram: %s", exc)
        return False


def notify_guardrail_alert(
    *,
    guardrail_name: str,
    agent_id: str,
    reason: str,
    tenant_id: str = "",
) -> bool:
    """Notify the operator that a guardrail in ``alert`` mode would have blocked.

    This is what makes the middle rung of the observe→alert→enforce ladder
    real: ``observe`` records evidence silently, ``alert`` also puts it in
    front of the operator (via the agent-to-agent notification surface, which
    main's heartbeat surfaces), and ``enforce`` acts on it.

    Best-effort: a failed notification must never break the run — but it is
    logged at error level, because an alert nobody receives is the failure
    mode this rung exists to prevent.
    """
    try:
        from robothor.constants import DEFAULT_TENANT
        from robothor.crm import dal

        # "escalation" — NOT "alert": the crm_agent_notifications check
        # constraint rejects "alert", so that INSERT is refused and the
        # operator is never told. send_notification swallows the failure and
        # returns None, so the returned id is the only proof of delivery.
        notif_id = dal.send_notification(
            from_agent="engine",
            to_agent="main",
            notification_type="escalation",
            subject=f"Guardrail would block: {guardrail_name}",
            body=(
                f"{guardrail_name} is in alert mode and would have BLOCKED this "
                f"call under enforce.\n\nAgent: {agent_id}\nReason: {reason}\n\n"
                f"Promote to enforce (docs/runbooks/GUARDRAIL_FLIPS.md) or fix "
                f"the agent behavior."
            ),
            tenant_id=tenant_id or DEFAULT_TENANT,
        )
        if not notif_id:
            logger.error(
                "guardrail %s is in alert mode but the operator notification was "
                "dropped — the alert rung is not delivering",
                guardrail_name,
            )

        # The DB row is the audit record; Telegram is the delivery. The rung is
        # only real if it lands where the operator actually looks.
        delivered = _post_telegram(
            f"\u26a0\ufe0f {guardrail_name} would BLOCK under enforce\n\n"
            f"Agent: {agent_id}\nReason: {reason}\n\n"
            "Promote to enforce or fix the agent (docs/runbooks/GUARDRAIL_FLIPS.md)."
        )
        if not delivered:
            logger.error(
                "guardrail %s alert was not delivered to the operator's channel",
                guardrail_name,
            )
        return bool(notif_id) or delivered
    except Exception as exc:
        logger.error(
            "guardrail %s is in alert mode but the operator notification failed: %s",
            guardrail_name,
            exc,
        )
        return False


#: The guardrails whose absence makes a process unguarded. Kept here rather
#: than in the daemon so any entry point can report the same posture.
_SECURITY_GUARDRAILS: dict[str, str] = {
    "rbac": "rbac_enforcement_mode",
    "injection_scan": "injection_scan_mode",
    "exec_allowlist_strict": "exec_allowlist_mode",
    "approval": "approval_mode",
    "sandbox_default": "sandbox_default_mode",
}


def security_posture() -> dict[str, str]:
    """What each security guardrail resolves to IN THIS PROCESS.

    Per-process, deliberately. The flags are set by ``Environment=`` lines in
    a drop-in on a single systemd unit, so a second daemon running the same
    engine code inherits none of them. That is not hypothetical: this
    instance ran a second engine daemon with all five of these off for four
    days, and nothing anywhere said so.
    """
    posture: dict[str, str] = {}
    for name, fn_name in _SECURITY_GUARDRAILS.items():
        fn = globals().get(fn_name)
        try:
            posture[name] = str(fn()) if callable(fn) else "unknown"
        except Exception:  # noqa: BLE001 - a posture report must never crash a boot
            posture[name] = "unknown"
    return posture


def log_security_posture() -> None:
    """State the posture at startup, and warn when a guardrail is off.

    A process that is unguarded should say so in its own journal. Reading a
    drop-in on another unit is not something anyone does at 2am.
    """
    posture = security_posture()
    rendered = ", ".join(f"{k}={v}" for k, v in sorted(posture.items()))
    off = sorted(k for k, v in posture.items() if v == "off")
    if off:
        logger.warning(
            "Security posture: %s — this process is running UNGUARDED for: %s. "
            "Guardrail flags are per-process; check this unit's environment, "
            "not another unit's drop-in.",
            rendered,
            ", ".join(off),
        )
    else:
        logger.info("Security posture: %s", rendered)
