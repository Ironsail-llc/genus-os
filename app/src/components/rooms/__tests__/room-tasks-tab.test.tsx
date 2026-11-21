/**
 * The tasks tab inside a workspace (the `sections` slot):
 * - E1: an edition's suggestions as suggested tasks, linked to that edition.
 * - E2: Accept; told when someone else already took it; a viewer cannot act.
 * - E5: Dismiss takes it off the list.
 * - E7: a linked task's status changes live on a `room.task` event.
 * - E3: a proposal the agent made for my task, with its exact effect, Approve /
 *   Reject, and a receipt afterwards; a 403 is said plainly.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { StreamEvent } from "@/lib/event-bus/use-event-stream";
import { can } from "@/lib/rooms/access";
import { EDITIONS, EDITION_1, REPORT_ID, reply, room } from "@/lib/rooms/__tests__/fixtures";
import {
  ACTION_ID,
  OTHER_ROOM_PROPOSAL,
  ROOM_PROPOSAL,
  ROOM_TASKS,
  TASK_ACCEPTED,
  TASK_DISMISSED,
  TASK_OLDER,
  TASK_SUGGESTED,
  roomTask,
} from "@/lib/rooms/__tests__/hub-fixtures";
import type { RoomTask } from "@/lib/rooms/hub";
import type { Access, Room } from "@/lib/rooms/types";
import type { RoomSlotContext } from "@/components/rooms/room-slots";

let streamEvents: StreamEvent[] = [];
vi.mock("@/lib/event-bus/use-event-stream", () => ({
  useEventStream: () => ({ events: streamEvents, connected: true, synced: true }),
}));

import { RoomTasksTab } from "@/components/rooms/room-tasks-tab";
import { roomSections } from "@/components/rooms/room-sections";

let tasks: RoomTask[];
let pending: unknown[];
let approvalsStatus: number;
let acceptReply: () => Response;
let answerReply: () => Response;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  streamEvents = [];
  tasks = ROOM_TASKS.map((t) => ({ ...t }));
  pending = [ROOM_PROPOSAL, OTHER_ROOM_PROPOSAL];
  approvalsStatus = 200;
  acceptReply = () => reply(roomTask({ acceptedBy: { userId: "u-bob", displayName: "Bob" } }));
  answerReply = () => reply({ settled: true, kind: "action", id: ACTION_ID });
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url === `/api/bridge/api/rooms/${REPORT_ID}/tasks`) return reply({ tasks });
    const decide = url.match(/\/tasks\/([^/]+)\/(accept|dismiss)$/);
    if (decide && method === "POST") {
      const [, id, verb] = decide;
      if (verb === "dismiss") {
        tasks = tasks.map((t) => (t.taskId === id ? { ...t, dismissed: true } : t));
        return reply(tasks.find((t) => t.taskId === id));
      }
      return acceptReply();
    }
    if (url === "/api/bridge/api/approvals") {
      return approvalsStatus === 200
        ? reply({ count: pending.length, pending })
        : reply({ detail: "operator only" }, approvalsStatus);
    }
    if (url === `/api/bridge/api/approvals/action/${ACTION_ID}` && method === "POST") return answerReply();
    return reply({ error: "unexpected" }, 500);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => vi.unstubAllGlobals());

function context(access: Access = "shared", overrides: Partial<RoomSlotContext> = {}): RoomSlotContext {
  const r: Room = room({ access });
  return {
    room: r,
    access,
    can: (action) => can(access, action),
    edition: EDITIONS[0],
    editions: EDITIONS,
    showEdition: vi.fn(),
    refresh: vi.fn(),
    ...overrides,
  };
}

function wrap(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function roomEvent(id: string, type: string, roomId = REPORT_ID): StreamEvent {
  return {
    id,
    stream: "agent",
    timestamp: "2026-11-21T00:00:00Z",
    type,
    source: "engine",
    actor: "main",
    payload: { room_id: roomId, tenant_id: "test-tenant" },
    correlation_id: "",
  };
}

describe("RoomTasksTab — suggestions (E1, E5)", () => {
  it("lists open suggestions, each linked to the edition it came from", async () => {
    const ctx = context();
    wrap(<RoomTasksTab ctx={ctx} />);
    const suggested = await screen.findByTestId("room-tasks-suggested");
    expect(within(suggested).getByText("Call the three lapsed customers")).toBeInTheDocument();
    expect(within(suggested).getByText("Chase the late invoice")).toBeInTheDocument();
    expect(within(suggested).queryByText("Reprice the bundle")).toBeNull();
    const link = screen.getByTestId(`room-task-edition-${TASK_OLDER}`);
    expect(link).toHaveTextContent("Week 46");
    fireEvent.click(link);
    expect(ctx.showEdition).toHaveBeenCalledWith(expect.objectContaining({ id: EDITION_1 }));
  });

  it("dismisses a suggestion and it leaves the list", async () => {
    wrap(<RoomTasksTab ctx={context()} />);
    fireEvent.click(await screen.findByTestId(`room-task-dismiss-${TASK_SUGGESTED}`));
    await waitFor(() => expect(screen.queryByTestId(`room-task-${TASK_SUGGESTED}`)).toBeNull());
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/bridge/api/rooms/${REPORT_ID}/tasks/${TASK_SUGGESTED}/dismiss`,
      expect.objectContaining({ method: "POST" }),
    );
    expect(screen.queryByTestId(`room-task-${TASK_DISMISSED}`)).toBeNull();
  });

  it("says so when the room has no tasks", async () => {
    tasks = [];
    pending = [];
    wrap(<RoomTasksTab ctx={context()} />);
    expect(await screen.findByTestId("room-tasks-empty")).toHaveTextContent(/No tasks/i);
  });

  it("says the tasks could not be read, rather than showing none", async () => {
    fetchMock.mockImplementation(async () => reply({ error: "down" }, 502));
    wrap(<RoomTasksTab ctx={context()} />);
    expect(await screen.findByTestId("room-tasks-error")).toHaveTextContent("502");
    expect(screen.queryByTestId("room-tasks-empty")).toBeNull();
  });
});

describe("RoomTasksTab — accepting (E2)", () => {
  it("accepts a suggestion", async () => {
    wrap(<RoomTasksTab ctx={context()} />);
    fireEvent.click(await screen.findByTestId(`room-task-accept-${TASK_SUGGESTED}`));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/bridge/api/rooms/${REPORT_ID}/tasks/${TASK_SUGGESTED}/accept`,
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("tells the user when someone else already took it, and does not claim it", async () => {
    acceptReply = () =>
      reply({ error: "already accepted", acceptedBy: { userId: "u-alice", displayName: "Alice" } }, 409);
    wrap(<RoomTasksTab ctx={context()} />);
    fireEvent.click(await screen.findByTestId(`room-task-accept-${TASK_SUGGESTED}`));
    expect(await screen.findByTestId(`room-task-message-${TASK_SUGGESTED}`)).toHaveTextContent(
      /Alice already took this task/,
    );
  });

  it("shows taken tasks with their assignee and status", async () => {
    wrap(<RoomTasksTab ctx={context()} />);
    const row = await screen.findByTestId(`room-task-${TASK_ACCEPTED}`);
    expect(within(screen.getByTestId("room-tasks-taken")).getByTestId(`room-task-${TASK_ACCEPTED}`)).toBe(row);
    expect(row).toHaveTextContent("Bob");
    expect(within(row).getByTestId(`room-task-status-${TASK_ACCEPTED}`)).toHaveTextContent("In progress");
  });

  it("lets a read-only user see the tasks but not act on them", async () => {
    wrap(<RoomTasksTab ctx={context("viewer")} />);
    const accept = await screen.findByTestId(`room-task-accept-${TASK_SUGGESTED}`);
    expect(accept).toBeDisabled();
    expect(screen.getByTestId(`room-task-dismiss-${TASK_SUGGESTED}`)).toBeDisabled();
    expect(screen.getByTestId("room-tasks-readonly")).toHaveTextContent(/read-only/i);
    fireEvent.click(accept);
    expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining("/accept"), expect.anything());
  });
});

describe("RoomTasksTab — live progress (E7)", () => {
  it("shows a linked task's new status when a room.task event arrives, without a reload", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const ctx = context();
    const ui = () => (
      <QueryClientProvider client={client}>
        <RoomTasksTab ctx={ctx} />
      </QueryClientProvider>
    );
    const { rerender } = render(ui());
    expect(await screen.findByTestId(`room-task-status-${TASK_ACCEPTED}`)).toHaveTextContent("In progress");

    tasks = tasks.map((t) => (t.taskId === TASK_ACCEPTED ? { ...t, status: "DONE" } : t));
    streamEvents = [roomEvent("e-1", "room.task")];
    await act(async () => rerender(ui()));
    await waitFor(() => expect(screen.getByTestId(`room-task-status-${TASK_ACCEPTED}`)).toHaveTextContent("Done"));
  });

  it("ignores another room's events", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const ctx = context();
    const ui = () => (
      <QueryClientProvider client={client}>
        <RoomTasksTab ctx={ctx} />
      </QueryClientProvider>
    );
    const { rerender } = render(ui());
    await screen.findByTestId(`room-task-status-${TASK_ACCEPTED}`);
    const reads = () => fetchMock.mock.calls.filter(([u]) => String(u).endsWith("/tasks")).length;
    const before = reads();
    streamEvents = [roomEvent("e-2", "room.task", "99999999-9999-4999-8999-999999999999")];
    await act(async () => rerender(ui()));
    expect(reads()).toBe(before);
  });
});

describe("RoomTasksTab — approving my own action (E3)", () => {
  it("shows the exact effect of this room's proposal, and none from other rooms", async () => {
    wrap(<RoomTasksTab ctx={context()} />);
    const card = await screen.findByTestId(`inbox-card-${ACTION_ID}`);
    expect(card).toHaveTextContent("alice@example.com");
    expect(card).toHaveTextContent("Just checking the order arrived safely.");
    expect(screen.queryByText("email.send: Somewhere else")).toBeNull();
    expect(within(card).getByTestId(`inbox-approve-${ACTION_ID}`)).toBeInTheDocument();
    expect(within(card).getByTestId(`inbox-reject-${ACTION_ID}`)).toBeInTheDocument();
  });

  it("approves, then shows a receipt in place of the card", async () => {
    wrap(<RoomTasksTab ctx={context()} />);
    fireEvent.click(await screen.findByTestId(`inbox-approve-${ACTION_ID}`));
    const receipt = await screen.findByTestId(`room-approval-receipt-${ACTION_ID}`);
    expect(receipt).toHaveTextContent(/Approved/);
    expect(receipt).toHaveTextContent("Following up on your order");
    expect(screen.queryByTestId(`inbox-card-${ACTION_ID}`)).toBeNull();
    const [, init] = fetchMock.mock.calls.find(([u]) => String(u).endsWith(`/action/${ACTION_ID}`))!;
    expect(JSON.parse(String(init.body))).toEqual({ approved: true });
  });

  it("rejects, and says nothing was sent", async () => {
    wrap(<RoomTasksTab ctx={context()} />);
    fireEvent.click(await screen.findByTestId(`inbox-reject-${ACTION_ID}`));
    const receipt = await screen.findByTestId(`room-approval-receipt-${ACTION_ID}`);
    expect(receipt).toHaveTextContent(/Rejected/);
    expect(receipt).toHaveTextContent(/nothing was sent/i);
  });

  it("says a 403 plainly and keeps the card", async () => {
    answerReply = () => reply({ message: "Only the person this task is assigned to may decide it." }, 403);
    wrap(<RoomTasksTab ctx={context()} />);
    fireEvent.click(await screen.findByTestId(`inbox-approve-${ACTION_ID}`));
    expect(await screen.findByTestId(`inbox-message-${ACTION_ID}`)).toHaveTextContent(
      "Only the person this task is assigned to may decide it.",
    );
    expect(screen.getByTestId(`inbox-card-${ACTION_ID}`)).toBeInTheDocument();
  });

  it("shows no proposals, and no error, when the approvals listing is not this user's", async () => {
    approvalsStatus = 403;
    wrap(<RoomTasksTab ctx={context()} />);
    await screen.findByTestId(`room-task-${TASK_ACCEPTED}`);
    expect(screen.queryByTestId(`inbox-card-${ACTION_ID}`)).toBeNull();
    expect(screen.queryByTestId("room-approvals")).toBeNull();
  });
});

describe("roomSections", () => {
  it("draws the tasks tab in report and task rooms, and nothing in agent rooms", () => {
    expect(roomSections(context())).not.toBeNull();
    expect(roomSections(context("shared", { room: room({ kind: "task" }) }))).not.toBeNull();
    expect(roomSections(context("shared", { room: room({ kind: "agent" }) }))).toBeNull();
  });
});
