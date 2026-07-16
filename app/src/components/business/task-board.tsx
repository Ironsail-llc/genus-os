"use client";

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

interface Task {
  id: string;
  title: string;
  status: "TODO" | "IN_PROGRESS" | "REVIEW" | "DONE";
  body?: string;
  dueAt?: string;
  priority?: string;
  assignedToAgent?: string;
  tags?: string[];
  slaDeadlineAt?: string;
  parentTaskId?: string;
  requiresHuman?: boolean;
  // Phase 4 — planner-set fields surfaced for the operator.
  objective?: string;
  nextAction?: string;
  nextActionAgent?: string;
  questionForOperator?: string;
  escalationCount?: number;
}

interface TaskBoardProps {
  tasks: Task[];
  onApprove?: (taskId: string, resolution: string) => void;
  onReject?: (taskId: string, reason: string) => void;
  onResolve?: (taskId: string, resolution: string) => void;
  // advanceTo is constrained to the status union so this stays assignable
  // from useTasks().answerQuestion (whose advanceTo is Task["status"]). The
  // Promise<boolean> result lets the wired handler report POST success so the
  // board can keep the operator's typed answer for retry on failure.
  onAnswer?: (taskId: string, answer: string, advanceTo?: Task["status"]) => void | Promise<boolean>;
}

const statusColumns = ["TODO", "IN_PROGRESS", "REVIEW", "DONE"] as const;
const statusLabels: Record<string, string> = {
  TODO: "To Do",
  IN_PROGRESS: "In Progress",
  REVIEW: "Review",
  DONE: "Done",
};
const statusColors: Record<string, string> = {
  TODO: "border-t-muted-foreground",
  IN_PROGRESS: "border-t-info",
  REVIEW: "border-t-warning",
  DONE: "border-t-success",
};
const columnTints: Record<string, string> = {
  TODO: "bg-muted-foreground/[0.03]",
  IN_PROGRESS: "bg-info/[0.03]",
  REVIEW: "bg-warning/[0.03]",
  DONE: "bg-success/[0.03]",
};
const priorityColors: Record<string, string> = {
  urgent: "bg-destructive/20 text-destructive",
  high: "bg-warning/20 text-warning",
  normal: "bg-muted text-muted-foreground",
  low: "bg-muted/50 text-muted-foreground",
};

function isSlaOverdue(slaDeadlineAt?: string): boolean {
  if (!slaDeadlineAt) return false;
  return new Date(slaDeadlineAt) < new Date();
}

