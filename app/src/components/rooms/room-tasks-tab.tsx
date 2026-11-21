"use client";

import { useCallback, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { InboxCard } from "@/components/views/inbox/inbox-card";
import { useRoomEvents } from "@/hooks/use-rooms";
import { APPROVALS_ENDPOINT, postApprovalAnswer } from "@/lib/inbox/answer";
import { OpenAsWorkspace } from "@/components/rooms/open-as-workspace";
import { RoomEmailApproval } from "@/components/rooms/room-email-approval";
import { readEmail } from "@/lib/rooms/email";
import { normalizePending, type AnswerBody, type PendingItem, type PendingKind } from "@/lib/inbox/pending";
import { acceptRoomTask, dismissRoomTask, fetchRoomTasks } from "@/lib/rooms/api";
import { splitRoomTasks, TASK_STATUS_LABELS, type RoomTask } from "@/lib/rooms/hub";
import { ROOM_LABEL } from "@/lib/rooms/labels";
import { refusalOf } from "@/lib/query/fetch-refused";
import { useAppQueryClient } from "@/lib/query/query-client";
import type { RoomSlotContext } from "./room-slots";

const STATUS_CLASSES: Record<RoomTask["status"], string> = {
  TODO: "border-border bg-muted text-muted-foreground",
  IN_PROGRESS: "border-info/30 bg-info/10 text-info",
  REVIEW: "border-warning/30 bg-warning/10 text-warning",
  DONE: "border-success/30 bg-success/10 text-success",
};

/**
 * Proposals waiting in this room. The approvals listing is the existing one:
 * an operator's lists the tenant's, a member's lists their own (or is refused,
 * which here means "nothing for you to decide", never an error on screen).
 */
async function fetchRoomProposals(roomId: string, signal?: AbortSignal): Promise<PendingItem[]> {
  const res = await fetch(APPROVALS_ENDPOINT, { signal, cache: "no-store" });
  if (!res.ok) return [];
  const rows = normalizePending(await res.json());
  return rows.filter((row) => row.kind === "action" && row.room_id === roomId);
}

interface Receipt {
  approved: boolean;
  question: string;
}

/**
 * A workspace's tasks: suggestions from its editions (each linked to the
 * edition it came from) with Accept and Dismiss, the work people have taken
 * with who has it and where it stands, and any action the agent proposed in
 * this room waiting on the person it is for. Live: a `room.task` event reads
 * the tasks again.
 *
 * A read-only user sees all of it with the controls disabled, and is told
 * why. The bridge decides every accept, dismiss and approval regardless.
 */
export function RoomTasksTab({ ctx }: { ctx: RoomSlotContext }) {
  const roomId = ctx.room.id;
  const client = useAppQueryClient();
  const tasksKey = useMemo(() => ["rooms", roomId, "tasks"] as const, [roomId]);
  const proposalsKey = useMemo(() => ["rooms", roomId, "proposals"] as const, [roomId]);

  const tasksQuery = useQuery(
    { queryKey: tasksKey, queryFn: ({ signal }) => fetchRoomTasks(roomId, signal), staleTime: 15_000 },
    client,
  );
  // A member decides an action only in a task workspace whose own task is
  // theirs; a report workspace leads to the task's workspace instead.
  const isTaskRoom = ctx.room.kind === "task";
  const proposalsQuery = useQuery(
    {
      queryKey: proposalsKey,
      queryFn: ({ signal }) => fetchRoomProposals(roomId, signal),
      staleTime: 15_000,
      enabled: isTaskRoom,
    },
    client,
  );

  useRoomEvents(roomId, (event) => {
    if (event.type === "room.task") {
      void client.invalidateQueries({ queryKey: tasksKey });
      void client.invalidateQueries({ queryKey: proposalsKey });
    } else if (event.type === "room.message") {
      // A proposal arrives with the agent's turn.
      void client.invalidateQueries({ queryKey: proposalsKey });
    }
  });

  const [messages, setMessages] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [receipts, setReceipts] = useState<Record<string, Receipt>>({});

  const canAct = ctx.can("accept_task");
  const { suggested, taken } = splitRoomTasks(tasksQuery.data ?? []);
  const proposals = (proposalsQuery.data ?? []).filter((item) => !receipts[item.id]);
  const receiptRows = Object.entries(receipts);

  const say = (taskId: string, text: string | null) =>
    setMessages((prev) => {
      const next = { ...prev };
      if (text) next[taskId] = text;
      else delete next[taskId];
      return next;
    });

  const decide = useCallback(
    async (task: RoomTask, verb: "accept" | "dismiss") => {
      if (!canAct) return;
      setBusy(task.taskId);
      say(task.taskId, null);
      try {
        const result = await (verb === "accept" ? acceptRoomTask : dismissRoomTask)(roomId, task.taskId);
        if (result.ok) {
          client.setQueryData<RoomTask[]>(tasksKey, (prev) =>
            prev?.map((row) =>
              row.taskId !== task.taskId
                ? row
                : (result.task ?? (verb === "dismiss" ? { ...row, dismissed: true } : row)),
            ),
          );
        } else if (verb === "accept" && result.status === 409) {
          say(task.taskId, `${result.takenBy ?? "Someone else"} already took this task. It has not been reassigned.`);
        } else {
          say(task.taskId, result.message);
        }
        void client.invalidateQueries({ queryKey: tasksKey });
      } finally {
        setBusy(null);
      }
    },
    [canAct, client, roomId, tasksKey],
  );

  const answer = useCallback(
    async (id: string, kind: PendingKind, body: AnswerBody) => {
      const outcome = await postApprovalAnswer(id, kind, body);
      if (outcome.settled) {
        const item = proposalsQuery.data?.find((row) => row.id === id);
        setReceipts((prev) => ({ ...prev, [id]: { approved: body.approved === true, question: item?.question ?? "" } }));
        void client.invalidateQueries({ queryKey: tasksKey });
      }
      return outcome;
    },
    [client, proposalsQuery.data, tasksKey],
  );

  const tasksError = refusalOf(tasksQuery.error);
  const nothing =
    !tasksQuery.isPending &&
    !tasksError &&
    suggested.length === 0 &&
    taken.length === 0 &&
    proposals.length === 0 &&
    receiptRows.length === 0;

  return (
    <section aria-labelledby="room-tasks-label" className="flex flex-col gap-3" data-testid="room-tasks">
      <div className="flex items-baseline gap-2">
        <h3
          id="room-tasks-label"
          className="text-[10.5px] font-medium uppercase tracking-[0.09em] text-muted-foreground/70"
        >
          Tasks
        </h3>
        {!canAct && (
          <span className="text-[11px] text-muted-foreground" data-testid="room-tasks-readonly">
            Read-only: you can follow these tasks but not take or dismiss them.
          </span>
        )}
      </div>

      {tasksError && (
        <p className="text-xs text-muted-foreground" data-testid="room-tasks-error">
          Tasks unavailable ({tasksError.status || "no answer"}). Nothing has been changed.
        </p>
      )}
      {tasksQuery.isPending && (
        <div className="h-12 animate-pulse rounded-lg bg-muted/60" aria-busy="true" aria-label="Loading tasks" />
      )}
      {nothing && (
        <p
          className="rounded-lg border border-dashed border-border px-3 py-4 text-center text-xs text-muted-foreground"
          data-testid="room-tasks-empty"
        >
          No tasks in this {ROOM_LABEL.singular} yet. Suggestions from new editions appear here.
        </p>
      )}

      {(proposals.length > 0 || receiptRows.length > 0) && (
        <div className="flex flex-col gap-2" data-testid="room-approvals">
          <h4 className="text-xs font-medium text-foreground">Waiting on your decision</h4>
          {proposals.map((item) =>
            readEmail(item.email) ? (
              <RoomEmailApproval key={item.id} item={item} email={readEmail(item.email)!} onAnswer={answer} />
            ) : (
              <InboxCard key={item.id} item={item} canWrite onAnswer={answer} onOpenRuns={() => undefined} />
            ),
          )}
          {receiptRows.map(([id, receipt]) => (
            <ReceiptRow key={id} id={id} receipt={receipt} />
          ))}
        </div>
      )}

      {suggested.length > 0 && (
        <TaskGroup label="Suggested" testId="room-tasks-suggested">
          {suggested.map((task) => (
            <TaskRow key={task.taskId} task={task} ctx={ctx} message={messages[task.taskId]}>
              <Button
                size="xs"
                disabled={!canAct || busy === task.taskId}
                onClick={() => void decide(task, "accept")}
                title={canAct ? undefined : "Read-only access"}
                data-testid={`room-task-accept-${task.taskId}`}
              >
                {busy === task.taskId && <Loader2 className="animate-spin" aria-hidden />}
                Accept
              </Button>
              <Button
                size="xs"
                variant="ghost"
                disabled={!canAct || busy === task.taskId}
                onClick={() => void decide(task, "dismiss")}
                title={canAct ? undefined : "Read-only access"}
                data-testid={`room-task-dismiss-${task.taskId}`}
              >
                Dismiss
              </Button>
            </TaskRow>
          ))}
        </TaskGroup>
      )}

      {taken.length > 0 && (
        <TaskGroup label="In hand" testId="room-tasks-taken">
          {taken.map((task) => (
            <TaskRow key={task.taskId} task={task} ctx={ctx} message={messages[task.taskId]}>
              {!isTaskRoom && canAct && (
                <OpenAsWorkspace taskId={task.taskId} label={`Open task ${ROOM_LABEL.singular}`} assumeServed />
              )}
            </TaskRow>
          ))}
        </TaskGroup>
      )}
    </section>
  );
}

function TaskGroup({ label, testId, children }: { label: string; testId: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5" data-testid={testId}>
      <h4 className="text-xs font-medium text-foreground">{label}</h4>
      <ul className="divide-y divide-border rounded-lg border border-border bg-card">{children}</ul>
    </div>
  );
}

function TaskRow({
  task,
  ctx,
  message,
  children,
}: {
  task: RoomTask;
  ctx: RoomSlotContext;
  message?: string;
  children?: ReactNode;
}) {
  const edition = task.editionId ? ctx.editions.find((e) => e.id === task.editionId) : undefined;
  const who = task.assignee?.displayName ?? task.acceptedBy?.displayName;
  return (
    <li className="flex flex-col gap-1.5 px-3 py-2.5" data-testid={`room-task-${task.taskId}`}>
      <div className="flex flex-wrap items-start gap-x-3 gap-y-1.5">
        <div className="min-w-0 flex-1">
          <p className="text-sm text-foreground wrap-anywhere">{task.title || "Untitled task"}</p>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <span
              className={`inline-flex items-center rounded-full border px-1.5 py-px text-[10.5px] font-medium ${STATUS_CLASSES[task.status] ?? STATUS_CLASSES.TODO}`}
              data-testid={`room-task-status-${task.taskId}`}
            >
              {TASK_STATUS_LABELS[task.status] ?? task.status}
            </span>
            {who && <span data-testid={`room-task-assignee-${task.taskId}`}>{who}</span>}
            {task.editionId &&
              (edition ? (
                <button
                  type="button"
                  onClick={() => ctx.showEdition(edition)}
                  className="rounded underline-offset-2 hover:text-foreground hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  data-testid={`room-task-edition-${task.taskId}`}
                >
                  From {edition.title || "an edition"}
                </button>
              ) : (
                <span data-testid={`room-task-edition-${task.taskId}`}>From an earlier edition</span>
              ))}
          </div>
        </div>
        {children && <div className="flex shrink-0 items-center gap-1.5">{children}</div>}
      </div>
      {message && (
        <p className="text-xs text-warning" role="status" data-testid={`room-task-message-${task.taskId}`}>
          {message}
        </p>
      )}
    </li>
  );
}

function ReceiptRow({ id, receipt }: { id: string; receipt: Receipt }) {
  const Icon = receipt.approved ? CheckCircle2 : XCircle;
  return (
    <div
      className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-xs ${
        receipt.approved ? "border-success/30 bg-success/5" : "border-border bg-muted/40"
      }`}
      role="status"
      data-testid={`room-approval-receipt-${id}`}
    >
      <Icon className={`mt-px size-3.5 shrink-0 ${receipt.approved ? "text-success" : "text-muted-foreground"}`} aria-hidden />
      <p className="min-w-0 text-foreground">
        <span className="font-medium">{receipt.approved ? "Approved" : "Rejected"}</span>
        {receipt.question ? ` · ${receipt.question}` : ""}
        <span className="block text-muted-foreground">
          {receipt.approved
            ? "It runs once, exactly as shown; its receipt appears in the conversation."
            : "Nothing was sent, and the task stays open."}
        </span>
      </p>
    </div>
  );
}

