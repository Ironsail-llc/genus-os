import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { VisualStateProvider } from "@/hooks/use-visual-state";

/**
 * The canvas used to hide every generation failure: a welcome answer with no
 * page fell back to the static dashboard without a word, a saved page from an
 * earlier session was restored and never regenerated, and Retry only reset
 * the canvas to idle without asking for anything. These run against the real
 * visual state, so a mode change is what the operator would see.
 */

const agent = vi.hoisted(() => ({
  error: null as string | null,
  retry: vi.fn(),
  dismiss: vi.fn(),
}));

vi.mock("@/hooks/use-dashboard-agent", () => ({
  useDashboardAgent: () => agent,
}));
vi.mock("@/lib/dashboard/error-reporter", () => ({ reportDashboardError: vi.fn() }));
vi.mock("@/components/business/default-dashboard", () => ({
  DefaultDashboard: () => <div data-testid="default-dashboard" />,
}));
vi.mock("../srcdoc-renderer", () => ({
  SrcdocRenderer: ({ html }: { html: string }) => <div data-testid="srcdoc">{html}</div>,
}));

type Reply = { status?: number; body: unknown };

let fetchMock: ReturnType<typeof vi.fn>;

function installFetch(session: Reply, welcome: Reply[]) {
  const queue = [...welcome];
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (url === "/api/session" && method === "GET") {
      return new Response(JSON.stringify(session.body), { status: session.status ?? 200 });
    }
    if (url === "/api/session") {
      return new Response("{}", { status: 200 });
    }
    if (url === "/api/dashboard/welcome") {
      const reply = queue.length > 1 ? queue.shift()! : queue[0];
      return new Response(JSON.stringify(reply.body), { status: reply.status ?? 200 });
    }
    return new Response("{}", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
}

const welcomeCalls = () =>
  fetchMock.mock.calls.filter(([url]) => String(url) === "/api/dashboard/welcome");

async function renderCanvas() {
  const { LiveCanvas } = await import("../live-canvas");
  return render(
    <VisualStateProvider>
      <LiveCanvas />
    </VisualStateProvider>,
  );
}

beforeEach(() => {
  agent.error = null;
  agent.retry.mockReset();
  agent.dismiss.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("LiveCanvas — a failed generation is visible and retryable", () => {
  it("shows the reason the welcome view failed", async () => {
    installFetch({ body: {} }, [{ body: { error: "Dashboard service temporarily unavailable" } }]);
    await renderCanvas();

    const error = await screen.findByTestId("canvas-error");
    expect(error).toHaveTextContent("Dashboard service temporarily unavailable");
  });

  it("re-requests the welcome view on Retry", async () => {
    installFetch({ body: {} }, [
      { body: { error: "Dashboard service temporarily unavailable" } },
      { body: { html: "<div>fresh view</div>", type: "html" } },
    ]);
    await renderCanvas();
    await screen.findByTestId("canvas-error");

    fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    await waitFor(() => expect(welcomeCalls()).toHaveLength(2));
    expect(await screen.findByTestId("srcdoc")).toHaveTextContent("fresh view");
    expect(screen.queryByTestId("canvas-error")).not.toBeInTheDocument();
  });

  it("does not fall back silently when the welcome answer has no page", async () => {
    installFetch({ body: {} }, [{ body: {} }]);
    await renderCanvas();

    expect(await screen.findByTestId("canvas-error")).toBeInTheDocument();
    expect(screen.queryByTestId("default-dashboard")).not.toBeInTheDocument();
  });

  it("offers to regenerate a page restored from an earlier session", async () => {
    installFetch({ body: { html: "<div>saved view</div>" } }, [
      { body: { html: "<div>fresh view</div>", type: "html" } },
    ]);
    await renderCanvas();
    expect(await screen.findByTestId("srcdoc")).toHaveTextContent("saved view");
    expect(welcomeCalls()).toHaveLength(0);

    fireEvent.click(screen.getByTestId("canvas-regenerate"));

    await waitFor(() => expect(welcomeCalls()).toHaveLength(1));
    await waitFor(() => expect(screen.getByTestId("srcdoc")).toHaveTextContent("fresh view"));
  });

  it("shows a failed conversation update with its reason and a Retry", async () => {
    agent.error = "Dashboard service temporarily unavailable";
    installFetch({ body: { html: "<div>saved view</div>" } }, [{ body: {} }]);
    await renderCanvas();

    const banner = await screen.findByTestId("canvas-update-error");
    expect(banner).toHaveTextContent("Dashboard service temporarily unavailable");

    fireEvent.click(screen.getByTestId("canvas-update-retry"));
    expect(agent.retry).toHaveBeenCalledTimes(1);
  });
});
