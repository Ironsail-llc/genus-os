/**
 * A room's chat loads its outbox module on demand. If that chunk will not
 * load (a deploy replaced it, the network blinked), the panel tries once more
 * and then says so, with the composer shut, instead of going quietly dead.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({ notifyConversationUpdate: vi.fn(), setRender: vi.fn() }),
}));
vi.mock("@/hooks/use-throttle", () => ({ useThrottle: (value: string) => value }));

const load = vi.hoisted(() => ({ failures: 0, attempts: 0 }));
vi.mock("@/lib/chat/room-outbox", async () => {
  load.attempts += 1;
  if (load.attempts <= load.failures) throw new Error("Failed to fetch dynamically imported module");
  return await vi.importActual<typeof import("@/lib/chat/room-outbox")>("@/lib/chat/room-outbox");
});

import { ChatPanel } from "../chat-panel";
import { REPORT_ID, reply } from "@/lib/rooms/__tests__/fixtures";

const sends: Array<Record<string, unknown>> = [];

beforeEach(() => {
  load.attempts = 0;
  sends.length = 0;
  vi.resetModules();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/api/chat/send") {
        sends.push(JSON.parse(String(init?.body ?? "{}")));
        return new Response(`event: done\ndata: {"text":"Noted."}\n\n`, { headers: { "content-type": "text/event-stream" } });
      }
      return reply({ messages: [] });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

async function type(text: string) {
  const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
  await act(async () => {
    fireEvent.change(input, { target: { value: text } });
    fireEvent.keyDown(input, { key: "Enter" });
  });
}

describe("a room chat whose outbox module will not load", () => {
  it("says so after one retry, with the composer shut", async () => {
    load.failures = 99;
    render(<ChatPanel variant="page" room={{ id: REPORT_ID }} />);
    const notice = await screen.findByTestId("room-chat-load-failed");
    expect(notice).toHaveTextContent(/couldn.t load this workspace.s chat/i);
    expect(notice).toHaveTextContent(/reload/i);
    expect(load.attempts).toBe(2);
    expect(screen.getByTestId("chat-input")).toBeDisabled();
  });

  it("recovers on the retry, and the message is sent", async () => {
    load.failures = 1;
    render(<ChatPanel variant="page" room={{ id: REPORT_ID }} />);
    await screen.findByTestId("room-chat-empty");
    await waitFor(() => expect(load.attempts).toBe(2));
    await type("Hello");
    await waitFor(() => expect(sends).toHaveLength(1));
    expect(screen.queryByTestId("room-chat-load-failed")).toBeNull();
  });
});
