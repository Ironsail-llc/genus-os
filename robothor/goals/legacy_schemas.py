"""Legacy session-goal schemas, retained separately from durable pursuits."""

from copy import deepcopy
from typing import Any

LEGACY_GOAL_SCHEMAS: dict[str, Any] = {
    "create_goal": {
        "type": "function",
        "function": {
            "name": "create_goal",
            "description": (
                "Create an active long-running session goal. Refuses to overwrite an "
                "existing active goal in the same scope. Workspace goals (no agent_id) "
                "auto-inject only into the main agent; agent-scoped goals inject only "
                "into the named agent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "objective": {
                        "type": "string",
                        "description": "Concrete objective the agent should keep pursuing.",
                    },
                    "success_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional explicit completion contract.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Optional target agent. Defaults to the current agent.",
                    },
                },
                "required": ["objective"],
            },
        },
    },
    "get_goal": {
        "type": "function",
        "function": {
            "name": "get_goal",
            "description": (
                "Return the active long-running session goal for the current scope, "
                "including objective, evidence count, and remaining completion "
                "requirements."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Optional target agent. Defaults to the current agent.",
                    },
                },
            },
        },
    },
    "update_goal": {
        "type": "function",
        "function": {
            "name": "update_goal",
            "description": (
                "Record typed evidence on a long-running session goal or mark it "
                "complete. Completion requires at least one validated 'test_run' AND "
                "one validated 'commit' evidence item. The reference field is verified "
                "per kind: pytest summary or UUID for test_run; git SHA validated via "
                "git cat-file for commit; https URL for ci_run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "complete"],
                        "description": "Set to complete only when the goal is truly finished.",
                    },
                    "edit_op": {
                        "type": "string",
                        "enum": ["objective", "criterion", "metric_target"],
                        "description": (
                            "Edit operation: 'objective' (with objective=<text>), "
                            "'criterion' (with text=<text>), or 'metric_target' "
                            "(with metric, target, optional weight/window_days/category)."
                        ),
                    },
                    "objective": {
                        "type": "string",
                        "description": "New objective text when edit_op='objective'.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Criterion text when edit_op='criterion'.",
                    },
                    "metric": {
                        "type": "string",
                        "description": (
                            "Metric name when edit_op='metric_target' (e.g. "
                            "benchmark_pass_rate, error_rate)."
                        ),
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "Target comparator when edit_op='metric_target' (e.g. '>=0.85')."
                        ),
                    },
                    "weight": {
                        "type": "number",
                        "description": "Goal weight (default 1.0).",
                    },
                    "window_days": {
                        "type": "integer",
                        "description": "Rolling window in days (default 7).",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["reach", "quality", "efficiency", "correctness"],
                        "description": "Category for metric_target (default 'correctness').",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["test_run", "commit", "ci_run", "note"],
                        "description": "Evidence kind. Only test_run + commit satisfy completion.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Short evidence summary.",
                    },
                    "reference": {
                        "type": "string",
                        "description": (
                            "Verifiable reference: pytest:passed:N or run UUID for "
                            "test_run; 7+ hex SHA for commit; https URL for ci_run."
                        ),
                    },
                    "completion_note": {
                        "type": "string",
                        "description": "Required when status is complete.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Optional target agent. Defaults to the current agent.",
                    },
                },
            },
        },
    },
}


def legacy_goal_schemas() -> dict[str, Any]:
    schemas = deepcopy(LEGACY_GOAL_SCHEMAS)
    for schema in schemas.values():
        schema["function"]["description"] = (
            "Legacy agent performance/session goal. For operator goals that execute "
            "continuously or wake over time use create_pursuit_goal/get_pursuit_goal/"
            "update_pursuit_goal instead. " + schema["function"]["description"]
        )
    return schemas
