import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { VisualStateProvider, useVisualState } from "@/hooks/use-visual-state";
import { useDashboardAgent } from "@/hooks/use-dashboard-agent";

vi.mock("@/lib/dashboard/error-reporter", () => ({ reportDashboardError: vi.fn() }));

/**
 * A failed canvas update used to go to the browser console and nowhere else:
 * the operator saw the old view, no spinner, and no reason. The hook now
 * hands the failure back to the canvas, with a way to try again.
 */

const messages = [
  { role: "user", content: "show me the open tasks" },
  { role: "assistant", content: "here they are" },
];

function Probe() {
  const { error, retry, dismiss } = useDashboardAgent();
  const { notifyConversationUpdate } = useVisualState();
  return (
    <>
      <span data-testid="agent-error">{error ?? ""}</span>
      <button data-testid="notify" onClick={() => notifyConversationUpdate(messages)}>
        notify
      </button>
      <button data-testid="retry" onClick={retry}>
        retry
      </button>
      <button data-testid="dismiss" onClick={dismiss}>
        dismiss
      </button>
    </>
  );
}

function renderProbe() {
  return render(
    <VisualStateProvider>
      <Probe />
    </VisualStateProvider>,
  );
}

const generateCalls = (mock: ReturnType<typeof vi.fn>) =>
  mock.mock.calls.filter(([url]) => String(url) === "/api/dashboard/generate");

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useDashboardAgent — failures reach the canvas", () => {
  it("surfaces the route's error reason", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: "Dashboard service temporarily unavailable" }), {
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderProbe();

    fireEvent.click(screen.getByTestId("notify"));

    await waitFor(() =>
      expect(screen.getByTestId("agent-error")).toHaveTextContent(
        "Dashboard service temporarily unavailable",
      ),
    );
  });

  it("surfaces an HTTP failure with its status", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ error: "Too many requests" }), { status: 429 }));
    vi.stubGlobal("fetch", fetchMock);
    renderProbe();

    fireEvent.click(screen.getByTestId("notify"));

    await waitFor(() => expect(screen.getByTestId("agent-error")).toHaveTextContent(/429/));
  });

  it("retries the same request and clears the error when it succeeds", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: "Dashboard service temporarily unavailable" }), {
          status: 200,
        }),
      )
      .mockResolvedValue(new Response(JSON.stringify({ html: "<div>ok</div>", type: "html" })));
    vi.stubGlobal("fetch", fetchMock);
    renderProbe();

    fireEvent.click(screen.getByTestId("notify"));
    await waitFor(() => expect(screen.getByTestId("agent-error")).not.toBeEmptyDOMElement());

    fireEvent.click(screen.getByTestId("retry"));

    await waitFor(() => expect(generateCalls(fetchMock)).toHaveLength(2));
    expect(JSON.parse(String(generateCalls(fetchMock)[1][1]?.body))).toEqual({ messages });
    await waitFor(() => expect(screen.getByTestId("agent-error")).toBeEmptyDOMElement());
  });

  it("shows a thrown error's message, not its class name", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    renderProbe();

    fireEvent.click(screen.getByTestId("notify"));

    await waitFor(() => expect(screen.getByTestId("agent-error")).toHaveTextContent("network down"));
    expect(screen.getByTestId("agent-error")).not.toHaveTextContent("Error:");
  });

  it("can be dismissed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: "nope" }), { status: 200 })),
    );
    renderProbe();
    fireEvent.click(screen.getByTestId("notify"));
    await waitFor(() => expect(screen.getByTestId("agent-error")).toHaveTextContent("nope"));

    fireEvent.click(screen.getByTestId("dismiss"));

    expect(screen.getByTestId("agent-error")).toBeEmptyDOMElement();
  });

  it("reports nothing when triage decides no update is needed", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    renderProbe();

    fireEvent.click(screen.getByTestId("notify"));

    await waitFor(() => expect(generateCalls(fetchMock)).toHaveLength(1));
    expect(screen.getByTestId("agent-error")).toBeEmptyDOMElement();
  });
});
