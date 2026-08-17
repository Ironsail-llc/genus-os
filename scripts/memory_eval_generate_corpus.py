#!/usr/bin/env python3
"""Generate stratified memory-eval cases with a different model family.

WHY A DIFFERENT FAMILY
    Fact extraction runs on a local qwen model. If the same family also writes
    the eval queries, the benchmark shares the extractor's vocabulary bias and
    scores the system on its own idiom — the retrieval equivalent of marking
    your own homework. This uses OpenRouter (MiMo) so the query distribution
    comes from somewhere else.

WHY EVERY CASE IS VALIDATED
    Asked for a query about a fact, a model restates the fact. robothor.memory
    .eval_corpus rejects shared 4-grams, >0.5 token Jaccard, unseeded golds and
    near-duplicate queries. Rejections are logged with their reason and
    regenerated, so the failure rate is visible rather than absorbed.

Appends to the existing suite; never rewrites it. Existing hand-written cases
are the control group and must survive expansion untouched.

    python scripts/memory_eval_generate_corpus.py --per-stratum 25 --out /tmp/new.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.memory.eval_corpus import (  # noqa: E402
    stratum_coverage,
    suite_path,
    validate_case,
    validate_suite,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("corpus-gen")

GENERATOR_MODEL = os.environ.get(
    "MEMORY_EVAL_GENERATOR_MODEL", "openrouter/xiaomi/mimo-v2.5"
)

# De-personalized cast per the platform rule — no instance data in a tracked
# corpus. The generator is told to stay inside it.
CAST = "Alice, Bob, Carol, Dave, FakeVendorCo, Helios, Meridian, Northwind"

# Measured, not guessed: batches of 30 and of 8 both came back EMPTY (the
# generator is a reasoning model and the thinking budget crowds out the answer);
# 4 with an 8k cap returned 4/4 valid cases on the first try. Small batches also
# give temperature more chances to diverge, which the duplicate-query check
# wants anyway.
BATCH_MAX = 4

STRATA: dict[str, str] = {
    "recall": (
        "A paraphrased question whose answer is one seeded fact. The query must "
        "NOT reuse wording from the fact — ask about the concept, not the words."
    ),
    "temporal": (
        "Two seeded facts about the same subject where the second SUPERSEDES the "
        "first (a changed time, owner, price, or status). gold is the CURRENT "
        "fact. The query asks for the current state without saying 'current'."
    ),
    "verbatim": (
        "A fact containing an exact string that must come back character-perfect "
        "(an ID, code, port, address, or version). Put that exact string in "
        "gold_exact. The query asks for it obliquely."
    ),
    "resolution": (
        "A query using a pronoun, role, or nickname that must resolve to a named "
        "entity in the seeded facts (e.g. 'the person who runs billing')."
    ),
    "persona": (
        "A stable preference or habit of a person, asked about in a later "
        "session with different phrasing than it was stated in."
    ),
    "noise": (
        "A question whose answer IS in memory but is buried among unrelated "
        "distractor facts. gold is the one relevant fact; seed it alongside 3-5 "
        "facts about entirely different subjects. This tests whether retrieval "
        "finds the signal, not whether it returns nothing."
    ),
}

PROMPT = """You write test cases for a memory-retrieval benchmark.

Stratum: {kind}
Definition: {definition}

Use ONLY these fictional names: {cast}. Never use real people or companies.

HARD RULES — a case that breaks one is discarded:
1. The query must NOT share any 4 consecutive words with its gold fact.
2. The query must NOT share more than half its words with its gold fact.
3. The gold fact MUST appear verbatim in the seed list.
4. Ask about the MEANING, never restate the sentence.

Bad  (restates the fact): fact "Alice manages the Helios project"
                          query "Who manages the Helios project?"
Good (asks the meaning):  query "Who should I escalate a Helios blocker to?"

Return a JSON array of exactly {n} objects, each:
{{"id": "{kind}-<slug>", "kind": "{kind}", "query": "...", "gold": "...",
  "seed": [{{"fact_text": "...", "category": "...", "entities": ["..."]}}]}}

