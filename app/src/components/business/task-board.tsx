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
}

interface TaskBoardProps {
  tasks: Task[];
  onApprove?: (taskId: string, resolution: string) => void;
  onReject?: (taskId: string, reason: string) => void;
  onResolve?: (taskId: string, resolution: string) => void;
}

const statusColumns = ["TODO", "IN_PROGRESS", "REVIEW", "DONE"] as const;
const statusLabels: Record<string, string> = {
  TODO: "To Do",
  IN_PROGRESS: "In Progress",
  REVIEW: "Review",
  DONE: "Done",
};
const statusColors: Record<string, string> = {
  TODO: "border-t-zinc-500",
  IN_PROGRESS: "border-t-blue-500",
  REVIEW: "border-t-amber-500",
  DONE: "border-t-emerald-500",
};
const columnTints: Record<string, string> = {
  TODO: "bg-zinc-500/[0.03]",
  IN_PROGRESS: "bg-blue-500/[0.03]",
  REVIEW: "bg-amber-500/[0.03]",
  DONE: "bg-emerald-500/[0.03]",
};
const priorityColors: Record<string, string> = {
  urgent: "bg-red-500/20 text-red-400",
  high: "bg-orange-500/20 text-orange-400",
  normal: "bg-zinc-500/20 text-zinc-400",
  low: "bg-zinc-700/20 text-zinc-500",
};

function isSlaOverdue(slaDeadlineAt?: string): boolean {
  if (!slaDeadlineAt) return false;
  return new Date(slaDeadlineAt) < new Date();
}

export function TaskBoard({ tasks, onApprove, onReject, onResolve }: TaskBoardProps) {
  const [actionPending, setActionPending] = useState<string | null>(null);
  const [resolvingTaskId, setResolvingTaskId] = useState<string | null>(null);
  const [resolutionText, setResolutionText] = useState("");

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
                className={`glass-panel ${isSlaOverdue(task.slaDeadlineAt) && status !== "DONE" ? "ring-1 ring-red-500/50 animate-pulse" : ""}`}
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
                      <Badge className="text-[10px] px-1 py-0 bg-red-500/20 text-red-400" data-testid="requires-human-badge">
                        needs you
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
                  {status === "REVIEW" && (
                    <div className="flex gap-1.5 mt-2" data-testid="review-actions">
                      <Button
                        size="sm"
                        variant="default"
                        className="h-8 text-xs px-3 bg-emerald-600 hover:bg-emerald-700"
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
                            className="h-8 text-xs px-3 bg-emerald-600 hover:bg-emerald-700"
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
