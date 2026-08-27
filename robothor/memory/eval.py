"""Memory evaluation harness — measures retrieval quality, not agent behavior.

This is deliberately separate from the agent benchmark runner
(``robothor/engine/tools/handlers/benchmark.py``), which is
prompt → agent → pattern-match and has no notion of seeding known facts.
Here we seed fixture facts into an isolated tenant, run the real memory
retrieval path (``search_facts``), and score how well retrieval surfaces
the planted facts.

Case kinds:
    recall    — gold fact must appear in the top-k results.
    temporal  — the most-recent ("latest") fact must rank first
                (supersession correctness).
    verbatim  — an exact, case-sensitive string must survive retrieval
                (drives the Knowledge Vault phase; paraphrase = fail).
    persona   — multi-session preference consistency (scored like recall).

The pure scoring/loading/reporting functions are importable and unit-testable
without a database or Ollama. The integration steps (``_ensure_tenant``,
``_seed_case``, ``_retrieve``, ``_cleanup_tenant``) require a live PostgreSQL
and local Ollama, and run only when the harness is executed end-to-end.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EVAL_TENANT = "memory-eval"


class EvalPreconditionError(RuntimeError):
    """The eval cannot run — as distinct from running and failing cases.

    Raised before any seeding so a misconfigured run refuses loudly instead of
    writing fixture facts somewhere it shouldn't, or reporting a shape that
    reads like a result.
    """


# Measured baseline on the 267-case corpus: 267/267. It was 253/267 until the
# 14 misses were traced to corpus defects rather than retrieval — temporal cases
# seeded the stale and current facts at the same instant (removing recency as a
# discriminator), and four verbatim cases carried no gold_exact, which is the
# only field score_verbatim reads.
#
# The floor stays well below 1.0 deliberately. The reranker is a model and
# individual cases flip between runs; a floor set flush against the observed
# best pages on noise, and a gate that pages on noise gets muted.
#
# NOT set to 1.0: demanding perfection from a 267-case generated corpus means
# the nightly unit fails every night, and a gate that always pages gets muted.
# That is the exact failure this overhaul exists to prevent.
DEFAULT_MIN_PASS_RATE = 0.90


def min_pass_rate() -> float:
    """Floor for the suite pass rate. Bad values fall back to the default.

    A typo must not parse as 0.0 and silently disable the gate.
    """
    raw = os.environ.get("MEMORY_EVAL_MIN_PASS_RATE", "").strip()
    if not raw:
        return DEFAULT_MIN_PASS_RATE
    try:
        value = float(raw)
    except ValueError:
        logger.warning("MEMORY_EVAL_MIN_PASS_RATE=%r is not a number; using default", raw)
        return DEFAULT_MIN_PASS_RATE
    if not 0.0 <= value <= 1.0:
        logger.warning("MEMORY_EVAL_MIN_PASS_RATE=%r out of range; using default", raw)
        return DEFAULT_MIN_PASS_RATE
    return value


def exit_code_for(report: dict[str, Any] | None, blocked: str | None) -> int:
    """Map an eval outcome to a process exit code.

    0 = the suite met its floor, 2 = it ran and fell below,
    3 = the suite could not run at all.

    Separating 3 from 2 is the point. Historically both collapsed to "non-zero",
    so a harness that could not execute — which is what row-level security had
    been doing to this eval for weeks — was indistinguishable from ordinary
    failures, and an empty or absent report read as success.
    """
    if blocked:
        return 3
    if not report:
        return 3
    total = int(report.get("total") or 0)
    if total <= 0:
        # 0/0 is vacuous, not a pass. Grading an empty suite green is how a
        # gate ends up certifying nothing.
        return 3
    floor = min_pass_rate()
    return 0 if (int(report.get("passed") or 0) / total) >= floor else 2


def report_to_benchmark_row(
    report: dict[str, Any],
    *,
    suite_path: str = "docs/benchmarks/memory/suite.yaml",
    triggered_by: str = "manual",
    experiment_id: str | None = None,
) -> dict[str, Any]:
    """Map an eval report onto a ``benchmark_results`` row.

    Pure, so the shape reaching the table is pinned by tests rather than
    discovered later from a dashboard that looks wrong.

    ``category_scores`` carries per-stratum pass rates because the existing
    visibility surfaces already read that column — a temporal regression then
    shows up where people are already looking, instead of needing a new surface.

    The suite is deliberately not converted to the fleet's ``tasks:`` form.
    That form runs an agent and pattern-matches its prose; this one seeds a
    known fact and checks whether retrieval returns it. Converting would trade
    deterministic ground truth for an LLM's wording.
    """
    total = int(report.get("total") or 0)
    passed = int(report.get("passed") or 0)
    by_kind = report.get("by_kind") or {}

    category_scores = {
        kind: (agg.get("passed", 0) / agg["total"] if agg.get("total") else 0.0)
        for kind, agg in by_kind.items()
    }

    failures = [
        {
            "case_id": c.get("case_id"),
            "category": c.get("kind"),
            "score": c.get("score"),
            "output_preview": (c.get("detail") or "")[:200],
        }
        for c in (report.get("cases") or [])
        if not c.get("passed")
    ]

    return {
        "agent_id": "memory",
        "suite_id": report.get("suite_id") or "memory-recall-v1",
        "suite_path": suite_path,
        "total_cases": total,
        "passed": passed,
        "failed": max(0, total - passed),
        # 0/0 must not serialise as a perfect score. A gate that grades an empty
        # suite green certifies nothing, which is the failure this eval exists
        # to stop being.
        "pass_rate": round(passed / total, 4) if total else 0.0,
        # Every case here is pass/fail — there is no partial credit to hold
        # apart, so the aggregate equals the pass rate. Written explicitly so
        # the column is never NULL for this suite (migration 103).
        "aggregate_score": round(passed / total, 4) if total else 0.0,
        "category_scores": category_scores,
        "failures": failures,
        "triggered_by": triggered_by,
        "experiment_id": experiment_id,
        # Local Ollama has no per-call price. 0.0 keeps the column meaningful;
        # omitting it would read as missing data.
        "cost_usd": 0.0,
    }


def record_benchmark_row(row: dict[str, Any]) -> bool:
    """Insert a benchmark_results row. Returns False on failure, never raises.

    Best-effort in the same spirit as the fleet runner's write-through: a
    reporting failure must not turn a passing eval into a failing process. The
    fleet's staleness check is what notices a persistently missing row.

    The one exception is ``DatabaseGuardError``: a row about to land in a
    non-test database from inside pytest is not a reporting failure, it is the
    defect that put 709 synthetic rows into production, and it propagates.

    Note benchmark_results carries a tenant_id defaulting to the primary tenant
    and sits inside migration 081's RLS loop, so this insert must run on a
    connection scoped to that tenant — not the eval's memory-eval scope.
    """
    import json

    from robothor.constants import DEFAULT_TENANT
    from robothor.db.connection import (
        DatabaseGuardError,
        assert_test_database_write,
        connection_database_name,
        get_connection,
    )

    try:
        with get_connection() as conn:
            assert_test_database_write(connection_database_name(conn), "benchmark_results")
            cur = conn.cursor()
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", (DEFAULT_TENANT,))
            cur.execute(
                """
                INSERT INTO benchmark_results
                  (agent_id, suite_id, suite_path, total_cases, passed, failed,
                   pass_rate, aggregate_score, category_scores, failures,
                   triggered_by, experiment_id, cost_usd, tenant_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s)
                """,
                (
                    row["agent_id"],
                    row["suite_id"],
                    row["suite_path"],
                    row["total_cases"],
                    row["passed"],
                    row["failed"],
                    row["pass_rate"],
                    row.get("aggregate_score", row["pass_rate"]),
                    json.dumps(row["category_scores"]),
                    json.dumps(row["failures"], default=str),
                    row["triggered_by"],
                    row["experiment_id"],
                    row["cost_usd"],
                    DEFAULT_TENANT,
                ),
            )
            conn.commit()
        return True
    except DatabaseGuardError:
        raise
    except Exception as e:
        logger.warning("could not record benchmark_results row: %s", e)
        return False


def preflight(tenant_id: str) -> str | None:
    """Return a human-readable blocker if the eval cannot run, else None.

    The blocker this exists for: row-level security (migration 081) applies a
    ``WITH CHECK`` on every tenant-scoped table, so seeding as ``memory-eval``
    from a process whose ``app.tenant_id`` is the production tenant raises
    InsufficientPrivilege partway through. Detecting it up front turns a
    confusing mid-run crash into an actionable message.
    """
    from robothor.db.connection import get_connection

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT current_setting('app.tenant_id', true)")
            row = cur.fetchone()
            bound = (row[0] if row else None) or ""
    except Exception as e:  # pragma: no cover - connection failures are their own blocker
        return f"cannot reach the database: {e}"

    if bound and bound != tenant_id:
        return (
            f"row-level security is bound to tenant {bound!r} but the suite seeds "
            f"as {tenant_id!r}; seeding would be rejected by the WITH CHECK policy. "
            f"Run this process with ROBOTHOR_TENANT_ID={tenant_id}."
        )
    return None


# recall/persona/noise/resolution → gold must appear in top-k (noise adds a
# distractor cloud; resolution seeds a detection→resolution arc). temporal →
# latest must rank first. verbatim → exact string must survive.
VALID_KINDS = frozenset({"recall", "temporal", "verbatim", "persona", "noise", "resolution"})

# direct   — store_fact, exact text, no LLM. Fast, but bypasses conflict
#            resolution, so a "stale then current" pair lands as two unrelated
#            active rows: a state production never reaches.
# resolve  — the real production write path (resolve_and_store). The second
#            fact is classified against the first and supersedes it, which is
#            what makes a temporal case test temporal behaviour at all.
# ingest   — full LLM extraction path (does a raw turn become a discrete fact?).
VALID_SEED_MODES = frozenset({"direct", "resolve", "ingest"})
_RECALL_KINDS = frozenset({"recall", "persona", "noise", "resolution"})
_DEFAULT_K = 5


@dataclass
class EvalCase:
    """A single memory-eval case loaded from a suite file."""

    id: str
    kind: str
    query: str
    gold: str | list[str] | None = None
    gold_exact: str | None = None
    k: int = _DEFAULT_K
    seed: list[dict[str, Any]] = field(default_factory=list)
    seed_mode: str = "direct"  # see VALID_SEED_MODES


@dataclass
class CaseResult:
    """Outcome of scoring one case against retrieved results."""

    case_id: str
    kind: str
    passed: bool
    score: float
    top_texts: list[str] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "passed": self.passed,
            "score": round(self.score, 4),
            "detail": self.detail,
        }


# --------------------------------------------------------------------------- #
# Pure scoring (no DB / no LLM)
# --------------------------------------------------------------------------- #


def score_recall(
    top_texts: list[str], gold: str | list[str] | None, k: int = _DEFAULT_K
) -> tuple[bool, float]:
    """Hit iff every gold substring appears (case-insensitively) in the top-k.

    Returns (passed, score) where score is the fraction of golds found.
    """
    if gold is None:
        return False, 0.0
    golds = [gold] if isinstance(gold, str) else list(gold)
    if not golds:
        return False, 0.0
    hay = [t.lower() for t in top_texts[:k]]
    found = sum(1 for g in golds if any(g.lower() in h for h in hay))
    score = found / len(golds)
    return score >= 1.0, score


def score_temporal(top_texts: list[str], gold: str | None) -> tuple[bool, float]:
    """Pass iff the latest fact (``gold``) ranks first."""
    if not top_texts or gold is None:
        return False, 0.0
    passed = gold.lower() in top_texts[0].lower()
    return passed, 1.0 if passed else 0.0


def score_verbatim(
    top_texts: list[str], gold_exact: str | None, k: int = _DEFAULT_K
) -> tuple[bool, float]:
    """Pass iff the exact (case-sensitive) string survives in any top-k result."""
    if not gold_exact:
        return False, 0.0
    found = any(gold_exact in t for t in top_texts[:k])
    return found, 1.0 if found else 0.0


def score_case(case: EvalCase, top_texts: list[str]) -> CaseResult:
    """Dispatch scoring by case kind."""
    if case.kind in _RECALL_KINDS:
        passed, score = score_recall(top_texts, case.gold, case.k)
    elif case.kind == "temporal":
        temporal_gold = case.gold if isinstance(case.gold, str) else None
        passed, score = score_temporal(top_texts, temporal_gold)
    elif case.kind == "verbatim":
        passed, score = score_verbatim(top_texts, case.gold_exact, case.k)
    else:
        raise ValueError(f"unknown eval case kind: {case.kind!r}")

    # Name the criterion that actually failed. Every kind used to report
    # "gold not found in top-k", including temporal — whose real criterion is
    # "ranked first". A gold sitting at position 2 was reported as missing,
    # which sent an investigation after a retrieval bug that did not exist.
    if passed:
        detail = ""
    elif case.kind == "temporal":
        gold_text = case.gold if isinstance(case.gold, str) else ""
        retrieved = any(gold_text.lower() in t.lower() for t in top_texts) if gold_text else False
        detail = (
            "gold retrieved but did not rank first (temporal cases require the "
            "current fact to outrank the stale one)"
            if retrieved
            else "gold not retrieved at all"
        )
    else:
        detail = f"gold not found in top-{case.k}"
    return CaseResult(
        case_id=case.id,
        kind=case.kind,
        passed=passed,
        score=score,
        top_texts=list(top_texts),
        detail=detail,
    )


# --------------------------------------------------------------------------- #
# Suite loading + reporting (no DB / no LLM)
# --------------------------------------------------------------------------- #


def _validated_seed_mode(raw: dict[str, Any]) -> str:
    """Reject an unknown seed_mode instead of silently defaulting to `direct`.

    A typo that falls back to `direct` quietly changes what the case measures —
    which is exactly how ten temporal cases ended up testing ranking in a state
    production never produces.
    """
    mode = raw.get("seed_mode", "direct")
    if mode not in VALID_SEED_MODES:
        raise ValueError(
            f"case {raw.get('id', '?')!r}: unknown seed_mode {mode!r} "
            f"(valid: {sorted(VALID_SEED_MODES)})"
        )
    return str(mode)


def load_suite(path: str | Path) -> tuple[dict[str, Any], list[EvalCase]]:
    """Parse a suite YAML into (meta, cases). Raises ValueError on bad kinds."""
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    suite_k = int(data.get("k", _DEFAULT_K))
    meta = {
        "id": data.get("id", ""),
        "description": data.get("description", ""),
        "k": suite_k,
    }

    cases: list[EvalCase] = []
    for raw in data.get("cases", []) or []:
        kind = raw.get("kind", "")
        if kind not in VALID_KINDS:
            raise ValueError(
                f"case {raw.get('id', '?')!r}: unknown kind {kind!r} (valid: {sorted(VALID_KINDS)})"
            )
        cases.append(
            EvalCase(
                id=raw.get("id", ""),
                kind=kind,
                query=raw.get("query", ""),
                gold=raw.get("gold"),
                gold_exact=raw.get("gold_exact"),
                k=int(raw.get("k", suite_k)),
                seed=raw.get("seed") or [],
                seed_mode=_validated_seed_mode(raw),
            )
        )
    return meta, cases


def format_report(report: dict[str, Any], *, as_json: bool = False) -> str:
    """Render a report dict as text (default) or JSON."""
    if as_json:
        return json.dumps(report, indent=2, default=str)

    lines = [f"Memory eval: {report.get('suite_id', '')}"]
    lines.append(f"  passed: {report.get('passed', 0)}/{report.get('total', 0)}")
    for kind, agg in report.get("by_kind", {}).items():
        lines.append(f"  {kind}: {agg['passed']}/{agg['total']}")
    for c in report.get("cases", []):
        mark = "PASS" if c.get("passed") else "FAIL"
        extra = f" — {c['detail']}" if c.get("detail") else ""
        lines.append(
            f"    [{mark}] {c.get('case_id')} ({c.get('kind')}) score={c.get('score')}{extra}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Integration steps (require live PostgreSQL + Ollama)
# --------------------------------------------------------------------------- #


def _ensure_tenant(tenant_id: str) -> None:
    """Idempotently create the eval tenant row so FK constraints hold."""
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO crm_tenants (id, display_name)
            VALUES (%s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (tenant_id, f"Memory Eval ({tenant_id})"),
        )


#: Facts seeded alongside every case so that retrieval has to actually
#: discriminate. Without them each case's corpus is <= k rows and top-k
#: returns everything, which is how this suite scored 266/267 with the entire
#: retrieval stack replaced by `ORDER BY created_at DESC LIMIT k` — no
#: embeddings, no BM25, no fusion, no reranker. A gate whose discriminative
#: power between 33 memory modules and one line of SQL is one case in 267 is
#: not a gate.
#:
#: Deliberately off-topic: they must be plausible facts about a working
#: instance, so they are realistic neighbours in embedding space, while never
#: answering any case's question.
_DISTRACTOR_CATEGORY = "__eval_distractor__"

_DISTRACTOR_FACTS: tuple[str, ...] = (
    "The office coffee machine is descaled on the first Monday of each month.",
    "Backups rotate at 05:30 and keep seven generations offsite.",
    "The staging environment uses a single shared database instance.",
    "Invoices are numbered sequentially starting from 1000 each fiscal year.",
    "The building's fire alarm is tested quarterly on a Wednesday morning.",
    "Parking permits expire at the end of March.",
    "The conference room projector needs an HDMI adapter for older laptops.",
    "Expense reports over 500 require a second approver.",
    "The support rota runs Monday to Friday with weekend cover on call.",
    "Laptops are replaced on a three-year cycle.",
    "The kitchen dishwasher runs automatically at 19:00.",
    "New starters get a welcome pack on their first day.",
    "The printer on the second floor jams with heavyweight paper.",
    "Quarterly reviews are scheduled in the last two weeks of the quarter.",
    "The visitor log is kept at reception for twelve months.",
    "Server room temperature is held between 18 and 22 degrees.",
    "The company uses a four-digit door code changed twice a year.",
    "Stationery orders are placed on the last Friday of the month.",
    "The lift is serviced every six months by an external contractor.",
    "Recycling is collected on Tuesday mornings.",
    "The window cleaner visits every eight weeks.",
    "Meeting rooms can be booked up to four weeks in advance.",
    "The first-aid kit is checked every February and August.",
    "Guest wifi credentials rotate weekly.",
    "The archive boxes are stored in the basement by year.",
)


async def _seed_distractors(tenant_id: str) -> int:
    """Seed the off-topic corpus once per suite run.

    Seeded once rather than per case because embedding 25 facts for each of
    267 cases would dominate the run. `_refresh_distractor_recency` then makes
    them the newest rows before each case, so recency cannot stand in for
    relevance.
    """
    from robothor.memory.facts import store_facts_batch

    payload = [
        {
            "fact_text": text,
            "category": _DISTRACTOR_CATEGORY,
            "importance_score": 0.5,
            "entities": [],
        }
        for text in _DISTRACTOR_FACTS
    ]
    ids = await store_facts_batch(
        payload,
        source_content="eval distractor corpus",
        source_type="eval_distractor",
        tenant_id=tenant_id,
    )
    return len(ids or [])


def _refresh_distractor_recency(tenant_id: str) -> None:
    """Make the distractors the newest rows in the tenant.

    This is what defeats a recency-only retriever. If the distractors were
    merely present but older, `ORDER BY created_at DESC LIMIT k` would still
    surface every case's freshly-seeded target and the suite would keep
    certifying nothing.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE memory_facts SET created_at = now() WHERE tenant_id = %s AND category = %s",
            (tenant_id, _DISTRACTOR_CATEGORY),
        )


