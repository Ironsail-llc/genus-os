"""The corpus gate: a case that leaks its answer measures nothing.

Lexical overlap is what BM25 rewards, so a query echoing a span of its gold
fact is scored by the retriever's easiest path and tells you nothing about
semantic recall — the thing this overhaul is trying to improve.

The shipped 12-case suite is clean under these rules (measured: 0 leaks); its
12/12 is earned and the problem is n=12. But the fix for n=12 is generation,
and echo is a generator's default failure mode. These tests pin the gate so the
care that was implicit in a human author's judgement is explicit in code before
the corpus grows 12x.
"""

from __future__ import annotations

import pytest

from robothor.memory.eval_corpus import (
    MAX_TOKEN_JACCARD,
    NGRAM_N,
    CaseRejection,
    shares_ngram,
    stratum_coverage,
    token_jaccard,
    validate_case,
    validate_suite,
)


class TestSharedNgram:
    def test_verbatim_span_is_caught(self):
        assert shares_ngram("who manages the Helios project", "Alice manages the Helios project")

    def test_paraphrase_is_allowed(self):
        assert not shares_ngram("who owns Helios", "Alice manages the Helios project")

    def test_shorter_than_n_cannot_share(self):
        assert not shares_ngram("who", "Alice manages the Helios project")

    def test_case_and_punctuation_do_not_hide_a_leak(self):
        # "Alice manages the Helios" with different casing/punctuation is still
        # a verbatim leak; a naive check would pass it.
        assert shares_ngram(
            "Alice, manages the Helios!", "alice manages the helios project"
        )

    def test_n_is_four(self):
        assert NGRAM_N == 4


class TestTokenJaccard:
    def test_identical_is_one(self):
        assert token_jaccard("a b c", "a b c") == 1.0

    def test_disjoint_is_zero(self):
        assert token_jaccard("a b c", "x y z") == 0.0

    def test_empty_is_zero_not_a_crash(self):
        assert token_jaccard("", "a b") == 0.0

    def test_threshold_is_half(self):
        assert MAX_TOKEN_JACCARD == 0.5


class TestValidateCase:
    def _case(self, **over):
        base = {
            "id": "c1",
            "kind": "recall",
            "query": "who owns the flagship rollout",
            "gold": "Alice manages the Helios project",
            "seed": [{"fact_text": "Alice manages the Helios project", "category": "project"}],
        }
        base.update(over)
        return base

    def test_a_clean_case_passes(self):
        assert validate_case(self._case()) == []

    def test_ngram_leak_is_rejected(self):
        errs = validate_case(self._case(query="Alice manages the Helios project?"))
        assert any(e.reason == "ngram_leak" for e in errs)

    def test_high_jaccard_is_rejected(self):
        # No 4-gram shared, but a bag-of-words giveaway.
        errs = validate_case(self._case(query="Helios project Alice manages"))
        assert any(e.reason == "token_overlap" for e in errs)

    def test_missing_gold_is_rejected(self):
        errs = validate_case(self._case(gold=""))
        assert any(e.reason == "missing_gold" for e in errs)

    def test_empty_seed_is_rejected(self):
        # Found by smoke-testing the generator: an empty seed skipped the
        # reachability check entirely, so a case that seeds nothing — gold
        # unreachable, scores 0 forever, reads as a retrieval regression —
        # sailed through the gate built to catch exactly that.
        errs = validate_case(self._case(seed=[]))
        assert any(e.reason == "empty_seed" for e in errs)

    def test_gold_absent_from_seed_is_rejected(self):
        # The single most dangerous corpus bug: an unreachable gold scores 0
        # forever and reads as a retrieval regression.
        errs = validate_case(self._case(seed=[{"fact_text": "unrelated", "category": "x"}]))
        assert any(e.reason == "gold_not_seeded" for e in errs)

    def test_verbatim_without_gold_exact_is_rejected(self):
        # Third variant of the unreachable-gold bug. score_verbatim reads
        # gold_exact SPECIFICALLY and returns False when it is missing, so a
        # verbatim case carrying only `gold` scores 0 forever no matter how
        # perfectly retrieval performs — measured: all four such cases had the
        # right fact ranked FIRST. The generator had simply used the wrong field.
        errs = validate_case(self._case(kind="verbatim", gold="BM-7890", gold_exact=None,
                                        seed=[{"fact_text": "Bob's code is BM-7890"}]))
        assert any(e.reason == "missing_gold_exact" for e in errs)

    def test_verbatim_with_gold_exact_passes(self):
        assert validate_case(
            self._case(kind="verbatim", gold="BM-7890", gold_exact="BM-7890",
                       seed=[{"fact_text": "Bob's code is BM-7890"}])
        ) == []

    def test_unknown_kind_is_rejected(self):
        errs = validate_case(self._case(kind="vibes"))
        assert any(e.reason == "unknown_kind" for e in errs)

    def test_noise_cases_are_NOT_exempt_from_gold_rules(self):
        # This asserted the opposite until the expanded suite was actually run.
        # eval.score_case routes "noise" through _RECALL_KINDS, so a noise case
        # is a recall case whose seed holds distractors — the gold must still be
        # findable. The exemption let 25 generated gold-less noise cases through
        # the very gate meant to catch cases that can never score.
        c = {"id": "n1", "kind": "noise", "query": "what is the capital of France", "seed": []}
        assert any(e.reason == "missing_gold" for e in validate_case(c))

    def test_rejection_names_the_case(self):
        errs = validate_case(self._case(id="c-42", query="Alice manages the Helios project"))
        assert errs[0].case_id == "c-42"
        assert "c-42" in str(errs[0])


