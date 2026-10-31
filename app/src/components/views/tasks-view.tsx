"use client";

import { useState } from "react";
import { MetricGrid } from "@/components/business/metric-grid";
import { TaskBoard } from "@/components/business/task-board";
import type { Task } from "@/hooks/use-tasks";
import { ListSkeleton } from "@/components/business/list-skeleton";

interface TasksViewProps {
  visible: boolean;
  tasks: Task[];
  isLoading: boolean;
  onApprove: (taskId: string, resolution: string) => void;
  onReject: (taskId: string, reason: string) => void;
  onAnswer: (taskId: string, answer: string, advanceTo?: Task["status"]) => void | Promise<boolean>;
}

export function TasksView({
  visible,
  tasks,
  isLoading,
  onApprove,
  onReject,
  onAnswer,
}: TasksViewProps) {
  const [now] = useState(() => Date.now());
  const dayAgo = now - 86_400_000;

  const todoCount = tasks.filter((t) => t.status === "TODO").length;
  const inProgressCount = tasks.filter((t) => t.status === "IN_PROGRESS").length;
  const reviewCount = tasks.filter((t) => t.status === "REVIEW").length;
  const doneRecent = tasks.filter(
    (t) => t.status === "DONE" && t.updatedAt && new Date(t.updatedAt).getTime() > dayAgo
  ).length;

  const metrics = [
    { title: "To Do", value: todoCount },
    { title: "In Progress", value: inProgressCount },
    { title: "Review", value: reviewCount },
    { title: "Done (24h)", value: doneRecent },
  ];

  return (
    <div
      className="h-full w-full min-w-0 flex flex-col overflow-y-auto"
      style={{ display: visible ? "flex" : "none" }}
      data-testid="tasks-view"
    >
      <div className="min-w-0 p-4 space-y-4">
        <MetricGrid metrics={metrics} />
        {isLoading && tasks.length === 0 ? (
          <ListSkeleton testId="tasks-loading" label="Loading tasks" />
        ) : (
          <TaskBoard tasks={tasks} onApprove={onApprove} onReject={onReject} onAnswer={onAnswer} />
        )}
      </div>
    </div>
  );
}