export function TaskBoard({ tasks, onApprove, onReject, onResolve, onAnswer }: TaskBoardProps) {
  const [actionPending, setActionPending] = useState<string | null>(null);
  const [resolvingTaskId, setResolvingTaskId] = useState<string | null>(null);
  const [resolutionText, setResolutionText] = useState("");
  const [answerText, setAnswerText] = useState<Record<string, string>>({});
  const [overrideOpen, setOverrideOpen] = useState<Record<string, boolean>>({});

  const handleAnswer = async (taskId: string, advanceTo?: Task["status"]) => {
    const text = (answerText[taskId] || "").trim();
    if (!text) return;
    setActionPending(taskId);
    try {
      if (onAnswer) {
        // Wired path: useTasks().answerQuestion applies the optimistic status
        // flip (which unmounts this block) and rolls back on a failed POST.
        // Await its success result so we only clear the typed answer below on
        // success — on failure the rollback re-shows the question and the
        // preserved text lets the operator retry. Mirrors the fallback below.
        const result = onAnswer(taskId, text, advanceTo);
        const ok = result instanceof Promise ? await result : true;
        if (!ok) return;
      } else {
        // Fallback for standalone usage: route through the same
        // /api/actions/execute allowlist as every other task mutation (so the
        // bridge gets verified auth + rate limiting). Check res.ok and throw so a
        // failed POST isn't silently swallowed (the throw skips the clear
        // below, so the operator keeps their text to retry).
        const res = await fetch("/api/actions/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tool: "answer_question",
            params: { task_id: taskId, answer: text, advanceTo, channel: "helm" },
          }),
        });
        if (!res.ok) throw new Error(`answer failed: ${res.status}`);
      }
      setAnswerText((prev) => {
        const next = { ...prev };
        delete next[taskId];
        return next;
      });
    } finally {
      setActionPending(null);
    }
  };

  const handleApprove = async (taskId: string) => {
    setActionPending(taskId);
    try {
      if (onApprove) {
        onApprove(taskId, "Approved via Helm");
      } else {
        await fetch("/api/actions/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tool: "approve_task", params: { task_id: taskId, resolution: "Approved via Helm" } }),
        });
      }
    } finally {
      setActionPending(null);
    }
  };

  const handleReject = async (taskId: string) => {
    setActionPending(taskId);
    try {
      if (onReject) {
        onReject(taskId, "Rejected via Helm");
      } else {
        await fetch("/api/actions/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tool: "reject_task", params: { task_id: taskId, reason: "Rejected via Helm" } }),
        });
      }
    } finally {
      setActionPending(null);
    }
  };

  const handleResolve = async (taskId: string) => {
    if (!resolutionText.trim()) return;
    setActionPending(taskId);
    try {
      if (onResolve) {
        onResolve(taskId, resolutionText);
      } else {
        await fetch("/api/actions/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tool: "resolve_task", params: { id: taskId, resolution: resolutionText } }),
        });
      }
    } finally {
      setActionPending(null);
      setResolvingTaskId(null);
      setResolutionText("");
    }
  };

  return (
    <div className="grid grid-cols-1 md:grid-cols-4 gap-4" data-testid="task-board">
      {statusColumns.map((status) => {
        const columnTasks = tasks.filter((t) => t.status === status);
        return (
          <div key={status} className={`space-y-2 rounded-lg p-2 ${columnTints[status]}`}>
            <div className={`flex items-center gap-2 mb-2 border-t-2 pt-2 ${statusColors[status]}`}>
              <h4 className="text-sm font-medium">{statusLabels[status]}</h4>
              <Badge variant="secondary">{columnTasks.length}</Badge>
            </div>
            {columnTasks.map((task) => (
              <Card
                key={task.id}
                className={`glass-panel ${isSlaOverdue(task.slaDeadlineAt) && status !== "DONE" ? "ring-1 ring-destructive/50 animate-pulse" : ""}`}
                data-testid="task-card"
              >
                <CardHeader className="pb-1 pt-3 px-3">
                  <div className="flex items-center gap-1.5">
                    {task.priority && task.priority !== "normal" && (
                      <Badge className={`text-[10px] px-1 py-0 ${priorityColors[task.priority] || ""}`} data-testid="priority-badge">
                        {task.priority}
                      </Badge>
                    )}
                    {task.requiresHuman && (
                      <Badge className="text-[10px] px-1 py-0 bg-destructive/20 text-destructive" data-testid="requires-human-badge">
                        needs you
                      </Badge>
                    )}
                    {task.escalationCount != null && task.escalationCount > 0 && (
                      <Badge
                        className="text-[10px] px-1 py-0 bg-warning/20 text-warning"
                        data-testid="escalation-badge"
                        title={`Escalated ${task.escalationCount}× since last operator answer`}
                      >
                        ↑{task.escalationCount}
                      </Badge>
                    )}
                    <CardTitle className="text-sm flex-1">{task.title}</CardTitle>
                  </div>
                </CardHeader>
                <CardContent className="px-3 pb-3">
                  {task.body && (
                    <p className="text-xs text-muted-foreground line-clamp-2">
                      {task.body}
                    </p>
                  )}
                  {/* Planner subsection — Phase 4 surface of objective/next_action/question. */}
                  {task.objective && status !== "DONE" && (
                    <p className="text-xs italic text-muted-foreground mt-1.5 truncate" data-testid="task-objective">
                      Objective: {task.objective}
                    </p>
                  )}
                  {task.nextAction && status !== "DONE" && (
                    <p className="text-xs text-muted-foreground mt-1" data-testid="task-next-action">
                      Next: {task.nextAction}
                      {task.nextActionAgent && (
                        <Badge variant="outline" className="ml-1 text-[10px] px-1 py-0">
                          @{task.nextActionAgent}
                        </Badge>
                      )}
                    </p>
                  )}
                  {task.assignedToAgent && (
                    <p className="text-xs text-muted-foreground mt-1">
                      {task.assignedToAgent}
                    </p>
                  )}
                  {task.dueAt && (
                    <p className="text-xs text-muted-foreground mt-1">
                      Due: {new Date(task.dueAt).toLocaleDateString()}
                    </p>
                  )}
                  {task.tags && task.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-1.5">
                      {task.tags.map((tag) => (
                        <Badge key={tag} variant="outline" className="text-[10px] px-1 py-0">
                          {tag}
                        </Badge>
                      ))}
                    </div>
                  )}
                  {/* Question + Answer UI — primary path on REVIEW when the planner asked something. */}
                  {status === "REVIEW" && task.questionForOperator && (
                    <div
                      className="mt-2 rounded border border-warning/30 bg-warning/[0.05] p-2"
                      data-testid="question-block"
                    >
                      <p className="text-xs font-medium text-warning/90 mb-1">Question</p>
                      <p className="text-xs text-foreground/90 mb-2" data-testid="question-text">
                        {task.questionForOperator}
                      </p>
                      <textarea
                        className="w-full h-16 text-xs rounded bg-background/40 border border-border/40 p-1.5"
                        placeholder="Type your answer…"
                        value={answerText[task.id] || ""}
                        onChange={(e) =>
                          setAnswerText((prev) => ({ ...prev, [task.id]: e.target.value }))
                        }
                        data-testid="answer-input"
                      />
                      <div className="flex gap-1.5 mt-1.5">
                        <Button
                          size="sm"
                          className="h-7 text-xs px-3 bg-warning hover:bg-warning/90 text-warning-foreground"
                          disabled={actionPending === task.id || !(answerText[task.id] || "").trim()}
                          onClick={() => handleAnswer(task.id, "IN_PROGRESS")}
                          data-testid="answer-button"
                        >
                          Answer
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-7 text-xs px-2"
                          onClick={() =>
                            setOverrideOpen((prev) => ({ ...prev, [task.id]: !prev[task.id] }))
                          }
                          data-testid="override-toggle"
                        >
                          {overrideOpen[task.id] ? "Hide override" : "Override (approve/reject)"}
                        </Button>
                      </div>
                    </div>
                  )}
                  {/* Approve/Reject — default for REVIEW without a question; hidden behind toggle when a question is present. */}
                  {status === "REVIEW" && (!task.questionForOperator || overrideOpen[task.id]) && (
                    <div className="flex gap-1.5 mt-2" data-testid="review-actions">
                      <Button
                        size="sm"
                        variant="default"
                        className="h-8 text-xs px-3 bg-success hover:bg-success/90 text-success-foreground"
                        disabled={actionPending === task.id}
                        onClick={() => handleApprove(task.id)}
                        data-testid="approve-button"
                      >
                        Approve
                      </Button>
                      <Button
                        size="sm"
                        variant="destructive"
                        className="h-8 text-xs px-3"
                        disabled={actionPending === task.id}
                        onClick={() => handleReject(task.id)}
                        data-testid="reject-button"
                      >
                        Reject
                      </Button>
                    </div>
                  )}
                  {status !== "DONE" && (
                    <div className="mt-2" data-testid="resolve-section">
                      {resolvingTaskId === task.id ? (
                        <div className="flex gap-1.5">
                          <Input
                            className="h-8 text-xs"
                            placeholder="Resolution note..."
                            value={resolutionText}
                            onChange={(e) => setResolutionText(e.target.value)}
                            onKeyDown={(e) => e.key === "Enter" && handleResolve(task.id)}
                            autoFocus
                            data-testid="resolution-input"
                          />
                          <Button
                            size="sm"
                            className="h-8 text-xs px-3 bg-success hover:bg-success/90 text-success-foreground"
                            disabled={actionPending === task.id || !resolutionText.trim()}
                            onClick={() => handleResolve(task.id)}
                            data-testid="confirm-resolve-button"
                          >
                            Confirm
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="h-8 text-xs px-2"
                            onClick={() => { setResolvingTaskId(null); setResolutionText(""); }}
                          >
                            ✕
                          </Button>
                        </div>
                      ) : (
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 text-xs px-2"
                          disabled={actionPending === task.id}
                          onClick={() => setResolvingTaskId(task.id)}
                          data-testid="resolve-button"
                        >
                          Resolve
                        </Button>
                      )}
                    </div>
                  )}
                </CardContent>
              </Card>
            ))}
          </div>
        );
      })}
    </div>
  );
}
