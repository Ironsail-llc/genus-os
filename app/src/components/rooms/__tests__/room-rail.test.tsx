/**
 * The rail beside the chat: the main conversation first, then the caller's
 * workspaces grouped Reports / Agents / Tasks, with unread badges, paused and
 * archived states, an empty state, and a collapsed form for narrow screens.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RoomRail } from "@/components/rooms/room-rail";
import { AGENT_ID, ARCHIVED_ID, REPORT_ID, SUMMARIES, TASK_ID } from "@/lib/rooms/__tests__/fixtures";

function rail(props: Partial<Parameters<typeof RoomRail>[0]> = {}) {
  const onSelect = vi.fn();
  render(<RoomRail rooms={SUMMARIES} activeRoomId={null} onSelect={onSelect} {...props} />);
  return onSelect;
}

describe("RoomRail", () => {
  it("puts the main conversation first, then the groups in order", () => {
    rail();
    const nav = screen.getByRole("navigation", { name: "Workspaces" });
    const items = within(nav).getAllByRole("button").map((b) => b.getAttribute("data-testid"));
    expect(items[0]).toBe("room-rail-main");
    const headings = within(nav).getAllByRole("heading").map((h) => h.textContent);
    expect(headings).toEqual(["Reports", "Agents", "Tasks", "Archived"]);
    expect(within(screen.getByTestId("room-group-report")).getByText("Weekly sales")).toBeInTheDocument();
    expect(within(screen.getByTestId("room-group-agent")).getByText("Research desk")).toBeInTheDocument();
    expect(within(screen.getByTestId("room-group-task")).getByText("Renew the lease")).toBeInTheDocument();
  });

  it("marks the main conversation current when no room is open", () => {
    rail();
    expect(screen.getByTestId("room-rail-main")).toHaveAttribute("aria-current", "page");
  });

  it("marks the open room current and selects rooms by id", () => {
    const onSelect = rail({ activeRoomId: AGENT_ID });
    expect(screen.getByTestId(`room-rail-item-${AGENT_ID}`)).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("room-rail-main")).not.toHaveAttribute("aria-current");
    fireEvent.click(screen.getByTestId(`room-rail-item-${REPORT_ID}`));
    expect(onSelect).toHaveBeenLastCalledWith(REPORT_ID);
    fireEvent.click(screen.getByTestId("room-rail-main"));
    expect(onSelect).toHaveBeenLastCalledWith(null);
  });

  it("shows an unread badge, and none for the room that is open", () => {
    rail();
    expect(screen.getByTestId(`room-unread-${REPORT_ID}`)).toHaveTextContent("3");
    expect(screen.queryByTestId(`room-unread-${AGENT_ID}`)).toBeNull();
    screen.getByTestId(`room-rail-item-${REPORT_ID}`);
  });

  it("hides the badge of the open room even before the list catches up", () => {
    rail({ activeRoomId: REPORT_ID });
    expect(screen.queryByTestId(`room-unread-${REPORT_ID}`)).toBeNull();
  });

  it("says when a room is paused, and keeps archived rooms apart", () => {
    rail();
    expect(within(screen.getByTestId(`room-rail-item-${TASK_ID}`)).getByText("Paused")).toBeInTheDocument();
    const archived = screen.getByTestId("room-group-archived");
    expect(within(archived).getByTestId(`room-rail-item-${ARCHIVED_ID}`)).toBeInTheDocument();
    expect(within(screen.getByTestId("room-group-report")).queryByText("Old pipeline")).toBeNull();
  });

  it("has an empty state that still offers the main conversation", () => {
    rail({ rooms: [] });
    expect(screen.getByTestId("room-rail-main")).toBeInTheDocument();
    expect(screen.getByTestId("room-rail-empty")).toHaveTextContent(/No workspaces yet/);
  });

  it("says so when the listing could not be read", () => {
    rail({ rooms: [], error: { endpoint: "/api/bridge/api/rooms", status: 502 } });
    expect(screen.getByTestId("room-rail-error")).toHaveTextContent("Workspaces unavailable (502)");
  });

  it("collapses to one button on narrow screens, with the unread total", () => {
    const onSelect = rail({ collapsed: true });
    expect(screen.queryByRole("navigation", { name: "Workspaces" })).toBeNull();
    const toggle = screen.getByTestId("room-rail-toggle");
    expect(toggle).toHaveTextContent("Workspaces");
    expect(toggle).toHaveTextContent("3");
    fireEvent.click(toggle);
    const nav = screen.getByRole("navigation", { name: "Workspaces" });
    fireEvent.click(within(nav).getByTestId(`room-rail-item-${AGENT_ID}`));
    expect(onSelect).toHaveBeenCalledWith(AGENT_ID);
    // Choosing closes it again.
    expect(screen.queryByRole("navigation", { name: "Workspaces" })).toBeNull();
  });

  it("names the open room on the collapsed button", () => {
    rail({ collapsed: true, activeRoomId: AGENT_ID });
    expect(screen.getByTestId("room-rail-toggle")).toHaveTextContent("Research desk");
  });

  it("closes the collapsed list when the open room changes from anywhere (its header, a link)", () => {
    const { rerender } = render(
      <RoomRail
        rooms={SUMMARIES}
        activeRoomId={null}
        onSelect={vi.fn()}
        collapsed
        header={<button type="button">digest item</button>}
      />,
    );
    fireEvent.click(screen.getByTestId("room-rail-toggle"));
    expect(screen.getByRole("navigation", { name: "Workspaces" })).toBeInTheDocument();
    // The header's own buttons navigate without passing through the rail's onSelect.
    rerender(
      <RoomRail
        rooms={SUMMARIES}
        activeRoomId={REPORT_ID}
        onSelect={vi.fn()}
        collapsed
        header={<button type="button">digest item</button>}
      />,
    );
    expect(screen.queryByRole("navigation", { name: "Workspaces" })).toBeNull();
  });
});

