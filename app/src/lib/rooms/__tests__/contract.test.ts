/**
 * The dashboard's API client against what the bridge REALLY answers.
 *
 * `contract/bridge-replies.json` is recorded from the real bridge routes over
 * the real definition module and Postgres
 * (`crm/bridge/tests/test_rooms_definition_seam.py`, which fails when a reply
 * changes). Here every recorded reply goes through the client the dashboard
 * uses, so a bridge that renames or drops a field, or a client that stops
 * reading one, fails a test. Ids are recorded as names (`room-report`,
 * `u-bob`), times as `<time>`.
 *
 * The key lists are checked against the TypeScript types at compile time: a
 * required field added to a type must be added here, and is then required of
 * the recorded reply.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import recorded from "./contract/bridge-replies.json";
import {
  RoomRequestError,
  acceptRoomTask,
  archiveRoom,
  createRoom,
  duplicateRoom,
  fetchAllRooms,
  fetchEdition,
  fetchEditions,
  fetchNotify,
  fetchPeople,
  fetchRevisions,
  fetchRoom,
  fetchRoomTasks,
  fetchRooms,
  openTaskRoom,
  patchRoom,
  postRoomRead,
  putNotify,
  reassignRoom,
  restoreRevision,
  unarchiveRoom,
} from "../api";
import type { RoomTask } from "../hub";
import { readProvenance } from "@/components/rooms/provenance-drawer";
import { isScopeRefusal } from "@/components/rooms/new-workspace-dialog";
import type { Edition, EditionHeadline, Room, RoomMember, RoomSummary } from "../types";

type Recorded = { status: number; body: unknown };
const replies = recorded as Record<string, Recorded>;

/** The keys of `T` that are not optional. */
type RequiredKeys<T> = { [K in keyof T]-?: undefined extends T[K] ? never : K }[keyof T];
/** Compiles only when `List` names every required key of `T`. */
type Covers<T, List extends readonly (keyof T)[]> = Exclude<RequiredKeys<T>, List[number]> extends never ? List : never;

const SUMMARY_KEYS = ["id", "kind", "title", "agentId", "paused", "archived", "unread", "lastActivityAt"] as const;
const ROOM_KEYS = [...SUMMARY_KEYS, "instructions", "settings", "rev", "access", "members"] as const;
const HEADLINE_KEYS = ["id", "seq", "title", "periodStart", "periodEnd"] as const;
const EDITION_KEYS = [
  "id", "seq", "kind", "status", "preview", "periodStart", "periodEnd", "grain", "revision",
  "title", "summary", "blocks", "suggestions", "provenance", "createdAt",
] as const;
const MEMBER_KEYS = ["userId", "displayName", "assigned"] as const;
const TASK_KEYS = ["taskId", "title", "status", "origin", "editionId", "assignee", "acceptedBy", "dismissed"] as const;

const covered: [
  Covers<RoomSummary, typeof SUMMARY_KEYS>,
  Covers<Room, typeof ROOM_KEYS>,
  Covers<EditionHeadline, typeof HEADLINE_KEYS>,
  Covers<Edition, typeof EDITION_KEYS>,
  Covers<RoomMember, typeof MEMBER_KEYS>,
  Covers<RoomTask, typeof TASK_KEYS>,
] = [SUMMARY_KEYS, ROOM_KEYS, HEADLINE_KEYS, EDITION_KEYS, MEMBER_KEYS, TASK_KEYS];

function hasKeys(value: unknown, keys: readonly string[]) {
  expect(value && typeof value === "object").toBe(true);
  const missing = keys.filter((k) => !(k in (value as object)));
  expect(missing).toEqual([]);
}

function reply(name: string): Response {
  const r = replies[name];
  if (!r) throw new Error(`no recorded reply ${name}`);
  return new Response(JSON.stringify(r.body), { status: r.status, headers: { "Content-Type": "application/json" } });
}

