import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { VisualStateProvider, useVisualState } from "@/hooks/use-visual-state";
import { useDashboardAgent } from "@/hooks/use-dashboard-agent";

/**
 * `isUpdating` drives the canvas "Updating" spinner, and only the dashboard
 * agent clears it. Since the AI canvas mounts on request, a chat reply can
 * arrive with no agent listening — the flag must not latch on in that case,
 * or the canvas would open showing a spinner for work nobody is doing.
 */

const messages = [
  { role: "user", content: "how are the services doing" },
  { role: "assistant", content: "all nominal" },
];

function AgentHost() {
  useDashboardAgent();
  return null;
}

function Readout() {
  const { isUpdating, notifyConversationUpdate } = useVisualState();
  return (
    <>
      <span data-testid="is-updating">{isUpdating ? "yes" : "no"}</span>
      <button data-testid="notify" onClick={() => notifyConversationUpdate(messages)}>
        notify
      </button>
    </>
  );
}

function renderProbe({ withAgent }: { withAgent: boolean }) {
  return render(
    <VisualStateProvider>
      {withAgent && <AgentHost />}
      <Readout />
    </VisualStateProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("notifyConversationUpdate", () => {
  it("does not latch the updating flag when no dashboard agent is mounted", async () => {
    renderProbe({ withAgent: false });

    fireEvent.click(screen.getByTestId("notify"));

    await waitFor(() => expect(screen.getByTestId("is-updating").textContent).toBe("no"));
  });

  it("raises the updating flag while a mounted agent regenerates", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    renderProbe({ withAgent: true });

    fireEvent.click(screen.getByTestId("notify"));

    await waitFor(() => expect(screen.getByTestId("is-updating").textContent).toBe("yes"));
  });
});
