"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useEventStream } from "@/lib/event-bus/use-event-stream";

export interface Task {
  id: string;
  title: string;
  status: "TODO" | "IN_PROGRESS" | "REVIEW" | "DONE";
  body?: string;
  dueAt?: string;
  priority?: string;
  assignedToAgent?: string;
  createdByAgent?: string;
  tags?: string[];
  slaDeadlineAt?: string;
  parentTaskId?: string;
  resolution?: string;
  requiresHuman?: boolean;
  escalationCount?: number;
  startedAt?: string;
  updatedAt?: string;
  createdAt?: string;
}

interface UseTasksOptions {
  agentFilter?: string;
  priorityFilter?: string;
  live?: boolean;
}

const STALE_THRESHOLD_MS = 30_000; // 30s tab-backgrounded → refetch

/**
 * Hook for fetching tasks with optional SSE live updates.
 *
 * When `live=true`, subscribes to the agent event stream and refetches
 * on task.created / task.updated / task.resolved events.
 * Uses Page Visibility API to detect stale tabs and refetch on return.
 */
export function useTasks(options: UseTasksOptions = {}) {
  const { agentFilter, priorityFilter, live = false } = options;
  const [tasks, setTasks] = useState<Task[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const lastVisibleRef = useRef(Date.now());
  const fetchIdRef = useRef(0);

  const fetchTasks = useCallback(async () => {
    const id = ++fetchIdRef.current;
    try {
      setIsLoading(true);
      const params = new URLSearchParams();
      if (agentFilter) params.set("assignedToAgent", agentFilter);
      if (priorityFilter) params.set("priority", priorityFilter);
      params.set("limit", "100");

      const res = await fetch("/api/actions/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tool: "list_tasks",
          params: Object.fromEntries(params),
        }),
      });

      if (!res.ok) return;
      const json = await res.json();
      // Only apply if this is still the latest fetch
      if (id === fetchIdRef.current) {
        setTasks(json.data?.tasks || []);
      }
    } finally {
      if (id === fetchIdRef.current) {
        setIsLoading(false);
      }
    }
  }, [agentFilter, priorityFilter]);

  // Initial fetch
  useEffect(() => {
    fetchTasks();
  }, [fetchTasks]);

  // SSE live updates
  const { events } = useEventStream({
    streams: ["agent"],
    enabled: live,
    maxEvents: 20,
  });

  // Refetch when task events arrive
  const lastEventIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (!live || events.length === 0) return;
    const latest = events[0];
    if (
      latest.id !== lastEventIdRef.current &&
      ["task.created", "task.updated", "task.resolved"].includes(latest.type)
    ) {
      lastEventIdRef.current = latest.id;
      fetchTasks();
    }
  }, [events, live, fetchTasks]);

  // Page Visibility stale detection
  useEffect(() => {
    if (!live) return;
    function onVisibilityChange() {
      if (document.visibilityState === "visible") {
        const elapsed = Date.now() - lastVisibleRef.current;
        if (elapsed > STALE_THRESHOLD_MS) {
          fetchTasks();
        }
      } else {
        lastVisibleRef.current = Date.now();
      }
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [live, fetchTasks]);

  // Optimistic status update
  const updateTaskStatus = useCallback(
    async (taskId: string, newStatus: string, resolution?: string) => {
      const previousTasks = tasks;
      // Optimistic update
      setTasks((prev) =>
        prev.map((t) =>
          t.id === taskId ? { ...t, status: newStatus as Task["status"] } : t
        )
      );
      try {
        const res = await fetch("/api/actions/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tool: "update_task",
            params: { task_id: taskId, status: newStatus, resolution },
          }),
        });
        if (!res.ok) {
          setTasks(previousTasks); // rollback
        }
      } catch {
        setTasks(previousTasks); // rollback
      }
    },
    [tasks]
  );

  // Approve a REVIEW task (moves to DONE)
  const approveTask = useCallback(
    async (taskId: string, resolution: string) => {
      const previousTasks = tasks;
      setTasks((prev) =>
        prev.map((t) =>
          t.id === taskId ? { ...t, status: "DONE" as Task["status"] } : t
        )
      );
      try {
        const res = await fetch("/api/actions/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tool: "approve_task",
            params: { task_id: taskId, resolution },
          }),
        });
        if (!res.ok) {
          setTasks(previousTasks);
        }
      } catch {
        setTasks(previousTasks);
      }
    },
    [tasks]
  );

  // Reject a REVIEW task (moves back to IN_PROGRESS)
  const rejectTask = useCallback(
    async (taskId: string, reason: string) => {
      const previousTasks = tasks;
      setTasks((prev) =>
        prev.map((t) =>
          t.id === taskId ? { ...t, status: "IN_PROGRESS" as Task["status"] } : t
        )
      );
      try {
        const res = await fetch("/api/actions/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tool: "reject_task",
            params: { task_id: taskId, reason },
          }),
        });
        if (!res.ok) {
          setTasks(previousTasks);
        }
      } catch {
        setTasks(previousTasks);
      }
    },
    [tasks]
  );

  // Resolve any non-DONE task with a resolution note
  const resolveTask = useCallback(
    async (taskId: string, resolution: string) => {
      const previousTasks = tasks;
      setTasks((prev) =>
        prev.map((t) =>
          t.id === taskId ? { ...t, status: "DONE" as Task["status"] } : t
        )
      );
      try {
        const res = await fetch("/api/actions/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tool: "resolve_task",
            params: { id: taskId, resolution },
          }),
        });
        if (!res.ok) {
          setTasks(previousTasks);
        }
      } catch {
        setTasks(previousTasks);
      }
    },
    [tasks]
  );

  return { tasks, isLoading, refetch: fetchTasks, updateTaskStatus, approveTask, rejectTask, resolveTask };
}
