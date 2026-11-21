/**
 * The rules under the workspace controls: how a period is worked out from a
 * day the user picks, which periods are refused before any request, how a
 * stored edition is found for a period, and how the controls talk to the
 * rooms Bridge API (the bodies they send, and what a refusal carries back).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  RoomRequestError,
  archiveRoom,
  deleteMember,
  duplicateRoom,
  fetchAllRooms,
  fetchNotify,
  fetchPeople,
  fetchRevisions,
  patchRoom,
  putMember,
  putNotify,
  reassignRoom,
  requestPeriod,
  restoreRevision,
  runRoom,
  unarchiveRoom,
} from "@/lib/rooms/api";
import {
  customPeriod,
  findStoredEdition,
  isFuturePeriod,
  isUnfinishedPeriod,
  lastCompletePeriod,
  resolvePeriod,
  todayIn,
} from "@/lib/rooms/periods";
import { EDITIONS, REPORT_ID, edition, reply, room } from "./fixtures";

describe("periods", () => {
  it("resolves a day to itself", () => {
    expect(resolvePeriod("day", "2026-11-18")).toEqual({ grain: "day", start: "2026-11-18", end: "2026-11-18" });
  });

  it("resolves a week to Monday through Sunday", () => {
    expect(resolvePeriod("week", "2026-11-18")).toEqual({ grain: "week", start: "2026-11-16", end: "2026-11-22" });
    expect(resolvePeriod("week", "2026-11-16")).toEqual({ grain: "week", start: "2026-11-16", end: "2026-11-22" });
    expect(resolvePeriod("week", "2026-11-22")).toEqual({ grain: "week", start: "2026-11-16", end: "2026-11-22" });
    // Across a month and a year boundary.
    expect(resolvePeriod("week", "2027-01-01")).toEqual({ grain: "week", start: "2026-12-28", end: "2027-01-03" });
  });

  it("resolves a month to its first and last day", () => {
    expect(resolvePeriod("month", "2026-11-18")).toEqual({ grain: "month", start: "2026-11-01", end: "2026-11-30" });
    expect(resolvePeriod("month", "2028-02-10")).toEqual({ grain: "month", start: "2028-02-01", end: "2028-02-29" });
  });

  it("takes a custom range only when it runs forwards", () => {
    expect(customPeriod("2026-11-01", "2026-11-10")).toEqual({ grain: "custom", start: "2026-11-01", end: "2026-11-10" });
    expect(customPeriod("2026-11-10", "2026-11-01")).toBeNull();
    expect(customPeriod("", "2026-11-01")).toBeNull();
  });

  it("reads today in the workspace's time zone", () => {
    const now = new Date("2026-11-21T23:30:00Z");
    expect(todayIn("UTC", now)).toBe("2026-11-21");
    expect(todayIn("Asia/Tokyo", now)).toBe("2026-11-22");
    expect(todayIn("America/Los_Angeles", now)).toBe("2026-11-21");
    // An unknown zone falls back to UTC rather than throwing.
    expect(todayIn("Not/AZone", now)).toBe("2026-11-21");
  });

  it("refuses a period that has not started, and a custom range that ends in the future", () => {
    expect(isFuturePeriod(resolvePeriod("day", "2026-11-22"), "2026-11-21")).toBe(true);
    expect(isFuturePeriod(resolvePeriod("day", "2026-11-21"), "2026-11-21")).toBe(false);
    expect(isFuturePeriod(resolvePeriod("week", "2026-11-23"), "2026-11-21")).toBe(true);
    expect(isFuturePeriod(resolvePeriod("week", "2026-11-18"), "2026-11-21")).toBe(false);
    expect(isFuturePeriod(resolvePeriod("month", "2026-12-01"), "2026-11-21")).toBe(true);
    expect(isFuturePeriod(customPeriod("2026-11-10", "2026-11-25")!, "2026-11-21")).toBe(true);
    expect(isFuturePeriod(customPeriod("2026-11-10", "2026-11-21")!, "2026-11-21")).toBe(false);
  });

  it("treats a period that has not ended as unfinished, as the engine does", () => {
    // The engine serves only a period whose last day is before today.
    expect(isUnfinishedPeriod(resolvePeriod("day", "2026-11-21"), "2026-11-21")).toBe(true);
    expect(isUnfinishedPeriod(resolvePeriod("day", "2026-11-20"), "2026-11-21")).toBe(false);
    expect(isUnfinishedPeriod(resolvePeriod("week", "2026-11-18"), "2026-11-21")).toBe(true);
    expect(isUnfinishedPeriod(resolvePeriod("week", "2026-11-11"), "2026-11-21")).toBe(false);
    expect(isUnfinishedPeriod(resolvePeriod("month", "2026-11-01"), "2026-11-21")).toBe(true);
    expect(isUnfinishedPeriod(resolvePeriod("month", "2026-10-31"), "2026-11-21")).toBe(false);
    expect(isUnfinishedPeriod(customPeriod("2026-11-10", "2026-11-21")!, "2026-11-21")).toBe(true);
    expect(isUnfinishedPeriod(customPeriod("2026-11-10", "2026-11-20")!, "2026-11-21")).toBe(false);
    expect(isUnfinishedPeriod(resolvePeriod("day", "2026-11-22"), "2026-11-21")).toBe(true);
  });

  it("offers the last complete period by default", () => {
    expect(lastCompletePeriod("day", "2026-11-21")).toEqual({ grain: "day", start: "2026-11-20", end: "2026-11-20" });
    expect(lastCompletePeriod("week", "2026-11-21")).toEqual({ grain: "week", start: "2026-11-09", end: "2026-11-15" });
    expect(lastCompletePeriod("month", "2026-11-21")).toEqual({ grain: "month", start: "2026-10-01", end: "2026-10-31" });
  });

  it("finds the stored, published, non-preview edition for exactly that period", () => {
    const week47 = resolvePeriod("week", "2026-11-18");
    expect(findStoredEdition(EDITIONS, week47)?.id).toBe(EDITIONS[0].id);
    expect(findStoredEdition(EDITIONS, resolvePeriod("day", "2026-11-18"))).toBeNull();
    const preview = edition({ id: "e-preview", preview: true, periodStart: "2026-11-02", periodEnd: "2026-11-08" });
    const building = edition({ id: "e-building", status: "building", periodStart: "2026-10-26", periodEnd: "2026-11-01" });
    expect(findStoredEdition([preview], resolvePeriod("week", "2026-11-04"))).toBeNull();
    expect(findStoredEdition([building], resolvePeriod("week", "2026-10-28"))).toBeNull();
  });
});

describe("rooms API: the controls' requests", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let answer: () => Response;

  beforeEach(() => {
    answer = () => reply(room());
    fetchMock = vi.fn(async () => answer());
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  function lastCall() {
    const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit | undefined];
    return { url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined };
  }
  const base = `/api/bridge/api/rooms/${REPORT_ID}`;

  it("saves a definition change with the revision the form was read at", async () => {
    const saved = await patchRoom(REPORT_ID, 3, { title: "Weekly revenue", scheduleText: "every Monday at 08:00" });
    expect(saved.id).toBe(REPORT_ID);
    expect(lastCall()).toEqual({
      url: base,
      method: "PATCH",
      body: { expectedRev: 3, changes: { title: "Weekly revenue", scheduleText: "every Monday at 08:00" } },
    });
  });

  it("reports a stale form as a conflict with the current revision, and never retries", async () => {
    answer = () => reply({ detail: { error: "revision_conflict", currentRev: 4 } }, 409);
    const err = await patchRoom(REPORT_ID, 3, { title: "x" }).catch((e) => e);
    expect(err).toBeInstanceOf(RoomRequestError);
    expect(err.status).toBe(409);
    expect(err.currentRev).toBe(4);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("carries a validation refusal's message and field", async () => {
    answer = () => reply({ detail: { error: "invalid_schedule", message: "Runs at most every 15 minutes", field: "scheduleText" } }, 422);
    const err = await patchRoom(REPORT_ID, 3, { scheduleText: "every minute" }).catch((e) => e);
    expect(err.status).toBe(422);
    expect(err.message).toBe("Runs at most every 15 minutes");
    expect(err.field).toBe("scheduleText");
  });

  it("works out the field from a plain FastAPI detail string", async () => {
    answer = () => reply({ detail: "Invalid schedule: could not read 'whenever'" }, 422);
    const err = await patchRoom(REPORT_ID, 3, { scheduleText: "whenever" }).catch((e) => e);
    expect(err.message).toBe("Invalid schedule: could not read 'whenever'");
    expect(err.field).toBe("scheduleText");
  });

  it("starts a run, or a preview only the requester sees", async () => {
    answer = () => reply(edition({ id: "e-new", status: "building" }));
    expect((await runRoom(REPORT_ID))?.id).toBe("e-new");
    expect(lastCall()).toEqual({ url: `${base}/run`, method: "POST", body: {} });
    await runRoom(REPORT_ID, { preview: true });
    expect(lastCall().body).toEqual({ preview: true });
  });

  it("accepts a run answer wrapped as {edition}, or with no edition at all", async () => {
    answer = () => reply({ edition: edition({ id: "e-wrapped", status: "building" }) });
    expect((await runRoom(REPORT_ID))?.id).toBe("e-wrapped");
    answer = () => reply({ ok: true }, 202);
    expect(await runRoom(REPORT_ID)).toBeNull();
  });

  it("asks for a period by grain and anchor, a custom range with its end", async () => {
    answer = () => reply(edition({ id: "e-day", grain: "day", status: "building" }));
    await requestPeriod(REPORT_ID, { grain: "day", start: "2026-11-18", end: "2026-11-18" });
    expect(lastCall()).toEqual({ url: `${base}/periods`, method: "POST", body: { grain: "day", anchor: "2026-11-18" } });
    await requestPeriod(REPORT_ID, { grain: "custom", start: "2026-11-01", end: "2026-11-10" });
    expect(lastCall().body).toEqual({ grain: "custom", anchor: "2026-11-01", end: "2026-11-10" });
  });

  it("names the daily on-demand cap when the server reports it", async () => {
    answer = () => reply({ detail: { error: "on_demand_cap", message: "3 on-demand runs a day", limit: 3 } }, 429);
    const err = await requestPeriod(REPORT_ID, { grain: "day", start: "2026-11-18", end: "2026-11-18" }).catch((e) => e);
    expect(err).toBeInstanceOf(RoomRequestError);
    expect(err.isCap).toBe(true);
    expect(err.limit).toBe(3);
  });

  it("reads revisions newest first, in either envelope", async () => {
    answer = () =>
      reply({
        revisions: [
          { rev: 1, changedBy: "u-alice", changedByName: "Alice", changedAt: "2026-11-01T09:00:00Z", definition: { title: "A" } },
          { rev: 2, changedBy: "u-alice", changedByName: "Alice", changedAt: "2026-11-02T09:00:00Z", definition: { title: "B" } },
        ],
      });
    expect((await fetchRevisions(REPORT_ID)).map((r) => r.rev)).toEqual([2, 1]);
    expect(lastCall().url).toBe(`${base}/revisions`);
    answer = () => reply([{ rev: 5, changedBy: "u-bob", changedAt: "2026-11-02T09:00:00Z", definition: {} }]);
    expect((await fetchRevisions(REPORT_ID))[0].rev).toBe(5);
  });

  it("restores a revision, archives, unarchives and duplicates", async () => {
    await restoreRevision(REPORT_ID, 2);
    expect(lastCall()).toMatchObject({ url: `${base}/revisions/2/restore`, method: "POST" });
    await archiveRoom(REPORT_ID);
    expect(lastCall()).toMatchObject({ url: `${base}/archive`, method: "POST" });
    await unarchiveRoom(REPORT_ID);
    expect(lastCall()).toMatchObject({ url: `${base}/unarchive`, method: "POST" });
    answer = () => reply(room({ id: "66666666-6666-4666-8666-666666666666", title: "Weekly sales (copy)" }));
    expect((await duplicateRoom(REPORT_ID)).title).toBe("Weekly sales (copy)");
    expect(lastCall()).toMatchObject({ url: `${base}/duplicate`, method: "POST" });
  });

  it("shares, marks assigned, unshares and reassigns by user id", async () => {
    await putMember(REPORT_ID, "u-bob", { assigned: true });
    expect(lastCall()).toEqual({ url: `${base}/members/u-bob`, method: "PUT", body: { assigned: true } });
    await deleteMember(REPORT_ID, "u-bob");
    expect(lastCall()).toMatchObject({ url: `${base}/members/u-bob`, method: "DELETE" });
    await reassignRoom(REPORT_ID, "u-bob");
    expect(lastCall()).toEqual({ url: `${base}/reassign`, method: "POST", body: { userId: "u-bob" } });
  });

  it("throws the refusal of a mutation instead of pretending it worked", async () => {
    answer = () => reply({ detail: "Forbidden" }, 403);
    await expect(archiveRoom(REPORT_ID)).rejects.toMatchObject({ status: 403 });
    fetchMock.mockRejectedValueOnce(new TypeError("offline"));
    await expect(putMember(REPORT_ID, "u-bob", { assigned: false })).rejects.toMatchObject({ status: 0 });
  });

  it("lists every company workspace for an admin", async () => {
    answer = () => reply({ rooms: [{ ...room(), members: room().members }] });
    const rows = await fetchAllRooms();
    expect(rows).toHaveLength(1);
    expect(lastCall().url).toBe("/api/bridge/api/rooms?scope=all");
  });

  it("lists the company's people from the workspace directory, which members may read", async () => {
    answer = () =>
      reply({
        users: [
          { userId: "u-alice", displayName: "Alice" },
          { userId: "u-bob", displayName: "" },
          { displayName: "No id" },
        ],
      });
    expect(await fetchPeople()).toEqual([
      { userId: "u-alice", displayName: "Alice" },
      { userId: "u-bob", displayName: "u-bob" },
    ]);
    expect(lastCall().url).toBe("/api/bridge/api/rooms/directory");
  });

  it("reads and saves this user's own notification choices", async () => {
    answer = () => reply({ inApp: true, muted: false, channels: { telegram: true }, linkedChannels: [{ name: "telegram", label: "Telegram" }] });
    const prefs = await fetchNotify(REPORT_ID);
    expect(prefs.linkedChannels).toEqual([{ name: "telegram", label: "Telegram" }]);
    expect(lastCall().url).toBe(`${base}/notify`);
    await putNotify(REPORT_ID, { inApp: true, muted: true, channels: { telegram: false } });
    expect(lastCall()).toEqual({
      url: `${base}/notify`,
      method: "PUT",
      body: { inApp: true, muted: true, channels: { telegram: false } },
    });
  });
});
