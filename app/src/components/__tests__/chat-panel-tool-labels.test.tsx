/**
 * Every composer control names itself. Plan and Deep were icon buttons with a
 * tooltip and no accessible name, so a screen reader announced "button".
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatPanel } from "../chat-panel";

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({ notifyConversationUpdate: vi.fn(), setRender: vi.fn() }),
}));
vi.mock("@/hooks/use-throttle", () => ({ useThrottle: (value: string) => value }));

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ messages: [], agents: [], active: false }) })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe.each([
  ["desktop", false],
  ["mobile", true],
])("ChatPanel composer controls (%s)", (_name, mobile) => {
  it("labels Plan, Deep and Tools", () => {
    stubFetch();
    render(<ChatPanel mobile={mobile} />);

    expect(screen.getByRole("button", { name: "Plan mode" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Deep mode" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Tools and MCP servers" })).toBeVisible();
  });
});

describe("ChatPanel Tools button and the default agent", () => {
  it("asks about the fleet's default agent, never a guessed one", async () => {
    let releaseListing!: () => void;
    const gate = new Promise<void>((resolve) => { releaseListing = resolve; });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/bridge/api/agent-manifests") {
        await gate;
        return {
          ok: true,
          status: 200,
          json: async () => ({ default_agent: "example-main", agents: [{ id: "example-main", name: "Example", chattable: true }] }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ messages: [], agents: [], active: false, servers: [] }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ChatPanel />);

    const button = screen.getByRole("button", { name: "Tools and MCP servers" });
    expect(button).toBeDisabled();

    releaseListing();
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() =>
      expect(fetchMock.mock.calls.map(([url]) => String(url))).toContain(
        "/api/bridge/api/agents/example-main/tools"
      )
    );
    expect(fetchMock.mock.calls.map(([url]) => String(url))).not.toContain(
      "/api/bridge/api/agents/main/tools"
    );
  });
});
