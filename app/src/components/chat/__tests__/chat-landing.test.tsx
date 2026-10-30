import { render, screen, fireEvent, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * The landing the chat home shows before the first message: a greeting, a
 * read-only strip of the dashboard's three figures, the dashboard's quick
 * actions and the suggested prompts. No model call.
 */

const mockPushView = vi.fn();
vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({ pushView: mockPushView }),
}));

import { ChatLanding } from "../chat-landing";

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
  people?: Response;
}

function routedFetch(overrides: RouteOverrides = {}) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/api/health")) {
      return (
        overrides.health ??
        jsonResponse({
          status: "ok",
          services: [
            { name: "bridge", status: "healthy" },
            { name: "orchestrator", status: "healthy" },
            { name: "vision", status: "unhealthy" },
          ],
        })
      );
    }
    if (url.includes("/api/bridge/api/people")) {
      return overrides.people ?? jsonResponse({ people: [] });
    }
    if (url.includes("/api/actions/execute")) {
      const body = JSON.parse(String(init?.body ?? "{}")) as { tool?: string };
      if (body.tool === "agent_status") {
        return jsonResponse({
          data: {
            agents: [
              { name: "worker", status: "healthy" },
              { name: "helper", status: "healthy" },
              { name: "broken", status: "failed" },
            ],
          },
        });
      }
      return jsonResponse({
        data: {
          tasks: [
            { id: "1", title: "One", status: "TODO" },
            { id: "2", title: "Two", status: "IN_PROGRESS" },
            { id: "3", title: "Three", status: "REVIEW" },
            { id: "4", title: "Four", status: "DONE" },
          ],
        },
      });
    }
    return jsonResponse({});
  });
}

const PROMPTS = ["How's everything running?", "What happened today?"];

function renderLanding(props: { ownerName?: string; onPrompt?: (p: string) => void } = {}) {
  return render(
    <ChatLanding
      ownerName={props.ownerName}
      suggestedPrompts={PROMPTS}
      onPrompt={props.onPrompt ?? vi.fn()}
    />,
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", routedFetch());
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("ChatLanding", () => {
  it("greets the owner by first name when one is given", () => {
    renderLanding({ ownerName: "Alice" });
    expect(screen.getByTestId("chat-landing-greeting").textContent).toMatch(
      /^Good (morning|afternoon|evening), Alice$/,
    );
  });

  it("greets without a name when none is given", () => {
    renderLanding();
    expect(screen.getByTestId("chat-landing-greeting").textContent).toMatch(
      /^Good (morning|afternoon|evening)$/,
    );
  });

  it("shows services healthy of total, active tasks and agents online in one strip", async () => {
    renderLanding();
    const strip = screen.getByTestId("chat-landing-metrics");
    expect(await screen.findByText("2/3")).toBeTruthy();
    // TODO + IN_PROGRESS + REVIEW are active; DONE is not.
    expect(strip.querySelector('[data-testid="landing-metric-tasks"]')?.textContent).toContain("3");
    expect(strip.querySelector('[data-testid="landing-metric-agents"]')?.textContent).toContain("2");
    expect(strip.textContent).toMatch(/services/i);
    expect(strip.textContent).toMatch(/active tasks/i);
    expect(strip.textContent).toMatch(/agents online/i);
  });

  it("names the endpoint and status when the health call fails", async () => {
    vi.stubGlobal("fetch", routedFetch({ health: jsonResponse({}, 401) }));
    renderLanding();
    const error = await screen.findByTestId("section-error-health");
    expect(error.textContent).toMatch(/unavailable \(401\)/i);
    expect(error.textContent).toContain("/api/health");
  });

  it("keeps the last known services figure next to the error when a refresh fails", async () => {
    const first = renderLanding();
    expect(await screen.findByText("2/3")).toBeTruthy();
    first.unmount();

    vi.stubGlobal("fetch", routedFetch({ health: jsonResponse({}, 401) }));
    renderLanding();
    const error = await screen.findByTestId("section-error-health");
    expect(error.textContent).toMatch(/unavailable \(401\)/i);
    expect(screen.getByTestId("landing-metric-services").textContent).toContain("2/3");
  });

  it("offers the dashboard's quick actions, and one opens its canvas view", async () => {
    renderLanding();
    expect(screen.getAllByTestId("quick-action")).toHaveLength(4);
    fireEvent.click(screen.getByRole("button", { name: /show my contacts/i }));
    await vi.waitFor(() =>
      expect(mockPushView).toHaveBeenCalledWith(
        expect.objectContaining({ toolName: "render_contact_table" }),
      ),
    );
  });

  it("reports a failed quick action", async () => {
    vi.stubGlobal("fetch", routedFetch({ people: jsonResponse({}, 401) }));
    renderLanding();
    fireEvent.click(screen.getByRole("button", { name: /show my contacts/i }));
    const error = await screen.findByTestId("section-error-actions");
    expect(error.textContent).toMatch(/unavailable \(401\)/i);
    expect(error.textContent).toContain("/api/bridge/api/people");
  });

  it("keeps the suggested prompts, and a chip hands its text to the composer", () => {
    const onPrompt = vi.fn();
    renderLanding({ onPrompt });
    const chips = screen.getAllByTestId("suggested-prompt");
    expect(chips.map((c) => c.textContent)).toEqual(PROMPTS);
    fireEvent.click(chips[1]);
    expect(onPrompt).toHaveBeenCalledWith("What happened today?");
  });

  it("polls health every 30 seconds while shown, and stops once it unmounts", () => {
    vi.useFakeTimers();
    const fetchMock = routedFetch();
    vi.stubGlobal("fetch", fetchMock);
    const healthCalls = () =>
      fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/health")).length;

    const { unmount } = renderLanding();
    expect(healthCalls()).toBe(1);
    act(() => {
      vi.advanceTimersByTime(30000);
    });
    expect(healthCalls()).toBe(2);

    unmount();
    act(() => {
      vi.advanceTimersByTime(90000);
    });
    expect(healthCalls()).toBe(2);
  });
});
