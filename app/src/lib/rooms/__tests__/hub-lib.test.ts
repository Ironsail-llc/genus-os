/**
 * The hub lane's library: which rooms are "new", how a room's tasks split
 * into suggested and taken, in-app navigation for workspace links, and the
 * Bridge API calls the hub makes (tasks, accept/dismiss, create, open a task).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  acceptRoomTask,
  createRoom,
  dismissRoomTask,
  fetchRoomTasks,
  openTaskRoom,
} from "@/lib/rooms/api";
import { splitRoomTasks, unreadRooms } from "@/lib/rooms/hub";
import { appRouteOf, navigateTo } from "@/lib/rooms/navigate";
import { roomLink } from "@/lib/rooms/room-id";
import { AGENT_ID, REPORT_ID, TASK_ID, reply, room, summary } from "./fixtures";
import { ROOM_TASKS, TASK_ACCEPTED, TASK_DISMISSED, TASK_OLDER, TASK_SUGGESTED, roomTask } from "./hub-fixtures";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("unreadRooms (C1)", () => {
  it("lists only live rooms with unread activity, newest activity first", () => {
    const rooms = [
      summary({ id: REPORT_ID, unread: 2, lastActivityAt: "2026-11-20T09:00:00Z" }),
      summary({ id: AGENT_ID, kind: "agent", unread: 1, lastActivityAt: "2026-11-21T09:00:00Z" }),
      summary({ id: TASK_ID, kind: "task", unread: 0, lastActivityAt: "2026-11-21T10:00:00Z" }),
      summary({ id: "44444444-4444-4444-8444-444444444444", unread: 5, archived: true }),
    ];
    expect(unreadRooms(rooms).map((r) => r.id)).toEqual([AGENT_ID, REPORT_ID]);
  });

  it("leaves out the room that is open", () => {
    const rooms = [summary({ id: REPORT_ID, unread: 2 }), summary({ id: AGENT_ID, unread: 1 })];
    expect(unreadRooms(rooms, REPORT_ID).map((r) => r.id)).toEqual([AGENT_ID]);
  });

  it("is empty when nothing is new", () => {
    expect(unreadRooms([summary({ unread: 0 })])).toEqual([]);
  });
});

describe("splitRoomTasks", () => {
  it("separates open suggestions from taken tasks and drops dismissed ones", () => {
    const { suggested, taken } = splitRoomTasks(ROOM_TASKS);
    expect(suggested.map((t) => t.taskId)).toEqual([TASK_SUGGESTED, TASK_OLDER]);
    expect(taken.map((t) => t.taskId)).toEqual([TASK_ACCEPTED]);
    expect([...suggested, ...taken].some((t) => t.taskId === TASK_DISMISSED)).toBe(false);
  });

  it("counts a room's subject task as taken work, not a suggestion", () => {
    const { suggested, taken } = splitRoomTasks([roomTask({ origin: "subject", editionId: null })]);
    expect(suggested).toEqual([]);
    expect(taken).toHaveLength(1);
  });
});

describe("in-app links (H1)", () => {
  it("recognises a workspace link and any other dashboard route", () => {
    expect(appRouteOf(roomLink(REPORT_ID))).toBe(`/?v=chat&room=${REPORT_ID}`);
    expect(appRouteOf("/?v=tasks")).toBe("/?v=tasks");
    expect(appRouteOf(`${window.location.origin}/?v=chat&room=${REPORT_ID}`)).toBe(`/?v=chat&room=${REPORT_ID}`);
  });

  it("leaves external links, other paths and anchors alone", () => {
    expect(appRouteOf("https://example.com/?v=chat")).toBeNull();
    expect(appRouteOf("/api/files/1")).toBeNull();
    expect(appRouteOf("#fn-1")).toBeNull();
    expect(appRouteOf("mailto:agent@example.com")).toBeNull();
    expect(appRouteOf(undefined)).toBeNull();
  });

  it("navigates by pushing history and announcing it, without a page load", () => {
    const push = vi.spyOn(window.history, "pushState");
    const heard = vi.fn();
    window.addEventListener("popstate", heard);
    navigateTo(roomLink(REPORT_ID));
    expect(push).toHaveBeenCalledWith(null, "", `/?v=chat&room=${REPORT_ID}`);
    expect(window.location.search).toBe(`?v=chat&room=${REPORT_ID}`);
    expect(heard).toHaveBeenCalledTimes(1);
    window.removeEventListener("popstate", heard);
    push.mockRestore();
  });
});

describe("room tasks API", () => {
  it("reads a room's tasks from either envelope", async () => {
    fetchMock.mockResolvedValueOnce(reply({ tasks: ROOM_TASKS }));
    expect(await fetchRoomTasks(REPORT_ID)).toHaveLength(4);
    expect(fetchMock).toHaveBeenLastCalledWith(`/api/bridge/api/rooms/${REPORT_ID}/tasks`, expect.anything());
    fetchMock.mockResolvedValueOnce(reply(ROOM_TASKS.slice(0, 1)));
    expect(await fetchRoomTasks(REPORT_ID)).toHaveLength(1);
  });

  it("accepts a task and hands back the updated row", async () => {
    const taken = roomTask({ acceptedBy: { userId: "u-bob", displayName: "Bob" } });
    fetchMock.mockResolvedValueOnce(reply(taken));
    const result = await acceptRoomTask(REPORT_ID, TASK_SUGGESTED);
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/bridge/api/rooms/${REPORT_ID}/tasks/${TASK_SUGGESTED}/accept`,
      expect.objectContaining({ method: "POST" }),
    );
    expect(result).toEqual({ ok: true, task: taken });
  });

  it("names who already took a task when the accept is refused (E2)", async () => {
    fetchMock.mockResolvedValueOnce(
      reply({ error: "already accepted", acceptedBy: { userId: "u-alice", displayName: "Alice" } }, 409),
    );
    const result = await acceptRoomTask(REPORT_ID, TASK_SUGGESTED);
    expect(result).toMatchObject({ ok: false, status: 409, takenBy: "Alice" });
  });

  it("reads who took it from the bridge's detail-wrapped 409", async () => {
    fetchMock.mockResolvedValueOnce(
      reply(
        {
          detail: {
            error: "already_accepted",
            message: "Alice already accepted this task.",
            acceptedBy: { userId: "u-alice", displayName: "Alice" },
          },
        },
        409,
      ),
    );
    expect(await acceptRoomTask(REPORT_ID, TASK_SUGGESTED)).toEqual({
      ok: false,
      status: 409,
      takenBy: "Alice",
      message: "Alice already accepted this task.",
    });
  });

  it("dismisses a task", async () => {
    fetchMock.mockResolvedValueOnce(reply(roomTask({ dismissed: true })));
    const result = await dismissRoomTask(REPORT_ID, TASK_SUGGESTED);
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/bridge/api/rooms/${REPORT_ID}/tasks/${TASK_SUGGESTED}/dismiss`,
      expect.objectContaining({ method: "POST" }),
    );
    expect(result.ok).toBe(true);
  });

  it("reports a refusal in the server's words", async () => {
    fetchMock.mockResolvedValueOnce(reply({ detail: "viewers cannot accept tasks" }, 403));
    expect(await dismissRoomTask(REPORT_ID, TASK_SUGGESTED)).toMatchObject({
      ok: false,
      status: 403,
      message: "viewers cannot accept tasks",
    });
  });

  it("reports a lost request as status 0", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("offline"));
    expect(await acceptRoomTask(REPORT_ID, TASK_SUGGESTED)).toMatchObject({ ok: false, status: 0 });
  });
});

describe("creating and opening rooms", () => {
  it("creates an agent room with the agent and title (F1)", async () => {
    fetchMock.mockResolvedValueOnce(reply(room({ id: AGENT_ID, kind: "agent" })));
    const result = await createRoom({ kind: "agent", title: "Research desk", agentId: "researcher" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/bridge/api/rooms");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ kind: "agent", title: "Research desk", agentId: "researcher" });
    expect(result).toMatchObject({ ok: true, room: { id: AGENT_ID } });
  });

  it("says why creation was refused (unknown agent)", async () => {
    fetchMock.mockResolvedValueOnce(reply({ detail: "unknown agent" }, 422));
    // The refusal's own body rides along (the dialog reads a scope refusal from it).
    expect(await createRoom({ kind: "agent", title: "X", agentId: "nobody" })).toMatchObject({
      ok: false,
      status: 422,
      message: "unknown agent",
    });
  });

  it("opens a task as a room (E6)", async () => {
    fetchMock.mockResolvedValueOnce(reply(room({ id: TASK_ID, kind: "task" })));
    const result = await openTaskRoom(TASK_ACCEPTED);
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/bridge/api/rooms/from-task/${TASK_ACCEPTED}`,
      expect.objectContaining({ method: "POST" }),
    );
    expect(result).toMatchObject({ ok: true, room: { id: TASK_ID } });
  });
});
