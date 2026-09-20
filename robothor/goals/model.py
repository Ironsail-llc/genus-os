"""Goal contracts and deterministic lifecycle transitions (no I/O)."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TERMINAL = {"complete", "canceled"}
INACTIVE = TERMINAL | {"paused", "blocked", "review"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def future(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


class CreateGoal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1, max_length=12000)
    success_criteria: list[str] = Field(min_length=1, max_length=50)
    kind: Literal["short", "long"] = "short"
    mode: Literal["finite", "ongoing"] = "finite"
    parent_goal_id: str | None = None
    request_key: str | None = Field(default=None, max_length=200)
    token_budget: int | None = Field(default=None, gt=0)
    review_seconds: int = Field(default=86400, ge=60)
    human_review: bool = False
    priority: int = Field(default=0, ge=0, le=10)

    @field_validator("parent_goal_id")
    @classmethod
    def parent_uuid(cls, value: str | None) -> str | None:
        return str(UUID(value)) if value is not None else None

    @model_validator(mode="after")
    def validate_contract(self) -> CreateGoal:
        self.objective = self.objective.strip()
        self.success_criteria = [s.strip() for s in self.success_criteria]
        if not self.objective or any(not s for s in self.success_criteria):
            raise ValueError("objective and criteria must not be blank")
        if self.mode == "ongoing" and self.kind != "long":
            raise ValueError("ongoing goals must be long-term")
        return self


class GoalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "progress",
        "evidence",
        "wait",
        "block",
        "complete",
        "assess",
        "pause",
        "resume",
        "cancel",
        "approve",
        "steer",
        "link_task",
        "reconciled",
        "revise",
    ]
    version: int = Field(ge=1)
    note: str = Field(default="", max_length=20000)
    next_action: str = Field(default="", max_length=12000)
    objective: str | None = Field(default=None, min_length=1, max_length=12000)
    success_criteria: list[str] | None = Field(default=None, min_length=1, max_length=50)
    criterion: int | None = Field(default=None, ge=0)
    reference: str = Field(default="", max_length=4000)
    satisfied: bool | None = None
    wake_at: datetime | None = None
    event_type: str | None = Field(default=None, max_length=200)
    event_match: dict[str, str] = Field(default_factory=dict)
    task_id: str | None = None
    assessment: Literal["meeting", "missing", "insufficient"] | None = None
    token_budget: int | None = Field(default=None, gt=0)

    @field_validator("task_id")
    @classmethod
    def task_uuid(cls, value: str | None) -> str | None:
        return str(UUID(value)) if value is not None else None

    @model_validator(mode="after")
    def aware_date(self) -> GoalUpdate:
        if self.wake_at is not None and self.wake_at.tzinfo is None:
            raise ValueError("wake_at must include a timezone")
        return self


def new_goal(spec: CreateGoal, actor: str) -> dict[str, Any]:
    return {
        **spec.model_dump(),
        "id": str(uuid4()),
        "owner": "main",
        "created_by": actor,
        "status": "queued",
        "version": 1,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "ready_at": now_iso(),
        "checkpoint": "",
        "next_action": "Start pursuing the objective",
        "evidence": [],
        "independent_evidence_required": True,
        "wait": None,
        "blocker": "",
        "blocker_count": 0,
        "failures": 0,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "attempts": 0,
        "assessment": None,
        "recovery_required": False,
    }


def completion_missing(goal: dict[str, Any]) -> list[str]:
    missing = []
    for i, criterion in enumerate(goal["success_criteria"]):
        evidence = [e for e in goal["evidence"] if e["criterion"] == i]
        if not evidence or not evidence[-1]["satisfied"]:
            missing.append(criterion)
    return missing


def requires_review(g: dict[str, Any]) -> bool:
    if g["human_review"]:
        return True
    if not g.get("independent_evidence_required", False):
        return False
    for criterion in range(len(g["success_criteria"])):
        evidence = [e for e in g["evidence"] if e["criterion"] == criterion]
        check = evidence[-1].get("verification", {}) if evidence else {}
        if not (check.get("independent") and check.get("criterion_verified")):
            return True
    return False


def transition(
    goal: dict[str, Any], change: GoalUpdate, *, operator: bool = False
) -> dict[str, Any]:
    """Apply a versioned command. Evidence assesses criteria; prose alone never completes."""
    g = deepcopy(goal)
    if change.version != g["version"]:
        raise ValueError("stale goal version; reload before updating")
    action = change.action
    if g["status"] in TERMINAL:
        raise ValueError("goal is already closed")
    if g["status"] in {"paused", "review", "blocked"} and action not in {
        "resume",
        "cancel",
        "approve",
        "steer",
        "pause",
        "revise",
    }:
        raise ValueError("goal is inactive; resume it before recording work")
    if action in {"resume", "approve", "steer", "revise"} and not operator:
        raise ValueError("this action requires the operator")
    if (
        action in {"progress", "block", "complete", "assess", "steer", "reconciled"}
        and not change.note.strip()
    ):
        raise ValueError("a concrete note is required")
    if action == "progress":
        if change.note != g["checkpoint"]:
            g["blocker_count"] = 0
            g["progress_revision"] = g.get("progress_revision", 0) + 1
        g.update(checkpoint=change.note, next_action=change.next_action)
    elif action == "evidence":
        if change.criterion is None or change.criterion >= len(g["success_criteria"]):
            raise ValueError("criterion must identify an existing success criterion")
        if not change.note.strip() or not change.reference.strip() or change.satisfied is None:
            raise ValueError("evidence requires a reference, assessment, and explanation")
        if change.satisfied and "pytest:failed:" in change.reference:
            raise ValueError("a failed test cannot establish satisfaction")
        g["progress_revision"] = g.get("progress_revision", 0) + 1
        g["evidence"].append(
            {
                "criterion": change.criterion,
                "summary": change.note,
                "reference": change.reference,
                "satisfied": change.satisfied,
                "recorded_at": now_iso(),
            }
        )
    elif action == "wait":
        if not change.note.strip():
            raise ValueError("waiting requires a reason")
        if change.wake_at and change.wake_at <= datetime.now(UTC):
            raise ValueError("wake_at must be in the future")
        wake_at = change.wake_at.isoformat() if change.wake_at else future(g["review_seconds"])
        if g["kind"] == "short" and not g["parent_goal_id"]:
            g["kind"] = "long"
        g.update(
            status="waiting",
            ready_at=wake_at,
            wait={
                "registered_at": now_iso(),
                "reason": change.note,
                "event_type": change.event_type,
                "event_match": change.event_match,
                "task_id": change.task_id,
                "wake_at": wake_at,
            },
        )
    elif action == "block":
        g["blocker_count"] = g["blocker_count"] + 1 if g["blocker"] == change.note else 1
        g["blocker"] = change.note
        if g["blocker_count"] >= 3:
            g["status"] = "blocked"
    elif action == "complete":
        if g["mode"] == "ongoing":
            raise ValueError("ongoing goals are assessed, not completed")
        if g["recovery_required"]:
            raise ValueError("reconcile the interrupted execution before completing")
        missing = completion_missing(g)
        if missing:
            raise ValueError("unsatisfied criteria: " + "; ".join(missing))
        g.update(status="review" if requires_review(g) else "complete", completion_note=change.note)
    elif action == "approve":
        if g["status"] != "review" or completion_missing(g):
            raise ValueError("goal is not ready for approval")
        if g["mode"] == "ongoing":
            g.update(status="waiting", ready_at=future(g["review_seconds"]), evidence=[])
            g["assessment"]["approved_by_operator"] = True
        else:
            g["status"] = "complete"
    elif action == "assess":
        if g["mode"] != "ongoing" or change.assessment is None:
            raise ValueError("ongoing assessments require meeting, missing, or insufficient")
        if change.assessment == "meeting" and completion_missing(g):
            raise ValueError("meeting target requires evidence for every criterion")
        g["assessment"] = {"status": change.assessment, "note": change.note, "at": now_iso()}
        g.update(
            status="waiting",
            ready_at=future(g["review_seconds"]),
            wait={"reason": "Next assessment"},
        )
        # Judgment-based meeting assessments wait for an operator, then start a new period.
        if change.assessment == "meeting" and requires_review(g):
            g["status"] = "review"
        else:
            g["evidence"] = []
    elif action in {"pause", "cancel"}:
        g["status"] = "paused" if action == "pause" else "canceled"
    elif action == "resume":
        if change.token_budget is not None:
            g["token_budget"] = change.token_budget
        if g["token_budget"] and g["tokens_used"] >= g["token_budget"]:
            raise ValueError("increase the exhausted token budget before resuming")
        g.update(
            status="queued",
            ready_at=now_iso(),
            blocker="",
            blocker_count=0,
            failures=0,
            no_progress_runs=0,
        )
    elif action == "steer":
        g.update(checkpoint=change.note, next_action=change.next_action or change.note)
    elif action == "revise":
        if not change.note.strip():
            raise ValueError("revising criteria requires a reason")
        spec = CreateGoal(
            objective=change.objective or g["objective"],
            success_criteria=change.success_criteria or g["success_criteria"],
        )
        g.update(
            objective=spec.objective,
            success_criteria=spec.success_criteria,
            evidence=[],
            criteria_revised_at=now_iso(),
        )
        if g["status"] == "review":
            g["status"] = "paused"
    elif action == "reconciled":
        g.update(recovery_required=False, checkpoint=change.note)
    if action in {"steer", "revise"}:
        g["steer_version"] = g["version"] + 1
    g["version"] += 1
    g["updated_at"] = now_iso()
    return g
