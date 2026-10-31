/**
 * One canvas generation per chat turn.
 *
 * Each mounted canvas (the chat's rail and the dashboard's AI canvas) runs its
 * own dashboard agent. Both used to answer the same turn, so a long reply with
 * both open paid for two triage+generate pipelines whose results land in the
 * same shared slot.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, screen, waitFor } from "@testing-library/react";
import { VisualStateProvider, useVisualState } from "@/hooks/use-visual-state";
import { useDashboardAgent } from "@/hooks/use-dashboard-agent";

function Canvas() {
  useDashboardAgent();
  return null;
}

const turn = (reply: string) => [
  { role: "user", content: "Show me the week" },
  { role: "assistant", content: reply },
];

/** The chat's side: each button ends one turn with a long reply. */
function Chat() {
  const { notifyConversationUpdate } = useVisualState();
  return (
    <>
      <button data-testid="first" onClick={() => notifyConversationUpdate(turn("first ".repeat(50)))} />
      <button data-testid="second" onClick={() => notifyConversationUpdate(turn("second ".repeat(50)))} />
    </>
  );
}

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
    fireEvent.click(screen.getByTestId("first"));
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
    fireEvent.click(screen.getByTestId("first"));
    await waitFor(() => expect(generateCalls(fetcher)).toBe(1));
    fireEvent.click(screen.getByTestId("second"));
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
    fireEvent.click(screen.getByTestId("first"));
    await new Promise((r) => setTimeout(r, 400));
    expect(generateCalls(fetcher)).toBe(0);
  });
});
