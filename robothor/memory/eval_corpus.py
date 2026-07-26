"""Corpus validation for the memory eval suite.

WHY THIS EXISTS

A retrieval benchmark is only as honest as the distance between its queries and
its gold facts. BM25 rewards lexical overlap directly, so a query that echoes a
span of its gold fact is graded on the retriever's easiest path and says nothing
about the semantic recall this overhaul is trying to improve.

Measured before writing this: the hand-written 12-case suite has **zero**
leaking cases under these rules — "Who is responsible for the Helios project?"
against "Alice manages the Helios project" shares no 4-gram and scores 0.33
Jaccard. Its 12/12 is earned. The problem is n=12, not contamination.

The gate exists because the fix for n=12 is generation, and echo is a
generator's default failure mode: asked for a query about a fact, a model
reliably restates the fact. Hand-written care does not survive being scaled up
150x, so the check that was implicit in the author's judgement has to become
explicit in code before the corpus grows.

WHAT IT ENFORCES

  ngram_leak      no shared 4-gram between query and gold (verbatim span)
  token_overlap   token Jaccard <= 0.5 (bag-of-words giveaway)
  gold_not_seeded the gold fact must appear in the case's own seed
  duplicate_query no two cases asking the same thing in different words

The third is the most dangerous corpus bug and the least obvious: an
unreachable gold scores zero forever and reads as a retrieval regression, so it
gets "fixed" by changing the retriever.

Paths and tenant come from the environment (CLAUDE.md rules 1-2); the harness
this replaces hardcoded a home directory and a tenant id.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robothor.memory.eval import VALID_KINDS

# A shared 4-gram is a verbatim span, not a coincidence. 3 fires on ordinary
# phrases ("one of the most"); 5 lets short leaks through.
NGRAM_N = 4

# Above half the tokens in common, the query is a restatement of the gold even
# with no contiguous span shared.
MAX_TOKEN_JACCARD = 0.5

# Below this a stratum cannot gate anything: at n=3 a single flip is a 33-point
# swing, which is noise wearing a regression's clothes.
MIN_PER_STRATUM = 25

# Kinds that assert an *absence* (nothing relevant should return), so they have
# no gold and nothing to leak.
_NO_GOLD_KINDS = frozenset({"noise"})

_WORD = re.compile(r"[a-z0-9]+")


def suite_path() -> Path:
    """Canonical suite location, workspace-relative."""
    workspace = os.environ.get("ROBOTHOR_WORKSPACE") or str(Path.home() / "robothor")
    return Path(workspace) / "docs" / "benchmarks" / "memory" / "suite.yaml"


def _tokens(text: str) -> list[str]:
    """Lowercased word tokens. Casing and punctuation must not hide a leak."""
    return _WORD.findall((text or "").lower())


def shares_ngram(a: str, b: str, n: int = NGRAM_N) -> bool:
    """True when the two strings share any contiguous n-token span."""
    ta, tb = _tokens(a), _tokens(b)
    if len(ta) < n or len(tb) < n:
        return False
    grams_a = {tuple(ta[i : i + n]) for i in range(len(ta) - n + 1)}
    return any(tuple(tb[i : i + n]) in grams_a for i in range(len(tb) - n + 1))


def token_jaccard(a: str, b: str) -> float:
    """Bag-of-words overlap, 0.0 when either side is empty."""
    sa, sb = set(_tokens(a)), set(_tokens(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


@dataclass(frozen=True)
class CaseRejection:
    """One reason one case may not enter the corpus."""

    case_id: str
    reason: str
    detail: str

    def __str__(self) -> str:
        return f"{self.case_id}: {self.reason} — {self.detail}"


def _seed_texts(case: dict[str, Any]) -> list[str]:
    return [
        (s or {}).get("fact_text", "")
        for s in (case.get("seed") or [])
        if isinstance(s, dict)
    ]


def validate_case(case: dict[str, Any]) -> list[CaseRejection]:
    """Structural and leakage checks for one case. Empty list means it passes."""
    cid = str(case.get("id") or "?")
    out: list[CaseRejection] = []

    kind = case.get("kind") or ""
    if kind not in VALID_KINDS:
        out.append(CaseRejection(cid, "unknown_kind", f"{kind!r} not in {sorted(VALID_KINDS)}"))

    query = case.get("query") or ""
    if not query.strip():
        out.append(CaseRejection(cid, "missing_query", "empty query"))

    if kind in _NO_GOLD_KINDS:
        return out

    gold = case.get("gold") or case.get("gold_exact") or ""
    if not gold.strip():
        out.append(CaseRejection(cid, "missing_gold", "no gold or gold_exact"))
        return out

    if shares_ngram(query, gold):
        out.append(
            CaseRejection(cid, "ngram_leak", f"query shares a {NGRAM_N}-gram with its gold")
        )

    jac = token_jaccard(query, gold)
    if jac > MAX_TOKEN_JACCARD:
        out.append(
            CaseRejection(cid, "token_overlap", f"token jaccard {jac:.2f} > {MAX_TOKEN_JACCARD}")
        )

    # An unreachable gold scores zero forever and reads as a retrieval bug.
    # An EMPTY seed is the same bug wearing a different hat, and the first
    # version of this check skipped it — `if seeds and ...` treated "seeded
    # nothing" as "nothing to check". Caught by smoke-testing the generator.
    seeds = _seed_texts(case)
    if not seeds:
        out.append(CaseRejection(cid, "empty_seed", "case seeds no facts, so gold is unreachable"))
    elif not any(gold.lower() in s.lower() or s.lower() in gold.lower() for s in seeds):
        out.append(
            CaseRejection(cid, "gold_not_seeded", "gold does not appear in this case's seed")
        )

    return out


def validate_suite(
    cases: list[dict[str, Any]], *, cross_case_jaccard: float = 0.8
) -> list[CaseRejection]:
    """Per-case checks plus the cross-case ones a single case cannot see.

    Generated corpora collapse toward the same phrasing, so 150 cases can be 12
    questions restated. ``duplicate_query`` is what stops the count from
    outrunning the coverage.
    """
    out: list[CaseRejection] = []
    for case in cases:
        out.extend(validate_case(case))

    seen_ids: set[str] = set()
    for case in cases:
        cid = str(case.get("id") or "?")
        if cid in seen_ids:
            out.append(CaseRejection(cid, "duplicate_id", "case id already used"))
        seen_ids.add(cid)

    for i, a in enumerate(cases):
        for b in cases[i + 1 :]:
            qa, qb = a.get("query") or "", b.get("query") or ""
            if qa and qb and token_jaccard(qa, qb) >= cross_case_jaccard:
                out.append(
                    CaseRejection(
                        str(b.get("id") or "?"),
                        "duplicate_query",
                        f"near-identical to case {a.get('id')!r}",
                    )
                )
    return out


def stratum_coverage(
    cases: list[dict[str, Any]], *, min_per_stratum: int = MIN_PER_STRATUM
) -> dict[str, Any]:
    """Which strata have enough cases to gate a promotion decision.

    Reported rather than enforced: an under-powered stratum should still be
    measured, it just must not be allowed to block or bless a change on its own.
    """
    counts = Counter(str(c.get("kind") or "?") for c in cases)
    gated = {k: v >= min_per_stratum for k, v in counts.items()}
    return {
        "counts": dict(counts),
        "gated": gated,
        "ungated": sorted(k for k, ok in gated.items() if not ok),
        "min_per_stratum": min_per_stratum,
        "total": len(cases),
    }
