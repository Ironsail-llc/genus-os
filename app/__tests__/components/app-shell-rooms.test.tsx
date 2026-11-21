/**
 * Workspaces in the shell: the rail beside the chat home, `?v=chat&room={id}`
 * opening a workspace in place of the main conversation (which stays mounted,
 * so nothing typed there is lost), opening one marking it read, and every
 * other view keeping the main conversation in its dock.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AGENT_ID, EDITIONS, REPORT_ID, SUMMARIES, reply, room } from "@/lib/rooms/__tests__/fixtures";

vi.mock("@/hooks/use-tasks", () => ({
  useTasks: () => ({ tasks: [], isLoading: false, approveTask: vi.fn(), rejectTask: vi.fn(), answerQuestion: vi.fn() }),
}));
vi.mock("@/hooks/use-agents", () => ({
  useAgents: () => ({
    agents: [],
    summary: { healthy: 0, degraded: 0, failed: 0, sleeping: 0, total: 0 },
    isLoading: false,
  }),
}));
vi.mock("@/hooks/use-inbox", () => ({
  useInbox: () => ({ items: [], count: 0, isLoading: false, error: null, refresh: vi.fn(), answer: vi.fn() }),
}));
vi.mock("@/hooks/use-accessible-tenants", () => ({ useHasChildTenants: () => false }));
vi.mock("@/components/layout/tenant-switcher", () => ({
  TenantSwitcher: () => null,
  ActingTenantBanner: () => null,
}));
vi.mock("@/components/mfa-setup-banner", () => ({ MfaSetupBanner: () => null }));
vi.mock("@/components/canvas/live-canvas", () => ({ LiveCanvas: () => <div data-testid="live-canvas" /> }));
vi.mock("@/components/chat-panel", () => ({
  ChatPanel: ({ room, variant }: { room?: { id: string }; variant?: string }) => (
    <div data-testid={room ? "room-chat-panel" : "chat-panel"} data-room={room?.id ?? ""} data-variant={variant ?? ""} />
  ),
}));
vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({ viewStack: [], pendingAgentData: null }),
  VisualStateProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/lib/event-bus/use-event-stream", () => ({
  useEventStream: () => ({ events: [], connected: true, synced: true }),
}));
vi.mock("@/components/business/bar-chart", () => ({
  BarChart: ({ title }: { title: string }) => <div data-testid="bar-chart">{title}</div>,
}));
const mockScreenSize = vi.fn(() => "desktop");
vi.mock("@/hooks/use-mobile", () => ({
  useScreenSize: () => mockScreenSize(),
  useIsMobile: () => mockScreenSize() === "mobile",
}));

import { AppShell } from "@/components/layout/app-shell";

const WAIT = { timeout: 5000 };
vi.setConfig({ testTimeout: 15000 });
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockScreenSize.mockReturnValue("desktop");
  window.localStorage.clear();
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/bridge/api/rooms") return reply({ rooms: SUMMARIES });
    if (url === `/api/bridge/api/rooms/${REPORT_ID}`) return reply(room());
    if (url === `/api/bridge/api/rooms/${AGENT_ID}`) return reply(room({ id: AGENT_ID, kind: "agent", title: "Research desk" }));
    if (url.endsWith("/editions")) return reply({ editions: url.includes(REPORT_ID) ? EDITIONS : [] });
    if (url.endsWith("/read") && init?.method === "POST") return reply({ ok: true });
    return reply({});
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

describe("workspaces in the shell", () => {
  it("shows the rail beside the main conversation on the chat home", async () => {
    window.history.replaceState(null, "", "/?v=chat");
    render(<AppShell />);
    const nav = await screen.findByRole("navigation", { name: "Workspaces" });
    expect(within(nav).getByTestId("room-rail-main")).toHaveAttribute("aria-current", "page");
    expect(await within(nav).findByText("Weekly sales")).toBeInTheDocument();
    expect(screen.getByTestId("chat-panel")).toBeVisible();
    expect(screen.queryByTestId("room-view")).toBeNull();
  });

  it("opens a workspace from the link, in place of the main conversation, and marks it read", async () => {
    window.history.replaceState(null, "", `/?v=chat&room=${REPORT_ID}`);
    render(<AppShell />);
    const roomView = await screen.findByTestId("room-view", {}, WAIT);
    expect(await within(roomView).findByRole("heading", { level: 1 })).toHaveTextContent("Weekly sales");
    expect(within(roomView).getByTestId("room-chat-panel")).toHaveAttribute("data-room", REPORT_ID);
    // The main conversation stays mounted, hidden, so its state survives.
    expect(screen.getByTestId("chat-panel")).not.toBeVisible();
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(`/api/bridge/api/rooms/${REPORT_ID}/read`, expect.objectContaining({ method: "POST" })),
    );
    // Its badge is gone for this user.
    await screen.findByTestId(`room-rail-item-${REPORT_ID}`);
    expect(screen.queryByTestId(`room-unread-${REPORT_ID}`)).toBeNull();
    expect(screen.getByTestId("header-title")).toHaveTextContent("Chat");
  });

  it("moves between workspaces and back to the main conversation from the rail", async () => {
    window.history.replaceState(null, "", "/?v=chat");
    render(<AppShell />);
    fireEvent.click(await screen.findByTestId(`room-rail-item-${AGENT_ID}`));
    expect(window.location.search).toBe(`?v=chat&room=${AGENT_ID}`);
    expect(await screen.findByTestId("room-view", {}, WAIT)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("room-rail-main"));
    expect(window.location.search).toBe("?v=chat");
    expect(screen.queryByTestId("room-view")).toBeNull();
    expect(screen.getByTestId("chat-panel")).toBeVisible();
  });

  it("follows the back button out of a workspace", async () => {
    window.history.replaceState(null, "", "/?v=chat");
    render(<AppShell />);
    fireEvent.click(await screen.findByTestId(`room-rail-item-${AGENT_ID}`));
    await screen.findByTestId("room-view", {}, WAIT);
    await act(async () => {
      window.history.replaceState(null, "", "/?v=chat");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(screen.queryByTestId("room-view")).toBeNull();
  });

  it("keeps the main conversation in the dock on every other view, with no rail", async () => {
    window.history.replaceState(null, "", `/?v=chat&room=${REPORT_ID}`);
    render(<AppShell />);
    await screen.findByTestId("room-view", {}, WAIT);
    fireEvent.click(screen.getByTestId("nav-tasks"));
    expect(window.location.search).toBe("?v=tasks");
    expect(screen.queryByTestId("room-view")).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Workspaces" })).toBeNull();
    expect(screen.getByTestId("chat-panel")).toHaveAttribute("data-variant", "dock");
  });

  it("shows no rail at all while nothing serves the rooms list", async () => {
    fetchMock.mockImplementation(async () => reply({ error: "Not found" }, 404));
    window.history.replaceState(null, "", "/?v=chat");
    render(<AppShell />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/bridge/api/rooms", expect.anything()));
    await act(async () => {});
    expect(screen.queryByTestId("room-rail")).toBeNull();
    expect(screen.queryByText(/Workspaces unavailable/)).toBeNull();
    expect(screen.getByTestId("chat-panel")).toBeVisible();
  });

  it("collapses the rail to a button on a phone", async () => {
    mockScreenSize.mockReturnValue("mobile");
    window.history.replaceState(null, "", "/?v=chat");
    render(<AppShell />);
    const toggle = await screen.findByTestId("room-rail-toggle");
    expect(screen.queryByRole("navigation", { name: "Workspaces" })).toBeNull();
    fireEvent.click(toggle);
    fireEvent.click(await screen.findByTestId(`room-rail-item-${REPORT_ID}`));
    expect(await screen.findByTestId("room-view", {}, WAIT)).toBeInTheDocument();
  });
});