class TestValidateSuite:
    def test_duplicate_ids_are_rejected(self):
        cases = [
            {"id": "dup", "kind": "noise", "query": "q one", "seed": []},
            {"id": "dup", "kind": "noise", "query": "q two", "seed": []},
        ]
        errs = validate_suite(cases)
        assert any(e.reason == "duplicate_id" for e in errs)

    def test_near_identical_queries_across_cases_are_rejected(self):
        # Generated corpora collapse toward the same phrasing; 150 cases that
        # are 12 questions restated measures 12 questions.
        cases = [
            {"id": "a", "kind": "noise", "query": "what did the vendor invoice cover", "seed": []},
            {"id": "b", "kind": "noise", "query": "what did the vendor invoice cover?", "seed": []},
        ]
        errs = validate_suite(cases)
        assert any(e.reason == "duplicate_query" for e in errs)

    def test_clean_suite_passes(self):
        cases = [
            {"id": "a", "kind": "recall", "query": "which country runs the northern grid",
             "gold": "Iceland operates the northern power grid",
             "seed": [{"fact_text": "Iceland operates the northern power grid"}]},
            {"id": "b", "kind": "recall", "query": "how tall is that mountain in Nepal",
             "gold": "Everest rises 8,849 metres above sea level",
             "seed": [{"fact_text": "Everest rises 8,849 metres above sea level"}]},
        ]
        assert validate_suite(cases) == []


class TestStratumCoverage:
    def test_reports_counts_and_shortfall(self):
        cases = [{"id": str(i), "kind": "recall", "query": "q", "seed": []} for i in range(25)]
        cov = stratum_coverage(cases, min_per_stratum=25)
        assert cov["counts"]["recall"] == 25
        assert cov["gated"]["recall"] is True
        # A stratum with 3 cases cannot gate: 1 flip is a 33-point swing.
        cov2 = stratum_coverage(cases[:3], min_per_stratum=25)
        assert cov2["gated"]["recall"] is False
        assert "recall" in cov2["ungated"]


class TestShippedSuiteIsClean:
    """The gate applied to the corpus actually in the repo."""

    def test_shipped_suite_has_no_structural_errors(self):
        import yaml

        from robothor.memory.eval_corpus import suite_path

        cases = yaml.safe_load(suite_path().read_text())["cases"]
        errs = [e for e in validate_suite(cases) if e.reason in
                ("duplicate_id", "duplicate_query", "unknown_kind", "missing_gold",
                 "gold_not_seeded")]
        assert errs == [], f"shipped suite is malformed: {errs}"
