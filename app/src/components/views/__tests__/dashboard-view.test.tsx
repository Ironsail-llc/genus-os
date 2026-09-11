import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { VisualStateProvider } from "@/hooks/use-visual-state";
import { DashboardView } from "../dashboard-view";

/**
 * The Dashboard tab is the operator's home. It must paint real data from the
 * BFF endpoints — never an LLM-generated page that can (and on the production
 * box did) arrive blank. The AI canvas stays reachable, but only on an
 * explicit click, so opening the app costs no model call.
 */

function jsonResponse(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => JSON.parse(text),
    text: async () => text,
  } as Response;
}

function routedFetch() {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/api/health")) {
      return jsonResponse({ status: "ok", services: [{ name: "bridge", status: "healthy" }] });
    }
    if (url.includes("/api/actions/execute")) {
      return jsonResponse({ data: { tasks: [], agents: [] } });
    }
    if (url.includes("/api/session")) {
      return jsonResponse({});
    }
    if (url.includes("/api/dashboard/welcome")) {
      return jsonResponse({ html: "<div>generated</div>", type: "html" });
    }
    return jsonResponse({});
  });
}

function renderView() {
  return render(
    <VisualStateProvider>
      <DashboardView visible />
    </VisualStateProvider>,
  );
}

let fetchMock: ReturnType<typeof routedFetch>;

beforeEach(() => {
  fetchMock = routedFetch();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const welcomeCalls = () =>
  fetchMock.mock.calls.filter(([input]) =>
    (typeof input === "string" ? input : String(input)).includes("/api/dashboard/welcome"),
  );

describe("DashboardView", () => {
  it("renders the real-data dashboard as the home content", async () => {
    renderView();
    expect(await screen.findByTestId("default-dashboard")).toBeTruthy();
  });

  it("makes no dashboard-generation call on mount", async () => {
    renderView();
    await screen.findByTestId("default-dashboard");
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(welcomeCalls()).toHaveLength(0);
    expect(screen.queryByTestId("live-canvas")).toBeNull();
  });

  it("mounts the AI canvas and generates only once the operator asks for it", async () => {
    renderView();
    const trigger = await screen.findByTestId("dashboard-generate-ai");
    fireEvent.click(trigger);

    expect(await screen.findByTestId("live-canvas")).toBeTruthy();
    await waitFor(() => expect(welcomeCalls().length).toBeGreaterThan(0));
  });
});
