/**
 * What one page load costs the bridge.
 *
 * The shell reads tasks and agents for the sidebar badges; the chat home's
 * landing and the (hidden, but mounted) Dashboard view show the same figures.
 * Three readers of one listing must cost one request, not three.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({
    notifyConversationUpdate: vi.fn(),
    setRender: vi.fn(),
    currentView: null,
    viewStack: [],
    pendingAgentData: null,
    popView: vi.fn(),
    clearViews: vi.fn(),
    canvasMode: "idle",
    setCanvasMode: vi.fn(),
    dashboardCode: null,
    setDashboardCode: vi.fn(),
    clearDashboard: vi.fn(),
    isUpdating: false,
    submitAction: vi.fn(),
    resolveAction: vi.fn(),
    pushView: vi.fn(),
  }),
}));
vi.mock("@/hooks/use-throttle", () => ({ useThrottle: (v: string) => v }));
vi.mock("@/hooks/use-dashboard-agent", () => ({
  useDashboardAgent: () => ({ error: null, retry: vi.fn(), dismiss: vi.fn() }),
}));
vi.mock("@/lib/event-bus/use-event-stream", () => ({
  useEventStream: () => ({ events: [], connected: false, synced: false }),
}));
vi.mock("@/components/canvas/live-canvas", () => ({
  LiveCanvas: () => <div data-testid="live-canvas" />,
}));
vi.mock("@/lib/api/health", () => ({
  fetchHealth: vi.fn().mockResolvedValue({ status: "ok", services: [] }),
}));
vi.mock("cronstrue", () => ({ default: { toString: (expr: string) => expr } }));
vi.mock("next-auth/react", () => ({
  useSession: () => ({ data: { role: "owner" }, status: "authenticated", update: vi.fn() }),
  SessionProvider: ({ children }: { children: React.ReactNode }) => children,
  signOut: vi.fn(),
  signIn: vi.fn(),
}));

function jsonResponse(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status < 400,
    status,
    json: async () => JSON.parse(text),
    text: async () => text,
  } as Response;
}

const toolCalls: string[] = [];

function routedFetch() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/api/actions/execute")) {
      const body = JSON.parse(String(init?.body ?? "{}")) as { tool?: string };
      toolCalls.push(body.tool ?? "");
      if (body.tool === "agent_status") {
        return jsonResponse({ data: { agents: [{ name: "worker", status: "healthy" }] } });
      }
      return jsonResponse({ data: { tasks: [{ id: "1", title: "One", status: "REVIEW" }] } });
    }
    return jsonResponse({ messages: [], active: false });
  });
}

function count(tool: string) {
  return toolCalls.filter((t) => t === tool).length;
}

beforeEach(() => {
  toolCalls.length = 0;
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 1280 });
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query.includes("min-width: 1024px"),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  window.history.replaceState(null, "", "/");
  vi.stubGlobal("fetch", routedFetch());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AppShell — requests on load", () => {
  it("reads tasks and agents once each with the shell, the landing and the dashboard mounted", async () => {
    const { AppShell } = await import("../app-shell");
    render(<AppShell />);

    // All three readers are on the page: the shell's badges, the chat home's
    // landing, and the Dashboard view mounted behind display:none.
    expect(await screen.findByTestId("chat-landing", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByTestId("default-dashboard")).toBeInTheDocument();
    await waitFor(() => expect(count("list_tasks")).toBeGreaterThan(0));
    await waitFor(() => expect(count("agent_status")).toBeGreaterThan(0));
    // Let any straggling effects fire before counting.
    await new Promise((r) => setTimeout(r, 50));

    expect({ list_tasks: count("list_tasks"), agent_status: count("agent_status") }).toEqual({
      list_tasks: 1,
      agent_status: 1,
    });
  }, 20_000);
});
