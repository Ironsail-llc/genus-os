/**
 * Every composer control names itself. Plan and Deep were icon buttons with a
 * tooltip and no accessible name, so a screen reader announced "button".
 */
import { render, screen } from "@testing-library/react";
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