def _cleanup_tenant(tenant_id: str, *, keep_distractors: bool = False) -> None:
    """Delete all seeded facts for the eval tenant. Never touches DEFAULT_TENANT."""
    from robothor.constants import DEFAULT_TENANT
    from robothor.db.connection import get_connection

    if tenant_id == DEFAULT_TENANT:
        logger.warning("refusing to cleanup memory_facts for DEFAULT_TENANT")
        return

    with get_connection() as conn:
        cur = conn.cursor()
        if keep_distractors:
            cur.execute(
                "DELETE FROM memory_facts WHERE tenant_id = %s AND (category IS DISTINCT FROM %s)",
                (tenant_id, _DISTRACTOR_CATEGORY),
            )
        else:
            cur.execute("DELETE FROM memory_facts WHERE tenant_id = %s", (tenant_id,))


def _signal_updates(
    ids: list[int], seed: list[dict[str, Any]]
) -> list[tuple[int, float | None, float | None]]:
    """Pure: pair seeded ids with any per-seed (importance, age_hours) overrides.

    ``store_facts_batch`` co-creates every seed at ``now()`` with the default
    importance (0.5), so recency and importance are uniform and cannot model the
    operator's real failure (a *fresh, high-importance* fact buried under *stale,
    low-importance* distractors). A seed item may carry ``importance`` and/or
    ``age_hours`` to break that symmetry. Returns only the rows needing a patch.
    """
    out: list[tuple[int, float | None, float | None]] = []
    for fact_id, item in zip(ids, seed, strict=False):
        imp = item.get("importance")
        age = item.get("age_hours")
        if imp is None and age is None:
            continue
        out.append(
            (fact_id, None if imp is None else float(imp), None if age is None else float(age))
        )
    return out