/** The next fetch answers with the recorded reply `name`. */
function answer(...names: string[]) {
  const fetchMock = vi.fn();
  for (const name of names) fetchMock.mockResolvedValueOnce(reply(name));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function refusal(promise: Promise<unknown>): Promise<RoomRequestError> {
  try {
    await promise;
  } catch (err) {
    expect(err).toBeInstanceOf(RoomRequestError);
    return err as RoomRequestError;
  }
  throw new Error("expected a refusal");
}

afterEach(() => vi.unstubAllGlobals());

describe("the recorded bridge replies", () => {
  it("are all present", () => {
    expect(covered).toHaveLength(6);
    expect(Object.keys(replies).length).toBeGreaterThan(30);
    expect(JSON.stringify(replies)).not.toMatch(/:room:/);
  });
});

describe("reads", () => {
  it("lists a member's workspaces as summaries with their latest edition", async () => {
    answer("list_mine");
    const rooms = await fetchRooms();
    expect(rooms.map((r) => r.id)).toContain("room-report");
    for (const room of rooms) hasKeys(room, SUMMARY_KEYS);
    const report = rooms.find((r) => r.id === "room-report")!;
    hasKeys(report.latestEdition, HEADLINE_KEYS);
    expect(report.latestEdition).toMatchObject({ id: "ed-published", periodStart: "2026-11-02", periodEnd: "2026-11-08" });
    expect(report.unread).toBeGreaterThan(0);
  });

  it("lists every workspace for an admin, with members", async () => {
    answer("list_all");
    const rows = await fetchAllRooms();
    const report = rows.find((r) => r.id === "room-report")!;
    hasKeys(report, SUMMARY_KEYS);
    for (const m of report.members ?? []) hasKeys(m, MEMBER_KEYS);
    expect(report.members?.map((m) => m.displayName)).toEqual(expect.arrayContaining(["Alice", "Bob"]));
  });

  it("reads one workspace with the caller's access, members and run-as", async () => {
    answer("room_shared", "room_admin");
    const shared = await fetchRoom("room-report");
    hasKeys(shared, ROOM_KEYS);
    expect(shared.access).toBe("shared");
    expect(shared.runsAs).toEqual({ userId: "u-alice", displayName: "Alice" });
    expect(shared.settings).toMatchObject({ tz: "UTC", default_grain: "week", schedule_text: "0 7 * * *" });
    expect(shared.nextRunAt).toBe("<time>");
    const admin = await fetchRoom("room-report");
    expect(admin.access).toBe("admin");
  });

  it("refuses a workspace the caller cannot see as a 404", async () => {
    answer("room_not_found");
    await expect(fetchRoom("room-report")).rejects.toMatchObject({ status: 404 });
  });

  it("lists editions newest first, failed and building ones included, no one else's preview", async () => {
    answer("editions");
    const editions = await fetchEditions("room-report");
    for (const e of editions) hasKeys(e, EDITION_KEYS);
    expect(editions.map((e) => e.seq)).toEqual([...editions.map((e) => e.seq)].sort((a, b) => b - a));
    expect(editions.some((e) => e.preview)).toBe(false);
    expect(new Set(editions.map((e) => e.status))).toEqual(new Set(["published", "failed", "building"]));
    const failed = editions.find((e) => e.status === "failed") as Edition & { error?: string };
    expect(failed.error).toBe("the sales tool timed out");
    const published = editions.find((e) => e.status === "published") as Edition & { publishedSeq?: number };
    expect(published.publishedSeq).toBe(1);
  });

  it("reads one edition with its blocks, suggestions and provenance as stored", async () => {
    answer("edition");
    const e = await fetchEdition("room-report", "ed-published");
    hasKeys(e, EDITION_KEYS);
    expect(e.blocks[0]).toEqual({ component: "render_markdown", props: { content: "Up 4%." } });
    expect(e.suggestions[0]).toMatchObject({ key: "call-lapsed", title: "Call lapsed buyers" });
    expect(e.provenance).toMatchObject({ run_id: "run-1", ran_as: "u-alice" });
  });

  it("shows the run's cost and tokens as the engine records them", async () => {
    answer("room_shared", "edition");
    const room = await fetchRoom("room-report");
    const e = await fetchEdition("room-report", "ed-published");
    const shown = readProvenance(e, room.members);
    expect(shown).toMatchObject({ runId: "run-1", ranAs: "Alice", costUsd: 0.0421 });
    expect(shown?.tokens).toEqual({ total: 12345, input: 10000, output: 2345 });
    expect(shown?.toolCalls.map((c) => c.tool)).toEqual(["sample_sales_figures"]);
  });

  it("reads revisions newest first with who made them", async () => {
    answer("revisions");
    const revisions = await fetchRevisions("room-report");
    expect(revisions[0].rev).toBeGreaterThan(revisions[revisions.length - 1].rev);
    expect(revisions[0]).toMatchObject({ changedByName: "Alice", changedAt: "<time>" });
    expect(revisions[0].definition).toHaveProperty("settings");
  });

  it("reads the share directory by name only", async () => {
    answer("directory");
    const people = await fetchPeople();
    expect(people.map((p) => p.displayName)).toEqual(expect.arrayContaining(["Alice", "Bob", "Vera"]));
    expect(JSON.stringify(replies.directory.body)).not.toMatch(/@|role/);
  });

  it("reads and saves the caller's notification choices", async () => {
    answer("notify", "notify_put");
    expect(await fetchNotify("room-report")).toEqual({ inApp: true, muted: false, channels: {}, linkedChannels: [] });
    await expect(putNotify("room-report", { inApp: false, muted: true, channels: {} })).resolves.toMatchObject({ muted: true });
  });

  it("refuses a channel the caller has not linked, naming the field", async () => {
    answer("notify_unlinked");
    const err = await refusal(putNotify("room-report", { inApp: true, muted: false, channels: { telegram: true } }));
    expect(err).toMatchObject({ status: 422, code: "channel_not_linked", field: "channels" });
  });

  it("reads a workspace's tasks", async () => {
    answer("tasks");
    const tasks = await fetchRoomTasks("room-report");
    for (const t of tasks) hasKeys(t, TASK_KEYS);
    expect(tasks[0]).toMatchObject({ taskId: "task-suggested", origin: "suggestion", editionId: "ed-published", acceptedBy: null });
  });

  it("marks read", async () => {
    answer("read");
    expect(await postRoomRead("room-report")).toBe(true);
  });
});

describe("writes and refusals", () => {
  it("creates an agent workspace with the hub's body", async () => {
    const fetchMock = answer("create_agent");
    const made = await createRoom({ kind: "agent", title: "Pricing", agentId: "analyst" });
    expect(made.ok).toBe(true);
    if (made.ok) hasKeys(made.room, ROOM_KEYS);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ kind: "agent", title: "Pricing", agentId: "analyst" });
  });

  it("says why an unknown agent is refused", async () => {
    answer("create_unknown_agent");
    const made = await createRoom({ kind: "agent", title: "Pricing", agentId: "ghost" });
    expect(made).toMatchObject({ ok: false, status: 422, message: "That agent is not available in this company." });
  });

  it("reads a viewer's refused create as the role not allowing it", async () => {
    // The bridge middleware's scope check answers before the router, in its own shape.
    expect(replies.create_viewer).toMatchObject({ status: 403, body: { error: "insufficient scope" } });
    answer("create_viewer");
    const made = await createRoom({ kind: "agent", title: "Pricing", agentId: "main" });
    expect(made).toMatchObject({ ok: false, status: 403 });
    // The new-workspace dialog reads exactly this refusal as "your role cannot create".
    if (!made.ok) expect(isScopeRefusal(made.status, made.body)).toBe(true);
  });

  it("saves a change and reads the new revision", async () => {
    answer("patch_ok");
    const saved = await patchRoom("room-report", 1, { title: "Sales" });
    expect(saved).toMatchObject({ title: "Sales", rev: 2 });
  });

  it("reads a stale form as a conflict with the current revision", async () => {
    answer("patch_conflict");
    const err = await refusal(patchRoom("room-report", 1, { title: "Late" }));
    expect(err).toMatchObject({ status: 409, code: "revision_conflict", currentRev: 2 });
  });

  it("puts a schedule refusal on the schedule field, in the server's words", async () => {
    answer("patch_invalid_schedule", "create_invalid_schedule");
    const err = await refusal(patchRoom("room-report", 2, { scheduleText: "in 2 hours" }));
    expect(err).toMatchObject({ status: 422, code: "invalid_schedule", field: "scheduleText" });
    expect(err.serverMessage).toMatch(/schedule could not be understood/);
  });

  it("refuses a shared user's edit and offers the duplicate", async () => {
    answer("patch_forbidden");
    const err = await refusal(patchRoom("room-report", 2, { title: "Mine" }));
    expect(err).toMatchObject({ status: 403, code: "forbidden" });
    expect(err.message).toMatch(/duplicate/);
  });

  it("accepts a task, and names who took one already", async () => {
    answer("task_accept", "task_accept_taken");
    const taken = await acceptRoomTask("room-report", "task-suggested");
    expect(taken.ok).toBe(true);
    if (taken.ok) expect(taken.task).toMatchObject({ acceptedBy: { userId: "u-bob", displayName: "Bob" } });
    const late = await acceptRoomTask("room-report", "task-suggested");
    expect(late).toMatchObject({ ok: false, status: 409, takenBy: "Bob" });
  });

  it("opens a task as a workspace", async () => {
    answer("from_task");
    const opened = await openTaskRoom("task-opened");
    expect(opened.ok).toBe(true);
    if (opened.ok) expect(opened.room).toMatchObject({ kind: "task", access: "assigned" });
  });

  it("duplicates, restores, reassigns, archives and unarchives", async () => {
    answer("duplicate", "restore", "reassign", "archive", "unarchive");
    const copy = await duplicateRoom("room-report");
    expect(copy).toMatchObject({ access: "assigned", paused: true, runsAs: { displayName: "Bob" } });
    expect(copy.id).not.toBe("room-report");
    expect(await restoreRevision("room-report", 1)).toMatchObject({ id: "room-report" });
    await expect(reassignRoom("room-report", "u-bob")).resolves.toMatchObject({ runsAs: { userId: "u-bob" } });
    await expect(archiveRoom("room-report")).resolves.toMatchObject({ archived: true });
    await expect(unarchiveRoom("room-report")).resolves.toMatchObject({ archived: false, paused: true, nextRunAt: null });
  });
});
