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

function Canvas({ name = "canvas" }: { name?: string }) {
  const { error, retry } = useDashboardAgent();
  return (
    <>
      <span data-testid={`${name}-error`}>{error ?? ""}</span>
      <button data-testid={`${name}-retry`} onClick={retry} />
    </>
  );
}

function Spinner() {
  const { isUpdating } = useVisualState();
  return <span data-testid="updating">{isUpdating ? "yes" : "no"}</span>;
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

  it("shows a failed generation on every mounted canvas, not only the one that asked", async () => {
    const fetcher = vi.fn(async () => Response.json({ error: "generation failed" }));
    vi.stubGlobal("fetch", fetcher);
    render(
      <VisualStateProvider>
        <Chat />
        <Canvas name="hidden" />
        <Canvas name="visible" />
      </VisualStateProvider>,
    );
    fireEvent.click(screen.getByTestId("first"));
    await waitFor(() => expect(screen.getByTestId("visible-error").textContent).toBe("generation failed"));
    expect(screen.getByTestId("hidden-error").textContent).toBe("generation failed");
    expect(generateCalls(fetcher)).toBe(1);
  });

  it("retries from a canvas that did not make the failed request", async () => {
    const fetcher = vi.fn(async () => Response.json({ error: "generation failed" }));
    vi.stubGlobal("fetch", fetcher);
    render(
      <VisualStateProvider>
        <Chat />
        <Canvas name="hidden" />
        <Canvas name="visible" />
      </VisualStateProvider>,
    );
    fireEvent.click(screen.getByTestId("first"));
    await waitFor(() => expect(screen.getByTestId("visible-error").textContent).toBe("generation failed"));
    fireEvent.click(screen.getByTestId("visible-retry"));
    await waitFor(() => expect(generateCalls(fetcher)).toBe(2));
  });

  it("hands the turn to the canvas that remains when the one generating it unmounts", async () => {
    let answer!: (res: Response) => void;
    const fetcher = vi.fn((_url: string | URL | Request, init?: RequestInit) => {
      if (generateCalls(fetcher) === 1) {
        // The first request never answers; it is aborted by its canvas leaving.
        return new Promise<Response>((_resolve, reject) =>
          init?.signal?.addEventListener("abort", () =>
            reject(Object.assign(new Error("aborted"), { name: "AbortError" })),
          ),
        );
      }
      return new Promise<Response>((resolve) => { answer = resolve; });
    });
    vi.stubGlobal("fetch", fetcher);
    const tree = (rail: boolean, dashboard: boolean) => (
      <VisualStateProvider>
        <Chat />
        <Spinner />
        {dashboard && <Canvas name="dashboard" />}
        {rail && <Canvas name="rail" />}
      </VisualStateProvider>
    );
    const { rerender } = render(tree(true, false));
    fireEvent.click(screen.getByTestId("first"));
    await waitFor(() => expect(generateCalls(fetcher)).toBe(1));
    expect(screen.getByTestId("updating").textContent).toBe("yes");

    // The operator closes the rail and opens the dashboard's AI canvas.
    rerender(tree(false, true));
    await waitFor(() => expect(generateCalls(fetcher)).toBe(2));
    answer(new Response(null, { status: 204 }));
    await waitFor(() => expect(screen.getByTestId("updating").textContent).toBe("no"));
  });

  it("puts the spinner down when the only canvas generating a turn unmounts", async () => {
    const fetcher = vi.fn((_url: string | URL | Request, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) =>
        init?.signal?.addEventListener("abort", () =>
          reject(Object.assign(new Error("aborted"), { name: "AbortError" })),
        ),
      ),
    );
    vi.stubGlobal("fetch", fetcher);
    const tree = (rail: boolean) => (
      <VisualStateProvider>
        <Chat />
        <Spinner />
        {rail && <Canvas name="rail" />}
      </VisualStateProvider>
    );
    const { rerender } = render(tree(true));
    fireEvent.click(screen.getByTestId("first"));
    await waitFor(() => expect(screen.getByTestId("updating").textContent).toBe("yes"));
    rerender(tree(false));
    await waitFor(() => expect(screen.getByTestId("updating").textContent).toBe("no"));
  });
});