def _apply_seed_signals(ids: list[int], seed: list[dict[str, Any]], tenant_id: str) -> None:
    """Apply per-seed importance_score / created_at overrides (see _signal_updates)."""
    updates = _signal_updates(ids, seed)
    if not updates:
        return
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        for fact_id, imp, age in updates:
            sets: list[str] = []
            params: list[Any] = []
            if imp is not None:
                sets.append("importance_score = %s")
                params.append(imp)
            if age is not None:
                sets.append("created_at = now() - (%s || ' hours')::interval")
                params.append(str(age))
            params.extend([fact_id, tenant_id])
            cur.execute(
                f"UPDATE memory_facts SET {', '.join(sets)} WHERE id = %s AND tenant_id = %s",  # noqa: S608 — fixed column set, params bound
                params,
            )


async def _seed_case(case: EvalCase, tenant_id: str) -> list[int]:
    """Seed a case's fixture facts into the eval tenant.

    ``direct`` (default) stores the exact fact text via ``store_facts_batch``
    (no LLM paraphrase). ``ingest`` routes through the extraction pipeline
    (LLM), which is what the verbatim-vs-vault comparison exercises.

    Per-seed ``importance`` / ``age_hours`` overrides are applied after a direct
    seed so cases can model fresh-high-importance vs stale-low-importance facts.
    """
    if not case.seed:
        return []

    if case.seed_mode == "resolve":
        # The real production write path. Each fact is classified against what
        # is already stored, so a "stale then current" pair produces an actual
        # supersession (is_active=FALSE + superseded_by) exactly as it would in
        # production. Seeded in order — the sequence is the point.
        from robothor.memory.conflicts import resolve_and_store

        resolved_ids: list[int] = []
        for item in case.seed:
            fact = {
                "fact_text": item.get("fact_text", ""),
                "category": item.get("category", "other"),
                "entities": item.get("entities", []),
                "confidence": item.get("confidence", 0.9),
            }
            outcome = await resolve_and_store(
                fact,
                item.get("source_content") or fact["fact_text"],
                "eval",
                tenant_id=tenant_id,
            )
            new_id = outcome.get("new_id")
            if new_id:
                resolved_ids.append(int(new_id))
        return resolved_ids

    if case.seed_mode == "ingest":
        # Exercise the real LLM extraction path (the resolution-capture question:
        # does a raw confirmation turn become a discrete fact?), tenant-scoped so
        # it never pollutes the operator's data. ingest_content is not tenant-
        # aware, so go through extract_facts + store_facts_batch directly.
        from robothor.memory.facts import extract_facts, store_facts_batch

        ids: list[int] = []
        for item in case.seed:  # noqa: PERF401 — awaits + guard, not a comprehension
            content = item.get("source_content") or item.get("fact_text", "")
            extracted = await extract_facts(content)
            if extracted:
                ids.extend(
                    await store_facts_batch(
                        extracted,
                        source_content=content,
                        source_type="eval",
                        tenant_id=tenant_id,
                    )
                )
        return ids

    from robothor.memory.facts import store_facts_batch

    facts = [
        {
            "fact_text": item["fact_text"],
            "category": item.get("category", "event"),
            "entities": item.get("entities", []),
            "confidence": item.get("confidence", 1.0),
        }
        for item in case.seed
    ]
    ids = await store_facts_batch(
        facts, source_content="memory-eval seed", source_type="eval", tenant_id=tenant_id
    )
    _apply_seed_signals(ids, case.seed, tenant_id)
    return ids


