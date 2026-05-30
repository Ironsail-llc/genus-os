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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EVAL_TENANT = "memory-eval"
# recall/persona/noise/resolution → gold must appear in top-k (noise adds a
# distractor cloud; resolution seeds a detection→resolution arc). temporal →
# latest must rank first. verbatim → exact string must survive.
VALID_KINDS = frozenset({"recall", "temporal", "verbatim", "persona", "noise", "resolution"})
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
    seed_mode: str = "direct"  # direct = store_fact; ingest = extraction path (llm)


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

    detail = "" if passed else f"gold not found in top-{case.k}"
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
                seed_mode=raw.get("seed_mode", "direct"),
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


def _cleanup_tenant(tenant_id: str) -> None:
    """Delete all seeded facts for the eval tenant. Never touches DEFAULT_TENANT."""
    from robothor.constants import DEFAULT_TENANT
    from robothor.db.connection import get_connection

    if tenant_id == DEFAULT_TENANT:
        logger.warning("refusing to cleanup memory_facts for DEFAULT_TENANT")
        return

    with get_connection() as conn:
        cur = conn.cursor()
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
    """Seed → retrieve → score one case."""
    await _seed_case(case, tenant_id)
    top = await _retrieve(case, tenant_id)
    return score_case(case, top)


async def run_suite(
    suite_path: str | Path,
    tenant_id: str = EVAL_TENANT,
    *,
    cleanup: bool = True,
) -> dict[str, Any]:
    """Run a whole suite end-to-end and return an aggregated report dict."""
    meta, cases = load_suite(suite_path)
    _ensure_tenant(tenant_id)

    results: list[CaseResult] = []
    try:
        for case in cases:
            # Per-case isolation: wipe the tenant before each case so an earlier
            # case's seeds (esp. noise distractors) can't contaminate a later one
            # and make results order-dependent. Disabled when cleanup=False (debug).
            if cleanup:
                _cleanup_tenant(tenant_id)
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
