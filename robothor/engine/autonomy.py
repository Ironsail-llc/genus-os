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
        return cat  # type: ignore[return-value]

    reversible = bool(meta.get("reversible", False))
    cost = float(meta.get("estimated_cost_usd") or 0.0)

    if not reversible:
        cap = float(b.get("irreversible_cap_usd") or 0)
        return "auto" if cost <= cap and cap > 0 else "ask"

    cap = float(b.get("reversible_cap_usd") or 0)
    return "auto" if cost <= cap else "ask"


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
