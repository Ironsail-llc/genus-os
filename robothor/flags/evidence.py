"""Per-flag evidence sources and the INERT/BLIND/ENFORCING/UNPROVEN verdict.

Each governed flag's control writes evidence to a DIFFERENT table: RBAC,
injection-scan, exec-allowlist, human-approval, sandbox-default, and
completion-contracts all write ``agent_guardrail_events`` keyed by
``guardrail_name`` (robothor/engine/runner.py, robothor/engine/guardrails.py).
RIP-7's drift detector snapshots its OWN audit table, ``memory_facts_audit``
(robothor/memory/drift.py), not ``agent_guardrail_events``. RIP-5's curator
writes a per-tenant state row to ``crm_curator_state``
(crm/migrations/068_curator_state.sql), not an event log. The judge writes
``agent_reviews`` rows (robothor/engine/judge.py). Querying the wrong table for
a given flag returns a comforting zero and makes THIS detector a liar — so
every source is declared here, in code, cited against the writer it observes,
never inferred or copy-pasted across flags.

Three flags currently have NO durable DB evidence surface at all:

* RIP-4 (skill write-origin provenance) stamps a ``meta.json`` sidecar on
  disk (robothor/engine/skill_provenance.py) — never a DB row.
* RIP-13 (symbolic-memory compaction) only ``logger.info()``s its token
  savings (robothor/engine/session.py:_finalize_symbol_graph) — never a DB
  row either.
* The benchmark sandbox records its ``state_checks`` on each in-memory task
  result, which ``failures_brief`` drops before the ``benchmark_results`` row
  is written, and sweeps its seeded fixtures at teardown.

Their EvidenceSource entries point at ``agent_guardrail_events`` with a
``guardrail_name`` that nothing in the codebase ever inserts. That is not a
wrong-table bug — it is an honest zero: whichever real table you query, there
is genuinely no record proving these ran, so the detector correctly reports
INERT rather than inventing a signal that does not exist.

RIP-1 (background-review fork) is the one predicate NOT keyed by an
unambiguous ``guardrail_name``: see the note on its EvidenceSource entry
below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import psycopg2

from robothor.db.connection import get_connection

if TYPE_CHECKING:
    from datetime import datetime

Status = Literal["ENFORCING", "INERT", "BLIND", "UNPROVEN", "UNKNOWN"]


@dataclass(frozen=True)
class EvidenceSource:
    table: str
    where: str  # SQL predicate identifying a "fired" event for this control
    time_column: str = "created_at"  # not every evidence table is an append-only event log


@dataclass(frozen=True)
class Verdict:
    name: str
    mode: str
    status: Status
    last_fired: datetime | None
    count_7d: int
    message: str


EVIDENCE_SOURCES: dict[str, EvidenceSource] = {
    "ROBOTHOR_RBAC_MODE": EvidenceSource("agent_guardrail_events", "guardrail_name = 'rbac'"),
    "ROBOTHOR_INJECTION_SCAN_MODE": EvidenceSource(
        "agent_guardrail_events", "guardrail_name = 'injection_scan'"
    ),
    "ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE": EvidenceSource(
        "agent_guardrail_events",
        "guardrail_name = 'exec_allowlist' AND reason LIKE 'shell control characters%'",
    ),
    "ROBOTHOR_APPROVAL_MODE": EvidenceSource(
        "agent_guardrail_events", "guardrail_name = 'human_approval'"
    ),
    "ROBOTHOR_SANDBOX_DEFAULT_MODE": EvidenceSource(
        "agent_guardrail_events", "guardrail_name = 'sandbox_default'"
    ),
    "ROBOTHOR_COMPLETION_CONTRACTS_MODE": EvidenceSource(
        "agent_guardrail_events", "guardrail_name = 'completion_contract'"
    ),
    # Admission writes this row on the SHADOW path too, so observe produces
    # real evidence rather than silence -- which is the whole reason FleetPool
    # could sit with zero production callers and look indistinguishable from a
    # control that simply never needed to fire.
    "ROBOTHOR_ADMISSION_MODE": EvidenceSource(
        "agent_guardrail_events", "guardrail_name = 'execution_mode_admission'"
    ),
    # Written by robothor/engine/tools/handlers/gws.py::_log_dnc_block, on both
    # rungs: action 'blocked' under enforce, 'observed' under observe. Not
    # filtered on action — an observed row is still proof the check ran, which
    # is exactly what this detector asks. The one refusal that files NO row is
    # the unreadable-list branch, whose write would go to the database that had
    # just failed; that branch's evidence is an ERROR log line, so a run of them
    # correctly reads here as no evidence.
    "ROBOTHOR_DNC_MODE": EvidenceSource(
        "agent_guardrail_events", "guardrail_name = 'do_not_contact'"
    ),
    "ROBOTHOR_RIP_7_MODE": EvidenceSource(
        "memory_facts_audit",
        "reason = 'pre_update_drift_detected'",
        time_column="snapshot_at",
    ),
    "ROBOTHOR_RIP_13_MODE": EvidenceSource(
        "agent_guardrail_events", "guardrail_name = 'rip_13_symbolic_memory'"
    ),
    # NOTE: best-effort signature match for the background-review fork, not
    # an unambiguous guardrail_name like every other entry above. It could
    # over-count if some other code path ever spawns a sub_agent targeting
    # agent_id='main' (no such path exists today, so this is not live-wrong,
    # just an undocumented latent imprecision). Tightening it — e.g. keying
    # off a persisted spawn mode='background_review' column — is a tracked
    # follow-up.
    "ROBOTHOR_RIP_1_ENABLED": EvidenceSource(
        "agent_runs",
        "trigger_type = 'sub_agent' AND trigger_detail LIKE 'spawned_by:%' AND agent_id = 'main'",
    ),
    "ROBOTHOR_RIP_4_ENABLED": EvidenceSource(
        "agent_guardrail_events", "guardrail_name = 'rip_4_skill_provenance'"
    ),
    "ROBOTHOR_RIP_5_ENABLED": EvidenceSource(
        "crm_curator_state",
        "last_archived_count > 0 OR last_merged_count > 0",
        time_column="last_pass_at",
    ),
    "ROBOTHOR_JUDGE_ENABLED": EvidenceSource("agent_reviews", "reviewer_type = 'judge'"),
    # ── The six controls added to GOVERNED_FLAGS with the flag audit ────────
    "ROBOTHOR_RUN_VERIFICATION_MODE": EvidenceSource(
        "agent_guardrail_events", "guardrail_name = 'run_verification'"
    ),
    "ROBOTHOR_DELIVERABLE_CONTRACT_MODE": EvidenceSource(
        "agent_guardrail_events", "guardrail_name = 'deliverable_contract'"
    ),
    # The tool post-condition checker owns this table outright: nothing else in
    # the codebase inserts into it (robothor/engine/tools/verification.py
    # `_insert_evidence` is the only writer), so every row is one recorded
    # verdict and the predicate does not need to discriminate. It also emits a
    # `tool_postconditions` guardrail event, but only on the failure path —
    # counting those would report a working control as INERT for as long as it
    # kept finding nothing wrong.
    "ROBOTHOR_TOOL_VERIFY_MODE": EvidenceSource("agent_run_evidence", "verified IS NOT NULL"),
    # Decontamination has no event log. Its ONLY durable write is the operator
    # notification raised by `notify_guardrail_alert`
    # (robothor/engine/analytics.py) — which fires at `alert` and above; at
    # `observe` it emits a log line and nothing else. So an honest zero here
    # means "never escalated", which at observe is exactly what is expected,
    # and the verdict is INERT rather than a fabricated green.
    "ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE": EvidenceSource(
        "crm_agent_notifications",
        "subject = 'Guardrail would block: benchmark_decontamination'",
    ),
    # The honesty grade lands in `benchmark_results.failures` as a
    # `honesty_verdict` key, and only for cases that FAILED
    # (robothor/engine/tools/handlers/benchmark.py `failures_brief`). An
    # abstention that passes leaves no row, so this counts caught fabrications,
    # not suite executions — the number that matters for the promotion gate
    # ("at least one real fabrication surfaced and triaged").
    "ROBOTHOR_HONESTY_SUITE_MODE": EvidenceSource(
        "benchmark_results",
        "failures::text LIKE '%honesty_verdict%'",
        time_column="run_at",
    ),
    # No durable DB surface, same honest-zero as RIP-4 and RIP-13 above: the
    # sandbox's proof of work is `state_checks` on each in-memory task result,
    # and `failures_brief` drops that key before the benchmark_results row is
    # written. Its fixtures are swept from the sandbox tenant at teardown by
    # design, so even the seeded rows are gone by the time anyone looks.
    # Whichever real table you query there is genuinely no record, so this
    # correctly reports INERT rather than inventing a signal.
    "ROBOTHOR_BENCHMARK_SANDBOX_MODE": EvidenceSource(
        "agent_guardrail_events", "guardrail_name = 'benchmark_sandbox'"
    ),
}


def evidence_horizon_days(table: str) -> int | None:
    """How far back this evidence table can actually see, or None if forever.

    Derived from the retention policy that does the pruning rather than
    restated here. A second copy would drift from the thing that actually
    deletes the rows, and this module would then quote a horizon nobody
    enforces — the parallel-list failure it exists to detect.
    """
    from robothor.engine.retention import RETENTION_POLICY

    policy = RETENTION_POLICY.get(table)
    if policy is None or policy.get("action") == "update":
        return None
    days = policy.get("days")
    return int(days) if days else None


def _unknown_verdict(name: str, mode: str, table: str) -> Verdict:
    return Verdict(
        name=name,
        mode=mode,
        status="UNKNOWN",
        last_fired=None,
        count_7d=0,
        message=(
            f"evidence table '{table}' is not present in this database — "
            "this control cannot be assessed and is NOT confirmed working."
        ),
    )


def verdict(name: str, mode: str) -> Verdict:
    """Classify a governed flag's real-world evidence, read straight from the
    table its control actually writes (see ``EVIDENCE_SOURCES``).

    A missing evidence table (present in some deploys' migrations, absent in
    others — e.g. ``agent_reviews`` from an external infra migration, or a
    drifted local test DB missing ``memory_facts_audit``) must never crash
    this function or produce a false green: it returns the distinct
    ``UNKNOWN`` status instead, loud and never ENFORCING.
    """
    src = EVIDENCE_SOURCES[name]
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{src.table}",))
        (regclass,) = cur.fetchone()
        if regclass is None:
            return _unknown_verdict(name, mode, src.table)

        try:
            cur.execute(
                f"SELECT max({src.time_column}), "  # noqa: S608 -- table/where/time_column are code-declared constants above, never user input
                f"count(*) FILTER (WHERE {src.time_column} > now() - interval '7 days') "
                f"FROM {src.table} WHERE {src.where}"
            )
            last_fired, count_7d = cur.fetchone()
        except (psycopg2.errors.UndefinedTable, psycopg2.Error):
            conn.rollback()
            return _unknown_verdict(name, mode, src.table)
    count_7d = int(count_7d or 0)

    off = mode in ("off", "false", None)
    ever_fired = last_fired is not None

    if off:
        status: Status = "UNPROVEN"
        message = "disabled"
    elif not ever_fired:
        status = "INERT"
        # What the data supports, and no more. `agent_guardrail_events` is
        # pruned at 30 days, so an empty result cannot distinguish "never
        # fired" from "fired before the window". RBAC blocked 46 calls on
        # 2026-07-02 and has been correctly quiet since — its allow-all
        # `service` role gives it nothing to block — and the old wording
        # called that a control that had never worked.
        horizon = evidence_horizon_days(src.table)
        message = (
            f"no evidence in {horizon}d (older rows are pruned) — not demonstrably firing"
            if horizon
            else "no evidence on record — this control cannot protect you."
        )
    elif count_7d == 0:
        status = "UNPROVEN"
        message = f"last fired {last_fired:%Y-%m-%d} — nothing in 7d"
    else:
        status = "ENFORCING" if mode in ("enforce", "true") else "BLIND"
        message = f"last fired {last_fired:%Y-%m-%d %H:%M} ({count_7d} events / 7d)"

    return Verdict(
        name=name,
        mode=mode,
        status=status,
        last_fired=last_fired,
        count_7d=count_7d,
        message=message,
    )