async def _retrieve(case: EvalCase, tenant_id: str) -> list[str]:
    """Run the real retrieval path and return the ranked fact texts."""
    from robothor.memory.facts import search_facts

    results = await search_facts(case.query, limit=case.k, tenant_id=tenant_id)
    return [r.get("fact_text", "") for r in results]


async def run_case(case: EvalCase, tenant_id: str) -> CaseResult:
    """Seed → make distractors newest → retrieve → score one case.

    The refresh sits between seeding and retrieval deliberately. Doing it
    before `_seed_case` (the first version of this change) accomplishes
    nothing: the case's own facts are then written afterwards and are newest
    again, so `ORDER BY created_at DESC LIMIT k` still surfaces every target
    and the suite still cannot tell the retrieval stack from one line of SQL.
    Measured: with the refresh before seeding a null retriever scored 265/267;
    after moving it here it scores far lower.
    """
    await _seed_case(case, tenant_id)
    _refresh_distractor_recency(tenant_id)
    top = await _retrieve(case, tenant_id)
    return score_case(case, top)


async def run_suite(
    suite_path: str | Path,
    tenant_id: str = EVAL_TENANT,
    *,
    cleanup: bool = True,
) -> dict[str, Any]:
    """Run a whole suite end-to-end and return an aggregated report dict."""
    # Refuse the production tenant before touching disk or the database. The
    # older guard lived in _cleanup_tenant, which meant a mistyped --tenant
    # would happily seed fixture facts into the operator's real memory and
    # then merely decline to delete them.
    from robothor.constants import DEFAULT_TENANT

    if not tenant_id or tenant_id == DEFAULT_TENANT:
        raise EvalPreconditionError(
            f"refusing to seed eval fixtures into {tenant_id or DEFAULT_TENANT!r} "
            f"(the production tenant); pass an isolated tenant such as {EVAL_TENANT!r}"
        )

    meta, cases = load_suite(suite_path)
    _ensure_tenant(tenant_id)

    results: list[CaseResult] = []
    # An off-topic corpus, seeded once and preserved across the per-case wipe.
    # Without it every case's corpus is <= k rows, top-k returns all of them,
    # and the suite cannot tell the retrieval stack from one line of SQL.
    if cleanup:
        _cleanup_tenant(tenant_id)
    seeded = await _seed_distractors(tenant_id)
    logger.info("eval: seeded %d distractor fact(s)", seeded)
    try:
        for case in cases:
            # Per-case isolation: wipe the case's own seeds so an earlier case
            # can't contaminate a later one and make results order-dependent.
            # The distractors survive — they are the point.
            if cleanup:
                _cleanup_tenant(tenant_id, keep_distractors=True)
            # The recency refresh lives inside run_case, AFTER seeding — see
            # its docstring for why doing it here instead is a no-op.
            results.append(await run_case(case, tenant_id))  # noqa: PERF401 — await in body
    finally:
        if cleanup:
            _cleanup_tenant(tenant_id)

    by_kind: dict[str, dict[str, int]] = {}
    for r in results:
        agg = by_kind.setdefault(r.kind, {"passed": 0, "total": 0})
        agg["total"] += 1
        if r.passed:
            agg["passed"] += 1

    return {
        "suite_id": meta.get("id", ""),
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "by_kind": by_kind,
        "cases": [r.to_dict() for r in results],
    }