Seed 2-4 facts per case; distractors make it a real test. For "verbatim" add
"gold_exact" AND set it to the exact string itself (e.g. "BM-7890", not a
sentence) — verbatim cases are scored on gold_exact alone and a case without it
can never pass. EVERY case needs a gold that appears verbatim in its own seed.
Return ONLY the JSON array."""


async def _generate(kind: str, n: int, attempt: int) -> list[dict[str, Any]]:
    from robothor.engine.llm_client import llm_call

    prompt = PROMPT.format(kind=kind, definition=STRATA[kind], cast=CAST, n=n)
    if attempt > 1:
        prompt += (
            f"\n\nAttempt {attempt}: previous cases were rejected for restating "
            "the fact. Push the query further from the wording."
        )
    resp = await llm_call(
        [
            {
                "role": "system",
                "content": "You produce strictly valid JSON arrays. No prose, no fences.",
            },
            {"role": "user", "content": prompt},
        ],
        model=GENERATOR_MODEL,
        # Variety matters more than determinism here; near-duplicate queries
        # are rejected downstream anyway.
        temperature=0.9,
        timeout=300,
        max_tokens=8000,
    )
    text = resp.choices[0].message.content or ""
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        raise ValueError(f"no JSON array in response: {text[:200]}")
    parsed = json.loads(text[start : end + 1])
    return [c for c in parsed if isinstance(c, dict)]


async def build(per_stratum: int, max_attempts: int) -> tuple[list[dict], Counter]:
    accepted: list[dict[str, Any]] = []
    rejected: Counter = Counter()
    existing = yaml.safe_load(suite_path().read_text())["cases"]
    seen_ids = {str(c.get("id")) for c in existing}

    for kind in STRATA:
        got: list[dict[str, Any]] = []
        for attempt in range(1, max_attempts + 1):
            need = per_stratum - len(got)
            if need <= 0:
                break
            try:
                batch = await _generate(kind, min(need + 2, BATCH_MAX), attempt)
            except Exception as exc:
                log.warning("%s attempt %d failed: %s", kind, attempt, exc)
                continue
            for case in batch:
                case["kind"] = kind
                cid = str(case.get("id") or "")
                if not cid or cid in seen_ids:
                    rejected["duplicate_id"] += 1
                    continue
                errs = validate_case(case)
                if errs:
                    for e in errs:
                        rejected[e.reason] += 1
                    continue
                # Cross-check against everything accepted so far, not just this
                # batch — otherwise strata silently converge on one phrasing.
                if validate_suite([*existing, *accepted, *got, case]):
                    rejected["duplicate_query"] += 1
                    continue
                seen_ids.add(cid)
                got.append(case)
                if len(got) >= per_stratum:
                    break
            log.info("%s: %d/%d after attempt %d", kind, len(got), per_stratum, attempt)
        accepted.extend(got)
        if len(got) < per_stratum:
            log.warning("%s: SHORT — %d of %d requested", kind, len(got), per_stratum)
    return accepted, rejected


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-stratum", type=int, default=25)
    ap.add_argument("--max-attempts", type=int, default=12)
    ap.add_argument("--out", default="")
    ap.add_argument("--apply", action="store_true", help="append into the tracked suite")
    args = ap.parse_args()

    accepted, rejected = await build(args.per_stratum, args.max_attempts)

    log.info("accepted %d cases", len(accepted))
    log.info("rejected: %s", dict(rejected))
    total_seen = len(accepted) + sum(rejected.values())
    if total_seen:
        log.info("rejection rate %.0f%% — this is the number to watch",
                 100 * sum(rejected.values()) / total_seen)

    existing = yaml.safe_load(suite_path().read_text())["cases"]
    merged = [*existing, *accepted]
    errs = validate_suite(merged)
    log.info("merged suite: %d cases, %d rejections", len(merged), len(errs))
    for e in errs[:10]:
        log.error("  %s", e)
    log.info("coverage: %s", stratum_coverage(merged))

    if errs:
        log.error("REFUSING to write an invalid suite")
        return 1

    out = Path(args.out) if args.out else None
    if args.apply:
        suite = yaml.safe_load(suite_path().read_text())
        suite["cases"] = merged
        suite_path().write_text(yaml.safe_dump(suite, sort_keys=False, width=100))
        log.info("appended into %s", suite_path())
    elif out:
        out.write_text(yaml.safe_dump({"cases": accepted}, sort_keys=False, width=100))
        log.info("wrote %s (not applied)", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
