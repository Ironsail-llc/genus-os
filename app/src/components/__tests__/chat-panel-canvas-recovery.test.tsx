import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { ChatPanel } from "../chat-panel";

const visual = vi.hoisted(() => ({
  notifyConversationUpdate: vi.fn(),
  setRender: vi.fn(),
}));
vi.mock("@/hooks/use-visual-state", () => ({ useVisualState: () => visual }));
vi.mock("@/hooks/use-throttle", () => ({ useThrottle: (value: string) => value }));

function sseWithoutTerminal(text: string) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(`event: delta\ndata: ${JSON.stringify({ text })}\n\n`));
      controller.close();
    },
  });
}

// A long turn's stream can close before its `done` event; the reply is then
// recovered from the run's recorded outcome. That reply must still reach the
// canvas, or every slow request silently loses its visual.
describe("ChatPanel — canvas markers on a recovered reply", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    visual.notifyConversationUpdate.mockReset();
    visual.setRender.mockReset();
  });

  function stubBackend(recovered: string) {
    global.fetch = vi.fn().mockImplementation(async (url: string) => {
      if (url.startsWith("/api/chat/outcome?")) {
        return { ok: true, json: async () => ({ terminal: true, state: "completed", text: recovered, reconciliation_pending: false }) };
      }
      if (url === "/api/chat/send") return { ok: true, body: sseWithoutTerminal("Working on it") };
      if (url === "/api/chat/history") return { ok: true, json: async () => ({ messages: [] }) };
      if (url === "/api/chat/plan/status") return { ok: true, json: async () => ({ active: false }) };
      return { ok: false, status: 404 };
    });
  }

  async function send(text: string) {
    render(<ChatPanel />);
    // let the mount-time history load settle, as it has before anyone types
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: text } });
    fireEvent.click(screen.getByTestId("send-button"));
  }

  it("hands [DASHBOARD:…] data to the canvas", async () => {
    stubBackend('Here is the breakdown.\n\n[DASHBOARD:{"intent":"bar_chart","data":{"title":"Tasks","values":[1,2]}}]\n\nTwo things stand out.');
    await send("Chart my tasks on the canvas");
    await waitFor(() => expect(screen.getByTestId("message-assistant").textContent).toContain("Two things stand out."));
    expect(screen.getByTestId("message-assistant").textContent).not.toContain("[DASHBOARD");
    await waitFor(() => expect(visual.notifyConversationUpdate).toHaveBeenCalled());
    const [messages, agentData] = visual.notifyConversationUpdate.mock.calls.at(-1)!;
    expect(agentData).toMatchObject({ title: "Tasks", values: [1, 2] });
    expect(messages).toContainEqual({ role: "user", content: "Chart my tasks on the canvas" });
  });

  it("hands [RENDER:…] components to the canvas", async () => {
    stubBackend('Your contacts:\n[RENDER:render_contact_table:{"contacts":[]}]');
    await send("Show my contacts");
    await waitFor(() => expect(visual.setRender).toHaveBeenCalledWith({ component: "render_contact_table", props: { contacts: [] } }));
  });
});
