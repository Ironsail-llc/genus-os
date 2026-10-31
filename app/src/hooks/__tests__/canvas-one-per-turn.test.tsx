/**
 * One canvas generation per chat turn.
 *
 * Each mounted canvas (the chat's rail and the dashboard's AI canvas) runs its
 * own dashboard agent. Both used to answer the same turn, so a long reply with
 * both open paid for two triage+generate pipelines whose results land in the
 * same shared slot.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, act, waitFor } from "@testing-library/react";
import { VisualStateProvider, useVisualState } from "@/hooks/use-visual-state";
import { useDashboardAgent } from "@/hooks/use-dashboard-agent";

function Canvas() {
  useDashboardAgent();
  return null;
}

let notify: ReturnType<typeof useVisualState>["notifyConversationUpdate"];
function Chat() {
  notify = useVisualState().notifyConversationUpdate;
  return null;
}

const turn = (reply: string) => [
  { role: "user", content: "Show me the week" },
  { role: "assistant", content: reply },
];

function generateCalls(fetcher: ReturnType<typeof vi.fn>) {
  return fetcher.mock.calls.filter(([url]) => String(url) === "/api/dashboard/generate").length;
}

describe("canvas generation per turn", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("asks for one generation when two canvases are mounted", async () => {
    const fetcher = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetcher);
    render(
      <VisualStateProvider>
        <Chat />
        <Canvas />
        <Canvas />
      </VisualStateProvider>,
    );
    act(() => notify(turn("x".repeat(250))));
    await waitFor(() => expect(generateCalls(fetcher)).toBeGreaterThan(0));
    await new Promise((r) => setTimeout(r, 400));
    expect(generateCalls(fetcher)).toBe(1);
  });

  it("asks once per turn, each turn", async () => {
    const fetcher = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetcher);
    render(
      <VisualStateProvider>
        <Chat />
        <Canvas />
        <Canvas />
      </VisualStateProvider>,
    );
    act(() => notify(turn("first ".repeat(50))));
    await waitFor(() => expect(generateCalls(fetcher)).toBe(1));
    act(() => notify(turn("second ".repeat(50))));
    await waitFor(() => expect(generateCalls(fetcher)).toBe(2));
    await new Promise((r) => setTimeout(r, 400));
    expect(generateCalls(fetcher)).toBe(2);
  });

  it("asks for nothing while no canvas is mounted (the canvas toggle closed)", async () => {
    const fetcher = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetcher);
    render(
      <VisualStateProvider>
        <Chat />
      </VisualStateProvider>,
    );
    act(() => notify(turn("x".repeat(250))));
    await new Promise((r) => setTimeout(r, 400));
    expect(generateCalls(fetcher)).toBe(0);
  });
});
