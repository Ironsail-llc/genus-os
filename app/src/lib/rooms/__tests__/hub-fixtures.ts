/**
 * Fixtures for the hub lane: a room's tasks, proposals waiting on the caller,
 * and the caller's notifications. Shaped as the shared-threads contract and the
 * existing bridge routes answer (rooms camelCase; approvals snake_case, as
 * `crm/bridge/routers/approvals.py` writes them). Generic people only.
 */
import type { RoomTask } from "@/lib/rooms/hub";
import { EDITION_1, EDITION_2, REPORT_ID } from "./fixtures";

export const TASK_SUGGESTED = "bbbbbbbb-0000-4000-8000-000000000001";
export const TASK_ACCEPTED = "bbbbbbbb-0000-4000-8000-000000000002";
export const TASK_DISMISSED = "bbbbbbbb-0000-4000-8000-000000000003";
export const TASK_OLDER = "bbbbbbbb-0000-4000-8000-000000000004";
export const ACTION_ID = "cccccccc-0000-4000-8000-000000000001";
export const NOTE_ID = "dddddddd-0000-4000-8000-000000000001";
export const ME_ID = "eeeeeeee-0000-4000-8000-00000000000b";

export function roomTask(overrides: Partial<RoomTask> = {}): RoomTask {
  return {
    taskId: TASK_SUGGESTED,
    title: "Call the three lapsed customers",
    status: "TODO",
    origin: "suggestion",
    editionId: EDITION_2,
    assignee: null,
    acceptedBy: null,
    dismissed: false,
    ...overrides,
  };
}

export const ROOM_TASKS: RoomTask[] = [
  roomTask(),
  roomTask({
    taskId: TASK_ACCEPTED,
    title: "Send the follow-up emails",
    status: "IN_PROGRESS",
    assignee: { userId: "u-bob", displayName: "Bob" },
    acceptedBy: { userId: "u-bob", displayName: "Bob" },
  }),
  roomTask({ taskId: TASK_DISMISSED, title: "Reprice the bundle", dismissed: true }),
  roomTask({ taskId: TASK_OLDER, title: "Chase the late invoice", editionId: EDITION_1 }),
];

/** A proposal the agent made in this room, for a task assigned to the caller. */
export const ROOM_PROPOSAL = {
  kind: "action",
  id: ACTION_ID,
  run_id: "",
  agent_id: "",
  action_kind: "email.send",
  question: "email.send: Following up on your order",
  detail: "Hi Alice,\n\nJust checking the order arrived safely.\n\nBob",
  envelope: { sender: "agent@example.com", recipient: "alice@example.com", subject: "Following up on your order" },
  options: [],
  expires_at: "2099-01-01T12:00:00Z",
  created_at: "2026-11-21T09:00:00Z",
  room_id: REPORT_ID,
  task_id: TASK_ACCEPTED,
};

/** One from another room: never shown in this one. */
export const OTHER_ROOM_PROPOSAL = {
  ...ROOM_PROPOSAL,
  id: "cccccccc-0000-4000-8000-000000000002",
  question: "email.send: Somewhere else",
  room_id: "99999999-9999-4999-8999-999999999999",
};

/** A workspace note in the caller's notifications (`notification_to_dict`). */
export function note(overrides: Record<string, unknown> = {}) {
  return {
    id: NOTE_ID,
    tenantId: "test-tenant",
    fromAgent: "main",
    toAgent: ME_ID,
    notificationType: "info",
    subject: "New edition: Week 47",
    body: "Weekly sales has a new edition.",
    metadata: { room_id: REPORT_ID },
    taskId: null,
    readAt: null,
    acknowledgedAt: null,
    createdAt: "2026-11-21T08:05:00Z",
    ...overrides,
  };
}
