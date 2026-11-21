/**
 * C1: "New in your workspaces", on the chat landing and at the top of the
 * rail. Workspaces with unread activity, newest first; an honest empty state;
 * choosing one opens it. The rail's header also offers "New workspace".
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NewInWorkspaces, RoomRailHeader } from "@/components/rooms/new-in-workspaces";
import { AGENT_ID, REPORT_ID, TASK_ID, summary } from "@/lib/rooms/__tests__/fixtures";

vi.mock("@/hooks/use-agents", () => ({
  useAgents: () => ({ agents: [{ name: "Main", agentId: "main", status: "healthy", schedule: "" }] }),
}));

const ROOMS = [
  summary({
    id: REPORT_ID,
    title: "Weekly sales",
    unread: 3,
    lastActivityAt: "2026-11-20T09:00:00Z",
    latestEdition: { id: "e2", seq: 2, title: "Week 47", periodStart: "2026-11-16", periodEnd: "2026-11-22" },
  }),
  summary({ id: AGENT_ID, kind: "agent", title: "Research desk", unread: 1, lastActivityAt: "2026-11-21T09:00:00Z" }),
  summary({ id: TASK_ID, kind: "task", title: "Renew the lease", unread: 0 }),
];

describe("NewInWorkspaces", () => {
  it("lists one unread workspace with its count and latest edition", () => {
    render(<NewInWorkspaces rooms={[ROOMS[0]]} onOpen={vi.fn()} />);
    const item = screen.getByTestId(`new-in-workspaces-item-${REPORT_ID}`);
    expect(item).toHaveTextContent("Weekly sales");
    expect(item).toHaveTextContent("Week 47");
    expect(item).toHaveTextContent("3");
  });

  it("lists many, newest activity first, and leaves out what has nothing new", () => {
    render(<NewInWorkspaces rooms={ROOMS} onOpen={vi.fn()} />);
    const list = screen.getByTestId("new-in-workspaces");
    const ids = within(list)
      .getAllByRole("button")
      .map((b) => b.getAttribute("data-testid"));
    expect(ids).toEqual([`new-in-workspaces-item-${AGENT_ID}`, `new-in-workspaces-item-${REPORT_ID}`]);
    expect(within(list).queryByText("Renew the lease")).toBeNull();
  });

  it("says so when nothing is new", () => {
    render(<NewInWorkspaces rooms={[ROOMS[2]]} onOpen={vi.fn()} />);
    expect(screen.getByTestId("new-in-workspaces-empty")).toHaveTextContent(/Nothing new/i);
  });

  it("does not claim nothing is new while the list is loading or failed", () => {
    const { rerender } = render(<NewInWorkspaces rooms={[]} isLoading onOpen={vi.fn()} />);
    expect(screen.queryByTestId("new-in-workspaces-empty")).toBeNull();
    rerender(<NewInWorkspaces rooms={[]} error={{ endpoint: "/api/bridge/api/rooms", status: 502 }} onOpen={vi.fn()} />);
    expect(screen.queryByTestId("new-in-workspaces-empty")).toBeNull();
    expect(screen.getByTestId("new-in-workspaces-error")).toHaveTextContent("502");
  });

  it("opens a workspace by id", () => {
    const onOpen = vi.fn();
    render(<NewInWorkspaces rooms={ROOMS} onOpen={onOpen} />);
    fireEvent.click(screen.getByTestId(`new-in-workspaces-item-${REPORT_ID}`));
    expect(onOpen).toHaveBeenCalledWith(REPORT_ID);
  });

  it("is a labelled region so it can be found by its name", () => {
    render(<NewInWorkspaces rooms={ROOMS} onOpen={vi.fn()} />);
    expect(screen.getByRole("region", { name: "New in your workspaces" })).toBeInTheDocument();
  });
});

describe("RoomRailHeader", () => {
  it("folds to one line with the count, since the rail's groups already carry the badges", () => {
    const onSelect = vi.fn();
    render(<RoomRailHeader rooms={ROOMS} activeRoomId={null} onSelect={onSelect} />);
    const toggle = screen.getByTestId("new-in-workspaces-toggle");
    expect(toggle).toHaveTextContent("2 workspaces updated");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId(`new-in-workspaces-item-${REPORT_ID}`)).toBeNull();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByTestId(`new-in-workspaces-item-${REPORT_ID}`));
    expect(onSelect).toHaveBeenCalledWith(REPORT_ID);
  });

  it("says nothing is new on the rail too", () => {
    render(<RoomRailHeader rooms={[ROOMS[2]]} activeRoomId={null} onSelect={vi.fn()} />);
    expect(screen.getByTestId("new-in-workspaces-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("new-in-workspaces-toggle")).toBeNull();
  });

  it("shows what is new, without the room that is open", () => {
    render(<RoomRailHeader rooms={ROOMS} activeRoomId={REPORT_ID} onSelect={vi.fn()} />);
    fireEvent.click(screen.getByTestId("new-in-workspaces-toggle"));
    const list = screen.getByTestId("new-in-workspaces");
    expect(within(list).queryByText("Weekly sales")).toBeNull();
    expect(within(list).getByText("Research desk")).toBeInTheDocument();
  });

  it("offers a new workspace", () => {
    render(<RoomRailHeader rooms={ROOMS} activeRoomId={null} onSelect={vi.fn()} />);
    expect(screen.getByTestId("new-workspace-button")).toHaveTextContent(/New workspace/);
  });
});
