"""The structured output a review job must return, and a checker for it.

``REVIEW_OUTPUT_SCHEMA`` is handed to Claude Code as ``--json-schema``; the
pr-review skill ships the same document as ``agents/skills/pr-review/schema.json``
(a test pins the two together). :func:`validate_review_output` re-checks the
result in code before anything is posted: Claude Code's own validation is a
second opinion, not the gate.
"""

from __future__ import annotations

import copy
from typing import Any

__all__ = ["REVIEW_OUTPUT_SCHEMA", "SEVERITIES", "review_output_schema", "validate_review_output"]

SEVERITIES: tuple[str, ...] = ("blocker", "major", "minor", "nit")
_VERDICTS = ("APPROVE", "COMMENT", "REQUEST_CHANGES")
_PRIOR_STATUSES = ("resolved", "partially_resolved", "unresolved")

REVIEW_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "issues", "prior_issues"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": list(_VERDICTS),
            "description": (
                "Your proposed verdict. The service recomputes it from the issues: any "
                "blocker or major finding means it is never posted as APPROVE."
            ),
        },
        "summary": {
            "type": "string",
            "description": (
                "The top of the review body in GitHub markdown: what the change does and "
                "what you checked. Do not repeat the issues here."
            ),
        },
        "issues": {
            "type": "array",
            "description": "Every finding of this review, nits included. Empty if none.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "line", "start_line", "side", "severity", "title", "body"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "File path relative to the repository root, as shown in the "
                            "diff. Empty string for a PR-wide finding."
                        ),
                    },
                    "line": {
                        "type": ["integer", "null"],
                        "description": (
                            "Anchor line: new-file numbering for RIGHT, old-file for LEFT. "
                            "null when the finding is not tied to a changed line."
                        ),
                    },
                    "start_line": {
                        "type": ["integer", "null"],
                        "description": "First line of a multi-line range, or null.",
                    },
                    "side": {"type": "string", "enum": ["RIGHT", "LEFT"]},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "title": {"type": "string", "description": "One-line summary."},
                    "body": {
                        "type": "string",
                        "description": "The full comment in GitHub markdown, with how it was verified.",
                    },
                },
            },
        },
        "prior_issues": {
            "type": "array",
            "description": (
                "Re-reviews only: the status of each finding from the previous review. "
                "Empty for an initial review."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["comment_id", "description", "status", "note"],
                "properties": {
                    "comment_id": {
                        "type": ["integer", "null"],
                        "description": "The previous finding's comment_id as given, or null.",
                    },
                    "description": {"type": "string"},
                    "status": {"type": "string", "enum": list(_PRIOR_STATUSES)},
                    "note": {
                        "type": "string",
                        "description": "How it was (or was not) addressed.",
                    },
                },
            },
        },
    },
}


def review_output_schema() -> dict[str, Any]:
    """A copy of the schema, safe to hand to a caller that may mutate it."""
    return copy.deepcopy(REVIEW_OUTPUT_SCHEMA)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_object(obj: Any, spec: dict[str, Any], where: str, errors: list[str]) -> None:
    if not isinstance(obj, dict):
        errors.append(f"{where} must be an object")
        return
    props: dict[str, Any] = spec["properties"]
    errors.extend(
        f"{where}.{key} is required" for key in spec.get("required", []) if key not in obj
    )
    for key, value in obj.items():
        if key not in props:
            errors.append(f"{where}.{key} is not allowed")
            continue
        _check_value(value, props[key], f"{where}.{key}", errors)


def _check_value(value: Any, spec: dict[str, Any], where: str, errors: list[str]) -> None:
    kinds = spec.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    ok = False
    for kind in kinds:
        if (
            (kind == "string" and isinstance(value, str))
            or (kind == "integer" and _is_int(value))
            or (kind == "null" and value is None)
            or (kind == "array" and isinstance(value, list))
            or (kind == "object" and isinstance(value, dict))
        ):
            ok = True
            break
    if not ok:
        errors.append(f"{where} must be {' or '.join(str(k) for k in kinds)}")
        return
    if "enum" in spec and value not in spec["enum"]:
        errors.append(f"{where} must be one of {spec['enum']}, got {value!r}")
    if isinstance(value, list) and "items" in spec:
        for i, item in enumerate(value):
            _check_object(item, spec["items"], f"{where}[{i}]", errors)


def validate_review_output(output: Any) -> list[str]:
    """Every way ``output`` departs from :data:`REVIEW_OUTPUT_SCHEMA`; empty when valid."""
    errors: list[str] = []
    _check_object(output, REVIEW_OUTPUT_SCHEMA, "output", errors)
    return errors
