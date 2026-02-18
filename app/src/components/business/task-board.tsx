"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface Task {
  id: string;
  title: string;
  status: "TODO" | "IN_PROGRESS" | "DONE";
  body?: string;
  dueAt?: string;
}

interface TaskBoardProps {
  tasks: Task[];
}

const statusColumns = ["TODO", "IN_PROGRESS", "DONE"] as const;
const statusLabels: Record<string, string> = {
  TODO: "To Do",
  IN_PROGRESS: "In Progress",
  DONE: "Done",
};

export function TaskBoard({ tasks }: TaskBoardProps) {
  return (
    <div className="grid grid-cols-3 gap-4" data-testid="task-board">
      {statusColumns.map((status) => {
        const columnTasks = tasks.filter((t) => t.status === status);
        return (
          <div key={status} className="space-y-2">
            <div className="flex items-center gap-2 mb-2">
              <h4 className="text-sm font-medium">{statusLabels[status]}</h4>
              <Badge variant="secondary">{columnTasks.length}</Badge>
            </div>
            {columnTasks.map((task) => (
              <Card key={task.id} className="glass-panel" data-testid="task-card">
                <CardHeader className="pb-1 pt-3 px-3">
                  <CardTitle className="text-sm">{task.title}</CardTitle>
                </CardHeader>
                <CardContent className="px-3 pb-3">
                  {task.body && (
                    <p className="text-xs text-muted-foreground line-clamp-2">
                      {task.body}
                    </p>
                  )}
                  {task.dueAt && (
                    <p className="text-xs text-muted-foreground mt-1">
                      Due: {new Date(task.dueAt).toLocaleDateString()}
                    </p>
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
