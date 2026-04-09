"""Interpret Managed Agents outcome evaluation results.

Provides a standalone mapping from MA outcome events to a simple
pass/fail assessment.  Does not import from or modify ``verifier.py``.
"""

from __future__ import annotations

from typing import Any

_CONFIDENCE_MAP: dict[str, float] = {
    "satisfied": 1.0,
    "needs_revision": 0.5,
    "max_iterations_reached": 0.3,
    "failed": 0.0,
    "interrupted": 0.0,
}


def interpret_outcome(outcome_event: dict[str, Any]) -> dict[str, Any]:
    """Convert an MA ``span.outcome_evaluation_end`` event to a result dict.

    Returns
    -------
    dict
        ``passed`` (bool), ``confidence`` (float 0-1), ``result`` (str),
        ``explanation`` (str), ``iteration`` (int).
    """
    result = outcome_event.get("result", "failed")
    return {
        "passed": result == "satisfied",
        "confidence": _CONFIDENCE_MAP.get(result, 0.0),
        "result": result,
        "explanation": outcome_event.get("explanation", ""),
        "iteration": outcome_event.get("iteration", 0),
    }


def build_outcome_event(
    description: str,
    rubric: str,
    *,
    max_iterations: int = 5,
) -> dict[str, Any]:
    """Build a ``user.define_outcome`` event payload.

    Parameters
    ----------
    description
        What the agent should produce.
    rubric
        Markdown rubric for the grader.
    max_iterations
        Max evaluation cycles (1-20, default 5).
    """
    return {
        "type": "user.define_outcome",
        "description": description,
        "rubric": {"type": "text", "content": rubric},
        "max_iterations": max(1, min(20, max_iterations)),
    }
