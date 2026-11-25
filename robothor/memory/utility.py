"""Outcome utility: how much a memory has helped the runs that recalled it.

Pure functions only — no database access. The writers live in
``robothor.memory.outcomes`` (counts, at run end) and the reader in
``robothor.memory.lifecycle`` (the nightly decay pass turns the counts into the
``memory_facts.utility`` scalar that decay and ranking read).

Evidence per fact, counted once per run whose recall returned the fact:

* ``outcome_citations`` — the run completed without failure AND its final
  answer contains a distinctive value of the fact (``is_cited``) that the task
  did not already state: evidence the fact was used.
* ``outcome_failures`` — the run failed (a penalty, as before).
* ``outcome_successes`` — the run completed without failure. Recorded, but NOT
  credit: every fact a search returns rides along in a successful run whether
  or not the answer used it, so counting it would let a fact rise by being
  returned (GEN-873 criterion 3). It neither raises utility nor offsets
  failures.

Being returned is not evidence. A fact recalled in a thousand runs, successful
or not, that never used it has the same utility as one never recalled.

The function::

    utility = (CITED_CREDIT * citations - FAILURE_WEIGHT * failures)
              / (CITED_CREDIT * citations + FAILURE_WEIGHT * failures + PRIOR_STRENGTH)

is a smoothed use rate with a neutral prior: 0 with no evidence, in the open
interval (-1, 1), strictly increasing in citations and strictly decreasing in
failures, and every step moves it less than the last.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

# ── The utility function ────────────────────────────────────────────────────

#: Utility with no evidence: neither helpful nor harmful.
UTILITY_PRIOR = 0.0
UTILITY_MIN = -1.0
UTILITY_MAX = 1.0

#: Pseudo-observations of the neutral prior. Larger means more evidence is
#: needed to move a fact away from 0.
PRIOR_STRENGTH = 2.0

#: Support from one run whose answer used the fact.
CITED_CREDIT = 1.0
#: Weight of one failed run.
FAILURE_WEIGHT = 1.0


def compute_utility(*, citations: int, failures: int) -> float:
    """Utility in (-1, 1) from a fact's use and failure counts. Negatives are 0."""
    support = CITED_CREDIT * max(int(citations or 0), 0)
    against = FAILURE_WEIGHT * max(int(failures or 0), 0)
    return (support - against) / (support + against + PRIOR_STRENGTH)


# ── Utility in the decay score ─────────────────────────────────────────────

#: Most a fact's decay score can rise from utility (at utility 1).
MAX_UTILITY_BOOST = 0.3
#: Most a fact's decay score can fall from failures. The failure-only penalty
#: this term replaces had the same cap, so a streak of misattributed failures
#: cannot destroy a fact outright.
MAX_FAILURE_PENALTY = 0.4
#: Decay penalty per unit of negative utility, before the cap. With no
#: citations, f failures give utility -f/(f+2), so 0.6 keeps the penalty at or
#: above the old linear 0.1 per failure and reaches the 0.4 cap at 4 failures,
#: as the old one did.
FAILURE_PENALTY_SLOPE = 0.6


def utility_decay_term(utility: float) -> float:
    """The signed, bounded amount utility adds to a decay score."""
    u = max(UTILITY_MIN, min(UTILITY_MAX, float(utility)))
    if u >= 0.0:
        return MAX_UTILITY_BOOST * u
    return max(-MAX_FAILURE_PENALTY, FAILURE_PENALTY_SLOPE * u)


# ── Trust: lower authority earns no reinforcement ───────────────────────────

#: Origins that never earn positive utility (design: lower authority cannot
#: be reinforced). Values match the graph schema's origin vocabulary.
NO_REINFORCEMENT_ORIGINS = frozenset({"external", "inferred"})


def earns_positive_utility(row: Mapping[str, Any]) -> bool:
    """Whether a fact may gain utility, by its origin.

    ``memory_facts.origin`` arrives with the graph schema. A row without the
    key (the column does not exist yet) or with a NULL origin predates origins
    and stays eligible, as it was before this rule.
    """
    origin = row.get("origin")
    if origin is None:
        return True
    return str(origin).strip().lower() not in NO_REINFORCEMENT_ORIGINS


def utility_for_row(row: Mapping[str, Any]) -> float:
    """A fact row's utility, applying the origin rule.

    An ineligible fact keeps its failures and ignores its citations, so it can
    fall but never rise above neutral. ``outcome_successes`` is not read: see
    the module docstring.
    """
    failures = row.get("outcome_failures") or 0
    citations = (row.get("outcome_citations") or 0) if earns_positive_utility(row) else 0
    return compute_utility(citations=citations, failures=failures)


