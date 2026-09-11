import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { VisualStateProvider } from "@/hooks/use-visual-state";
import { DefaultDashboard } from "../default-dashboard";

/**
 * The home dashboard degrades honestly: a section whose data call fails names
 * the endpoint and the status instead of rendering an empty card, and the
 * sections that did load still paint.
 */

function jsonResponse(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 401 ? "Unauthorized" : "OK",
    json: async () => JSON.parse(text),
    text: async () => text,
  } as Response;
}

interface RouteOverrides {
  health?: Response;
  conversations?: Response;
}

function routedFetch(overrides: RouteOverrides = {}) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/api/health")) {
      return (
        overrides.health ??
        jsonResponse({
          status: "ok",
          services: [
            { name: "bridge", status: "healthy" },
            { name: "orchestrator", status: "healthy" },
          ],
        })
      );
    }
    if (url.includes("/api/bridge/api/conversations")) {
      return overrides.conversations ?? jsonResponse({ data: { meta: {}, payload: [] } });
    }
    if (url.includes("/api/actions/execute")) {
      return jsonResponse({ data: { tasks: [], agents: [] } });
    }
    return jsonResponse({});
  });
}

function renderDashboard() {
  return render(
    <VisualStateProvider>
      <DefaultDashboard />
    </VisualStateProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

beforeEach(() => {
  vi.stubGlobal("fetch", routedFetch());
});

describe("DefaultDashboard", () => {
  it("names the endpoint and status when the health call fails, and still paints the rest", async () => {
    vi.stubGlobal("fetch", routedFetch({ health: jsonResponse({}, 401) }));
    renderDashboard();

    const error = await screen.findByTestId("section-error-health");
    expect(error.textContent).toMatch(/unavailable \(401\)/i);
    expect(error.textContent).toContain("/api/health");

    // The failure is confined to its own section.
    expect(screen.getByTestId("metric-summary")).toBeTruthy();
    expect(screen.getAllByTestId("quick-action").length).toBeGreaterThan(0);
  });

  it("reports a failed quick action instead of swallowing it into the console", async () => {
    vi.stubGlobal("fetch", routedFetch({ conversations: jsonResponse({}, 401) }));
    renderDashboard();

    const inbox = await screen.findByRole("button", { name: /check inbox/i });
    fireEvent.click(inbox);

    const error = await screen.findByTestId("section-error-actions");
    expect(error.textContent).toMatch(/unavailable \(401\)/i);
    expect(error.textContent).toContain("/api/bridge/api/conversations");
  });

  it("reports a request that never got an answer, rather than showing a confident zero", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.includes("/api/actions/execute")) {
          throw new TypeError("Failed to fetch");
        }
        return jsonResponse({ status: "ok", services: [] });
      }),
    );
    renderDashboard();

    const tasksError = await screen.findByTestId("section-error-tasks");
    expect(tasksError.textContent).toMatch(/no response/i);
    expect(tasksError.textContent).toContain("/api/actions/execute");
    expect((await screen.findByTestId("section-error-agents")).textContent).toMatch(/no response/i);
  });

  it("survives a health payload with no services list rather than taking the page down", async () => {
    // Home renders this component on every load now, so a 200 whose body is
    // not the shape we expect must degrade to "awaiting first probe" — not
    // throw out of render and white-screen the whole app.
    vi.stubGlobal("fetch", routedFetch({ health: jsonResponse({ status: "healthy" }) }));
    renderDashboard();

    expect(await screen.findByTestId("default-dashboard")).toBeTruthy();
    expect(screen.getByTestId("metric-summary").textContent).toMatch(/awaiting first probe/i);
    expect(screen.getAllByTestId("quick-action").length).toBeGreaterThan(0);
  });

  it("shows no section error while every call succeeds", async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByTestId("metric-summary")).toBeTruthy());
    await waitFor(() =>
      expect(screen.getByTestId("metric-summary").textContent).toMatch(/2/),
    );
    expect(screen.queryByTestId("section-error-health")).toBeNull();
  });

  it("counts only services that are meant to run, and says how many are switched off", async () => {
    vi.stubGlobal(
      "fetch",
      routedFetch({
        health: jsonResponse({
          status: "ok",
          services: [
            { name: "engine", label: "Agent engine", status: "healthy" },
            { name: "bridge", label: "API bridge", status: "healthy" },
            { name: "vision", label: "Vision (camera)", status: "disabled" },
          ],
        }),
      }),
    );
    renderDashboard();
    const metric = await screen.findByTestId("metric-summary");
    await waitFor(() => expect(metric.textContent).toContain("2/2"));
    expect(metric.textContent).toContain("1 switched off");
    expect(metric.textContent).not.toContain("Degraded");
  });

  it("names a degraded service in the health caption instead of a bare count", async () => {
    vi.stubGlobal(
      "fetch",
      routedFetch({
        health: jsonResponse({
          status: "degraded",
          services: [
            { name: "engine", label: "Agent engine", status: "healthy" },
            { name: "bridge", label: "API bridge", status: "degraded", detail: "memory error:503" },
          ],
        }),
      }),
    );
    renderDashboard();
    const metric = await screen.findByTestId("metric-summary");
    await waitFor(() => expect(metric.textContent).toContain("1/2"));
    expect(metric.textContent).toContain("API bridge");
  });
});
