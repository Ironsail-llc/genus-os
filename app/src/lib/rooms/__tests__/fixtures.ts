/**
 * Rooms fixtures shaped exactly like the shared-threads contract's Bridge API
 * (camelCase, as the rest of the bridge). Generic people only: Alice and Bob.
 */
import type { Edition, Room, RoomSummary } from "@/lib/rooms/types";

export const REPORT_ID = "11111111-1111-4111-8111-111111111111";
export const AGENT_ID = "22222222-2222-4222-8222-222222222222";
export const TASK_ID = "33333333-3333-4333-8333-333333333333";
export const ARCHIVED_ID = "44444444-4444-4444-8444-444444444444";
export const EMPTY_REPORT_ID = "55555555-5555-4555-8555-555555555555";
export const EDITION_1 = "aaaaaaaa-0000-4000-8000-000000000001";
export const EDITION_2 = "aaaaaaaa-0000-4000-8000-000000000002";

export function summary(overrides: Partial<RoomSummary> = {}): RoomSummary {
  return {
    id: REPORT_ID,
    kind: "report",
    title: "Weekly sales",
    agentId: "main",
    paused: false,
    archived: false,
    unread: 0,
    lastActivityAt: "2026-11-20T09:00:00Z",
    ...overrides,
  };
}

export const SUMMARIES: RoomSummary[] = [
  summary({
    unread: 3,
    latestEdition: {
      id: EDITION_2,
      seq: 2,
      title: "Week 47",
      periodStart: "2026-11-16",
      periodEnd: "2026-11-22",
    },
  }),
  summary({ id: AGENT_ID, kind: "agent", title: "Research desk", unread: 0 }),
  summary({ id: TASK_ID, kind: "task", title: "Renew the lease", paused: true }),
  summary({ id: ARCHIVED_ID, kind: "report", title: "Old pipeline", archived: true }),
];

export function room(overrides: Partial<Room> = {}): Room {
  return {
    ...summary(),
    instructions: "Summarise the week's sales.",
    settings: { tz: "UTC", default_grain: "week" },
    rev: 3,
    access: "assigned",
    runsAs: { userId: "u-alice", displayName: "Alice" },
    members: [
      { userId: "u-alice", displayName: "Alice", assigned: true },
      { userId: "u-bob", displayName: "Bob", assigned: false },
    ],
    nextRunAt: "2026-11-23T08:00:00Z",
    ...overrides,
  };
}

export function edition(overrides: Partial<Edition> = {}): Edition {
  return {
    id: EDITION_2,
    seq: 2,
    kind: "edition",
    status: "published",
    preview: false,
    periodStart: "2026-11-16",
    periodEnd: "2026-11-22",
    grain: "week",
    revision: 3,
    title: "Week 47",
    summary: "Revenue up on the week.",
    blocks: [
      {
        component: "render_bar_chart",
        props: { title: "Revenue by day", data: [{ name: "Mon", value: 3 }], dataKey: "value" },
      },
      { component: "render_markdown", props: { content: "Strong finish." } },
    ],
    suggestions: [],
    provenance: { run_id: "run-1", ran_as: "u-alice", started_at: "2026-11-22T08:00:00Z", tool_calls: [] },
    createdAt: "2026-11-22T08:05:00Z",
    ...overrides,
  };
}

export const EDITIONS: Edition[] = [
  edition(),
  edition({ id: EDITION_1, seq: 1, title: "Week 46", periodStart: "2026-11-09", periodEnd: "2026-11-15" }),
];

/** A minimal fetch Response stand-in. */
export function reply(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
