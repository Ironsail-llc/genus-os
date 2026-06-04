"""Autonomy classifier for the Stage 4 forward planner.

Decides whether the planner can act on a proposed next action or must
surface a specific question to the operator. Evaluated in this order:

  1. hard_floor  — if the action_type appears in the budget's hard_floor
                   list, return "refuse" regardless of everything else.
  2. objective   — if the task's objective contains a veto phrase (e.g.
                   "without scheduling a meeting") that matches the
                   action_type, return "refuse". Objectives beat budgets.
  3. category    — if the budget maps this action_type explicitly (auto|
                   ask|refuse), that wins over the cost/reversibility gate.
  4. default     — irreversible → "ask"; reversible under cap → "auto";
                   reversible over cap → "ask".

Keep this module pure: no DB access, no LLM calls. The planner supplies
`metadata` (dict) with whatever context it has (reversible, estimated_cost_usd,
objective). Defaults live in brain/memory/autonomy_defaults.md per-instance.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

Verdict = Literal["auto", "ask", "refuse"]

# Patterns that veto specific action types regardless of numeric budget.
# Keyed by action_type. Matched case-insensitively against metadata["objective"].
_OBJECTIVE_VETOES: dict[str, list[str]] = {
    "calendar_send_new": [
        "without scheduling a meeting",
        "without a meeting",
        "no meeting",
        "async",
        "by email",
        "by email only",
        "answer by email",
    ],
    "calendar_reply_existing": [
        "without scheduling a meeting",
        "no meeting",
    ],
}


def _default_budget() -> dict[str, Any]:
    """Platform-level safe defaults. Tenants override via autonomy_defaults.md."""
    return {
        "reversible_cap_usd": 500,
        "irreversible_cap_usd": 0,
        "categories": {
            "vendor_data_ask": "auto",
            "reply_inbound_thread": "auto",
            "calendar_reply_existing": "auto",
            "calendar_send_new": "ask",
            "recurring_commitment": "ask",
            "contract_signature": "ask",
            "spend": "ask",
            "pushes_to_main": "ask",
            "deletes_data": "refuse",
        },
        "hard_floor": [
            "signs_contract",
            "pushes_to_main",
            "deletes_data",
            "sends_wire",
        ],
    }


def _objective_vetoes(action_type: str, objective: str) -> bool:
    vetoes = _OBJECTIVE_VETOES.get(action_type, [])
    low = objective.lower()
    return any(v in low for v in vetoes)


def classify_action(
    action_type: str,
    metadata: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
) -> Verdict:
    """Return "auto", "ask", or "refuse" for the given action.

    `action_type` — a short kebab-like string like "vendor_data_ask",
    "calendar_send_new", "pushes_to_main". Matches keys in
    budget["categories"] and budget["hard_floor"].

    `metadata` — optional context. Recognized keys:
        reversible (bool), estimated_cost_usd (float), objective (str).

    `budget` — if None, the platform default is used.
    """
    meta = metadata or {}
    b = budget or _default_budget()

    hard = b.get("hard_floor") or []
    if action_type in hard:
        return "refuse"

    objective = (meta.get("objective") or "").strip()
    if objective and _objective_vetoes(action_type, objective):
        return "refuse"

    categories = b.get("categories") or {}
    cat = categories.get(action_type)
    if cat in ("auto", "ask", "refuse"):
        return cat  # type: ignore[no-any-return]

    reversible = bool(meta.get("reversible", False))
    cost = float(meta.get("estimated_cost_usd") or 0.0)

    if not reversible:
        cap = float(b.get("irreversible_cap_usd") or 0)
        return "auto" if cost <= cap and cap > 0 else "ask"

    cap = float(b.get("reversible_cap_usd") or 0)
    return "auto" if cost <= cap else "ask"


# Allowed top-level keys in an autonomy_budget dict. Extra keys are typos, not
# features — the validator rejects them so the planner never silently degrades.
_VALID_BUDGET_KEYS = frozenset(
    {
        "reversible_cap_usd",
        "irreversible_cap_usd",
        "categories",
        "hard_floor",
    }
)

_VALID_VERDICTS = frozenset({"auto", "ask", "refuse"})


def validate_budget(budget: Any) -> tuple[bool, str]:
    """Validate an autonomy_budget dict before persisting to JSONB.

    Returns ``(True, "")`` for empty dicts and partial-but-recognized shapes —
    legacy rows must continue to round-trip cleanly. Returns ``(False, reason)``
    for clearly malformed inputs the DAL should reject with ``{"error": reason}``.

    Recognized shape:
        {
          "reversible_cap_usd":   <non-negative number>,
          "irreversible_cap_usd": <non-negative number>,
          "categories":           {<action_type>: "auto"|"ask"|"refuse"},
          "hard_floor":           [<action_type>, ...],
        }
    """
    if not isinstance(budget, dict):
        return False, "autonomy_budget must be a dict"

    extra = set(budget.keys()) - _VALID_BUDGET_KEYS
    if extra:
        return False, f"unknown autonomy_budget key(s): {sorted(extra)}"

    for cap_key in ("reversible_cap_usd", "irreversible_cap_usd"):
        if cap_key in budget:
            cap = budget[cap_key]
            if isinstance(cap, bool) or not isinstance(cap, (int, float)):
                return False, f"{cap_key} must be a non-negative number"
            if cap < 0:
                return False, f"{cap_key} must be non-negative (got {cap})"

    if "categories" in budget:
        cats = budget["categories"]
        if not isinstance(cats, dict):
            return False, "categories must be a dict of action_type → verdict"
        for action_type, verdict in cats.items():
            if not isinstance(action_type, str):
                return False, f"categories keys must be strings (got {type(action_type).__name__})"
            if verdict not in _VALID_VERDICTS:
                return (
                    False,
                    f"categories[{action_type!r}] must be one of {sorted(_VALID_VERDICTS)} (got {verdict!r})",
                )

    if "hard_floor" in budget:
        hf = budget["hard_floor"]
        if not isinstance(hf, list):
            return False, "hard_floor must be a list of action_type strings"
        for entry in hf:
            if not isinstance(entry, str):
                return False, f"hard_floor entries must be strings (got {type(entry).__name__})"

    return True, ""


def _parse_markdown_defaults(text: str) -> dict[str, Any]:
    """Parse the autonomy_defaults.md body into a budget dict.

    Format is intentionally loose — a fenced ```json block if present,
    otherwise sensible defaults. Tolerate missing files, malformed JSON,
    and empty bodies by returning platform defaults.
    """
    import json
    import re

    m = re.search(r"```json\s*\n(.*?)\n```", text, flags=re.DOTALL)
    if not m:
        return _default_budget()
    try:
        parsed = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.warning("autonomy_defaults.md has invalid JSON: %s", e)
        return _default_budget()
    default = _default_budget()
    default.update({k: v for k, v in parsed.items() if k in default})
    return default


def load_tenant_defaults(tenant_id: str) -> dict[str, Any]:
    """Read the instance's autonomy defaults. Returns platform defaults if missing."""
    workspace = os.environ.get("ROBOTHOR_WORKSPACE") or str(Path.home() / "robothor")
    path = Path(workspace) / "brain" / "memory" / "autonomy_defaults.md"
    if not path.exists():
        return _default_budget()
    try:
        return _parse_markdown_defaults(path.read_text())
    except Exception as e:
        logger.warning("Failed to load autonomy_defaults.md: %s", e)
        return _default_budget()
