"""Outcome utility: how much a memory has helped the runs that recalled it.

Pure functions only — no database access. The writers live in
``robothor.memory.outcomes`` (counts, at run end) and the reader in
``robothor.memory.lifecycle`` (the nightly decay pass turns the counts into the
``memory_facts.utility`` scalar that decay and ranking read).

Evidence per fact, counted once per run whose recall returned the fact:

* ``outcome_successes`` — the run completed without failure (a weak credit).
* ``outcome_citations`` — the run's final answer contains a distinctive value
  of the fact (``is_cited``) that the task did not already state (a strong one).
* ``outcome_failures`` — the run failed (a penalty, as before).

Being returned is not evidence. A fact recalled in a thousand runs that neither
succeeded nor failed has the same utility as one never recalled.

The function::

    support = min(SUCCESS_CREDIT * successes, WEAK_SUPPORT_CAP) + CITED_CREDIT * citations
    against = FAILURE_WEIGHT * failures
    utility = (support - against) / (support + against + PRIOR_STRENGTH)

is a smoothed success rate with a neutral prior: 0 with no evidence, in the
open interval (-1, 1), non-decreasing in successes (saturating at
``WEAK_UTILITY_CEILING`` from successes alone), strictly increasing in
citations and strictly decreasing in failures. Every step adds less than the
last.
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

#: Support from one run that completed without failure.
SUCCESS_CREDIT = 0.25
#: Most support completed runs can give on their own (4 runs' worth), so the
#: weak credit saturates however often a fact rides along in good runs.
WEAK_SUPPORT_CAP = 1.0
#: Support from one run whose answer used the fact.
CITED_CREDIT = 1.0
#: Weight of one failed run.
FAILURE_WEIGHT = 1.0

#: Highest utility successes alone can reach.
WEAK_UTILITY_CEILING = WEAK_SUPPORT_CAP / (WEAK_SUPPORT_CAP + PRIOR_STRENGTH)


def compute_utility(successes: int, citations: int, failures: int) -> float:
    """Utility in (-1, 1) from a fact's outcome counts. Negative counts are 0."""
    s = max(int(successes or 0), 0)
    c = max(int(citations or 0), 0)
    f = max(int(failures or 0), 0)
    support = min(SUCCESS_CREDIT * s, WEAK_SUPPORT_CAP) + CITED_CREDIT * c
    against = FAILURE_WEIGHT * f
    return (support - against) / (support + against + PRIOR_STRENGTH)


# ── Utility in the decay score ─────────────────────────────────────────────

#: Most a fact's decay score can rise from utility (at utility 1).
MAX_UTILITY_BOOST = 0.3
#: Most a fact's decay score can fall from failures. The failure-only penalty
#: this term replaces had the same cap, so a streak of misattributed failures
#: cannot destroy a fact outright.
MAX_FAILURE_PENALTY = 0.4


def utility_decay_term(utility: float) -> float:
    """The signed, bounded amount utility adds to a decay score."""
    u = max(UTILITY_MIN, min(UTILITY_MAX, float(utility)))
    if u >= 0.0:
        return MAX_UTILITY_BOOST * u
    return MAX_FAILURE_PENALTY * u


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

    An ineligible fact keeps its failures and ignores its successes, so it can
    fall but never rise above neutral.
    """
    failures = row.get("outcome_failures") or 0
    if not earns_positive_utility(row):
        return compute_utility(0, 0, failures)
    return compute_utility(
        row.get("outcome_successes") or 0, row.get("outcome_citations") or 0, failures
    )


# ── "Demonstrably used": a distinctive value of the fact in the answer ─────
#
# Deterministic and cheap: a fact counts as used by a run when one of its
# distinctive values — an email address, a URL, or a token carrying digits
# (an invoice or ticket number, a date, a version, an amount of 3+ digits) —
# appears as a whole token in the run's final output and was not already in
# the task text. Ordinary words never count: two texts on one topic share
# words without one having used the other. Bare years are excluded; they are
# everywhere.

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]*[A-Za-z0-9]")
_YEAR_RE = re.compile(r"(?:19|20)\d\d")
_TRAILING = ".,;:!?"
_MIN_DIGITS_NUMERIC = 3
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
        if not digits or _YEAR_RE.fullmatch(tok):
            continue
        letters = any(ch.isalpha() for ch in tok)
        long_enough = (letters and len(tok) >= _MIN_LEN_MIXED) or digits >= _MIN_DIGITS_NUMERIC
        if long_enough and "://" not in tok:
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
