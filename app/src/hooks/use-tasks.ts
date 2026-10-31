"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAppQueryClient } from "@/lib/query/query-client";
import { executeTool, refusalOf } from "@/lib/query/fetch-refused";
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
  // Phase 4 — planner-set fields surfaced for the operator.
  objective?: string;
  nextAction?: string;
  nextActionAgent?: string;
  questionForOperator?: string;
}

interface UseTasksOptions {
  agentFilter?: string;
  priorityFilter?: string;
  live?: boolean;
}

const STALE_MS = 30_000; // a reader mounting within 30s paints from cache

/** The cache entry for one filtered board, shared by every reader of it. */
export function tasksQueryKey(agentFilter?: string, priorityFilter?: string) {
  return ["tasks", { agentFilter: agentFilter ?? null, priorityFilter: priorityFilter ?? null }] as const;
}

async function fetchTaskList(
  signal: AbortSignal,
  agentFilter?: string,
  priorityFilter?: string
): Promise<Task[]> {
  const params = new URLSearchParams();
  if (agentFilter) params.set("assignedToAgent", agentFilter);
  if (priorityFilter) params.set("priority", priorityFilter);
  params.set("limit", "100");
  const json = await executeTool("list_tasks", Object.fromEntries(params), signal);
  return json.data?.tasks || [];
}

const TASK_EVENTS = ["task.created", "task.updated", "task.resolved", "task.answered"];

/**
 * Hook for fetching tasks with optional SSE live updates.
 *
 * Every reader of the same filters shares one cache entry and one request
 * (the shell's badges, the home metrics and the board). When `live=true`, the
 * reader subscribes to the agent event stream and refreshes the shared entry
 * on task.created / task.updated / task.resolved / task.answered. A tab that
 * comes back after the entry went stale reads it again once.
 */
export function useTasks(options: UseTasksOptions = {}) {
  const { agentFilter, priorityFilter, live = false } = options;
  const client = useAppQueryClient();
  const queryKey = useMemo(() => tasksQueryKey(agentFilter, priorityFilter), [agentFilter, priorityFilter]);
  const query = useQuery(
    {
      queryKey,
      queryFn: ({ signal }) => fetchTaskList(signal, agentFilter, priorityFilter),
      staleTime: STALE_MS,
    },
    client
  );
  const tasks = useMemo(() => query.data ?? [], [query.data]);
  // A refused fetch must be reportable: an empty board and an unreachable
  // board look identical to the operator otherwise.
  const error = refusalOf(query.error);
  const isLoading = query.isFetching || query.isPending;

  const { refetch: refetchQuery } = query;
  const fetchTasks = useCallback(async () => {
    await refetchQuery();
  }, [refetchQuery]);

  /** Write the shared entry, so every reader moves at once. */
  const setTasks = useCallback(
    (next: Task[] | ((prev: Task[]) => Task[])) => {
      client.setQueryData<Task[]>(queryKey, (prev) =>
        typeof next === "function" ? next(prev ?? []) : next
      );
    },
    [client, queryKey]
  );

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
    if (latest.id !== lastEventIdRef.current && TASK_EVENTS.includes(latest.type)) {
      lastEventIdRef.current = latest.id;
      void client.invalidateQueries({ queryKey });
    }
  }, [events, live, client, queryKey]);

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
    [tasks, setTasks]
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
    [tasks, setTasks]
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
    [tasks, setTasks]
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
    [tasks, setTasks]
  );

  // Phase 4 — answer a planner-set question_for_operator. Clears the
  // question, resets escalation_count, optionally advances status. The
  // status flip is applied optimistically (snappy board); returns whether
  // the POST succeeded so the caller can preserve the operator's typed
  // answer for retry when it didn't (state is rolled back here either way).
  const answerQuestion = useCallback(
    async (taskId: string, answer: string, advanceTo?: Task["status"]): Promise<boolean> => {
      const previousTasks = tasks;
      setTasks((prev) =>
        prev.map((t) =>
          t.id === taskId
            ? {
                ...t,
                questionForOperator: undefined,
                requiresHuman: false,
                escalationCount: 0,
                status: advanceTo ?? t.status,
              }
            : t
        )
      );
      try {
        const res = await fetch("/api/actions/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tool: "answer_question",
            params: { task_id: taskId, answer, advanceTo, channel: "helm" },
          }),
        });
        if (!res.ok) {
          setTasks(previousTasks);
          return false;
        }
        return true;
      } catch {
        setTasks(previousTasks);
        return false;
      }
    },
    [tasks, setTasks]
  );

  return {
    tasks,
    isLoading,
    error,
    refetch: fetchTasks,
    updateTaskStatus,
    approveTask,
    rejectTask,
    resolveTask,
    answerQuestion,
  };
}
