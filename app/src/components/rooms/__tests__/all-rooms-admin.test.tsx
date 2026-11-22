/**
 * "All company workspaces" (G4, A6): an owner or admin sees every workspace
 * in the company with its assigned people and its paused or archived state,
 * reassigns one, restores an archived one, and opens any of them. The entry
 * point on the rail is offered to owners and admins only (G6).
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let sessionRole: string | null = "admin";
vi.mock("next-auth/react", () => ({
  useSession: () => ({
    data: sessionRole ? { role: sessionRole, user: { name: "Dana" } } : null,
    status: sessionRole ? "authenticated" : "unauthenticated",
  }),
}));

import { AllRoomsAdmin, AllRoomsAdminEntry } from "@/components/rooms/all-rooms-admin";
import { RoomRail } from "@/components/rooms/room-rail";
import { ARCHIVED_ID, REPORT_ID, SUMMARIES, reply, room, summary } from "@/lib/rooms/__tests__/fixtures";
import { mockBridge, withClient } from "./controls-harness";

let bridge: ReturnType<typeof mockBridge>;

const ROWS = [
  { ...room(), members: room().members },
  {
    ...room({ id: ARCHIVED_ID, title: "Old pipeline", archived: true }),
    members: [{ userId: "u-carol", displayName: "Carol", assigned: true }],
  },
];

beforeEach(() => {
  sessionRole = "admin";
  bridge = mockBridge();
  bridge.on("GET", "/rooms?scope=all", { rooms: ROWS });
  bridge.on("GET", "/rooms/directory", { users: [ { userId: "u-alice", displayName: "Alice" }, { userId: "u-bob", displayName: "Bob" }, { userId: "u-carol", displayName: "Carol" } ] });;
});
afterEach(() => vi.unstubAllGlobals());

describe("all company workspaces", () => {
  it("lists every workspace with its assigned people and state", async () => {
    withClient(<AllRoomsAdmin open onOpenChange={vi.fn()} onOpenRoom={vi.fn()} />);
    const live = await screen.findByTestId(`rooms-admin-row-${REPORT_ID}`);
    expect(live).toHaveTextContent("Weekly sales");
    expect(live).toHaveTextContent("Alice");
    expect(live).not.toHaveTextContent("Bob"); // shared, not assigned
    const old = screen.getByTestId(`rooms-admin-row-${ARCHIVED_ID}`);
    expect(old).toHaveTextContent(/archived/i);
    expect(old).toHaveTextContent("Carol");
  });

  it("reads assigned people from an `assigned` list when the row carries no members", async () => {
    bridge.on("GET", "/rooms?scope=all", {
      rooms: [{ ...summary(), assigned: [{ userId: "u-bob", displayName: "Bob" }] }],
    });
    withClient(<AllRoomsAdmin open onOpenChange={vi.fn()} onOpenRoom={vi.fn()} />);
    const row = await screen.findByTestId(`rooms-admin-row-${REPORT_ID}`);
    expect(row).toHaveTextContent("Assigned: Bob");
    expect(row).not.toHaveTextContent(/nobody/i);
  });

  it("says who is assigned is unknown, never 'nobody', when the row names no one either way", async () => {
    bridge.on("GET", "/rooms?scope=all", { rooms: [summary()] });
    withClient(<AllRoomsAdmin open onOpenChange={vi.fn()} onOpenRoom={vi.fn()} />);
    const row = await screen.findByTestId(`rooms-admin-row-${REPORT_ID}`);
    expect(row).toHaveTextContent(/assigned: unknown/i);
    expect(row).not.toHaveTextContent(/nobody/i);
  });

  it("warns that nobody is assigned only when the data says so", async () => {
    bridge.on("GET", "/rooms?scope=all", {
      rooms: [
        { ...summary(), members: [{ userId: "u-bob", displayName: "Bob", assigned: false }] },
        { ...summary({ id: ARCHIVED_ID, title: "Old pipeline" }), assigned: [] },
      ],
    });
    withClient(<AllRoomsAdmin open onOpenChange={vi.fn()} onOpenRoom={vi.fn()} />);
    expect(await screen.findByTestId(`rooms-admin-row-${REPORT_ID}`)).toHaveTextContent(/nobody assigned/i);
    expect(screen.getByTestId(`rooms-admin-row-${ARCHIVED_ID}`)).toHaveTextContent(/nobody assigned/i);
  });

  it("reassigns a workspace to another person", async () => {
    bridge.on("POST", `/rooms/${REPORT_ID}/reassign`, room({ runsAs: { userId: "u-bob", displayName: "Bob" } }));
    withClient(<AllRoomsAdmin open onOpenChange={vi.fn()} onOpenRoom={vi.fn()} />);
    const row = await screen.findByTestId(`rooms-admin-row-${REPORT_ID}`);
    fireEvent.click(within(row).getByTestId(`rooms-admin-reassign-${REPORT_ID}`));
    const select = await within(row).findByTestId(`rooms-admin-reassign-to-${REPORT_ID}`);
    await waitFor(() => expect(within(select).getAllByRole("option").length).toBeGreaterThan(1));
    fireEvent.change(select, { target: { value: "u-bob" } });
    fireEvent.click(within(row).getByTestId(`rooms-admin-reassign-confirm-${REPORT_ID}`));
    await waitFor(() => expect(bridge.calls("POST", `/rooms/${REPORT_ID}/reassign`)).toHaveLength(1));
    expect(bridge.calls("POST", `/rooms/${REPORT_ID}/reassign`)[0].body).toEqual({ userId: "u-bob" });
    await waitFor(() => expect(bridge.calls("GET", "/rooms?scope=all").length).toBeGreaterThan(1));
  });

  it("restores an archived workspace, which comes back paused", async () => {
    bridge.on("POST", `/rooms/${ARCHIVED_ID}/unarchive`, room({ id: ARCHIVED_ID, archived: false, paused: true }));
    withClient(<AllRoomsAdmin open onOpenChange={vi.fn()} onOpenRoom={vi.fn()} />);
    const row = await screen.findByTestId(`rooms-admin-row-${ARCHIVED_ID}`);
    expect(within(row).getByTestId(`rooms-admin-restore-${ARCHIVED_ID}`)).toHaveAccessibleDescription(/paused/i);
    expect(screen.queryByTestId(`rooms-admin-restore-${REPORT_ID}`)).toBeNull();
    fireEvent.click(within(row).getByTestId(`rooms-admin-restore-${ARCHIVED_ID}`));
    await waitFor(() => expect(bridge.calls("POST", `/rooms/${ARCHIVED_ID}/unarchive`)).toHaveLength(1));
  });

  it("opens a workspace from the list", async () => {
    const onOpenRoom = vi.fn();
    withClient(<AllRoomsAdmin open onOpenChange={vi.fn()} onOpenRoom={onOpenRoom} />);
    const row = await screen.findByTestId(`rooms-admin-row-${REPORT_ID}`);
    fireEvent.click(within(row).getByRole("button", { name: /open/i }));
    expect(onOpenRoom).toHaveBeenCalledWith(REPORT_ID);
  });

  it("says what the server answered when the list is refused", async () => {
    bridge.on("GET", "/rooms?scope=all", () => reply({ detail: "Forbidden" }, 403));
    withClient(<AllRoomsAdmin open onOpenChange={vi.fn()} onOpenRoom={vi.fn()} />);
    expect(await screen.findByTestId("rooms-admin-error")).toHaveTextContent("403");
  });
});

describe("the rail entry point", () => {
  it("is offered to an admin and an owner", () => {
    withClient(<AllRoomsAdminEntry onOpenRoom={vi.fn()} />);
    expect(screen.getByTestId("rooms-admin-entry")).toHaveTextContent(/all company workspaces/i);
    sessionRole = "owner";
    withClient(<AllRoomsAdminEntry onOpenRoom={vi.fn()} />);
    expect(screen.getAllByTestId("rooms-admin-entry")).toHaveLength(2);
  });

  it("is not offered to a member, a viewer, or with no session", () => {
    for (const role of ["member", "viewer", null]) {
      sessionRole = role;
      const { unmount } = withClient(<AllRoomsAdminEntry onOpenRoom={vi.fn()} />);
      expect(screen.queryByTestId("rooms-admin-entry")).toBeNull();
      unmount();
    }
    expect(bridge.calls("GET", "/rooms?scope=all")).toHaveLength(0);
  });

  it("opens the list", async () => {
    withClient(<AllRoomsAdminEntry onOpenRoom={vi.fn()} />);
    fireEvent.click(screen.getByTestId("rooms-admin-entry"));
    expect(await screen.findByTestId("rooms-admin")).toBeInTheDocument();
  });
});

describe("on the rail", () => {
  it("sits on the rail for an admin and opens the chosen workspace through it", async () => {
    const onSelect = vi.fn();
    withClient(<RoomRail rooms={SUMMARIES} activeRoomId={null} onSelect={onSelect} />);
    fireEvent.click(await screen.findByTestId("rooms-admin-entry"));
    const row = await screen.findByTestId(`rooms-admin-row-${REPORT_ID}`);
    fireEvent.click(within(row).getByRole("button", { name: /open/i }));
    expect(onSelect).toHaveBeenCalledWith(REPORT_ID);
  });

  it("is absent from a member's rail", async () => {
    sessionRole = "member";
    withClient(<RoomRail rooms={SUMMARIES} activeRoomId={null} onSelect={vi.fn()} />);
    await screen.findByTestId("room-rail-main");
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId("rooms-admin-entry")).toBeNull();
  });
});