# ── "Demonstrably used": a distinctive value of the fact in the answer ─────
#
# Deterministic and cheap: a fact counts as used by a run when one of its
# distinctive values appears as a whole token in the run's final output and was
# not already in the task text. Distinctive means:
#   * an email address or a URL;
#   * an identifier mixing letters and digits, 4+ characters (INV-20417,
#     PO-77120, v1.4.2) — but not a number with a unit or ordinal (120kg,
#     21st) or a clock time (10am, 9:30pm);
#   * a bare number of 5+ digits (an account or order number).
# Never distinctive: ordinary words (two texts on one topic share words without
# one having used the other), short numbers (120, 1000 — counts and amounts
# recur everywhere), clock times and calendar dates (9:30, 11/25, 2026-11-24 —
# scheduled digests print today's), and bare years.
#
# Known gap: values the run read from a tool result rather than from memory
# also match; only the task text is excluded. Excluding tool results would mean
# reading agent_run_steps at delivery, which is written asynchronously.

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]*[A-Za-z0-9]")
_YEAR_RE = re.compile(r"(?:19|20)\d\d")
_DATE_OR_TIME_RE = re.compile(r"\d{1,4}([:/.-])\d{1,2}(?:\1\d{1,4})?")
_NUMBER_WITH_SUFFIX_RE = re.compile(r"\d+(?:[:.]\d+)?[a-z]{1,3}", re.IGNORECASE)
_TRAILING = ".,;:!?"
_MIN_DIGITS_NUMERIC = 5
_MIN_LEN_MIXED = 4


def _strip_url(url: str) -> str:
    u = re.sub(r"^(?:https?://)?(?:www\.)?", "", url, flags=re.IGNORECASE)
    return u.rstrip(_TRAILING + "/").lower()


def distinctive_tokens(text: str | None) -> set[str]:
    """The values in ``text`` specific enough that echoing one is evidence."""
    if not text:
        return set()
    out: set[str] = set()
    for m in _EMAIL_RE.finditer(text):
        out.add(m.group(0).rstrip(_TRAILING).lower())
    for m in _URL_RE.finditer(text):
        stripped = _strip_url(m.group(0))
        if stripped:
            out.add(stripped)
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0).rstrip(_TRAILING)
        digits = sum(ch.isdigit() for ch in tok)
        if not digits or "://" in tok or _YEAR_RE.fullmatch(tok):
            continue
        if _DATE_OR_TIME_RE.fullmatch(tok) or _NUMBER_WITH_SUFFIX_RE.fullmatch(tok):
            continue
        letters = any(ch.isalpha() for ch in tok)
        if (letters and len(tok) >= _MIN_LEN_MIXED) or digits >= _MIN_DIGITS_NUMERIC:
            out.add(tok.lower())
    return out


def _contains_token(haystack: str, token: str) -> bool:
    pattern = r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])"
    return re.search(pattern, haystack) is not None


def is_cited(fact_text: str | None, output_text: str | None, task_text: str | None) -> bool:
    """Whether a run's final output used ``fact_text``, per the rule above."""
    if not output_text:
        return False
    output = output_text.lower()
    task = (task_text or "").lower()
    for token in distinctive_tokens(fact_text):
        if task and _contains_token(task, token):
            continue
        if _contains_token(output, token):
            return True
    return False


# ── Edge utility seam ───────────────────────────────────────────────────────
#
# Not wired in this change: ``memory_relations.weight`` arrives with the graph
# schema. At integration the graph lane calls ``apply_edge_utility`` with the
# weights of the edges a fact supports and the fact's utility before and after
# the nightly pass, and writes the result back.

#: Weight change per unit of utility change.
EDGE_UTILITY_GAIN = 0.2
#: Largest weight change one pass can make.
MAX_EDGE_WEIGHT_DELTA = 0.2
#: Weight range an edge is kept in (the schema's default weight is 1.0).
EDGE_WEIGHT_MIN = 0.1
EDGE_WEIGHT_MAX = 2.0


def edge_weight_delta(utility_before: float, utility_after: float) -> float:
    """Bounded weight change for an edge whose supporting fact's utility moved."""
    delta = EDGE_UTILITY_GAIN * (float(utility_after) - float(utility_before))
    return max(-MAX_EDGE_WEIGHT_DELTA, min(MAX_EDGE_WEIGHT_DELTA, delta))


def apply_edge_utility(
    weights: Mapping[Any, float], *, utility_before: float, utility_after: float
) -> dict[Any, float]:
    """New weights for a fact's supporting edges. Pure: ``weights`` is not changed."""
    delta = edge_weight_delta(utility_before, utility_after)
    return {
        edge: max(EDGE_WEIGHT_MIN, min(EDGE_WEIGHT_MAX, float(w) + delta))
        for edge, w in weights.items()
    }
