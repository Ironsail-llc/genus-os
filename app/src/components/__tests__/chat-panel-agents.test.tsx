import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { ChatPanel } from "../chat-panel";
import { CHAT_AGENT_STORAGE_KEY } from "@/lib/chat/agent-session";

/**
 * Talking to an agent that is not the main one, from the chat the operator
 * already has open.
 *
 * The single invariant everything here defends: **the default agent sends no
 * `agent` at all.** The engine resolves an absent session key to
 * `main_session_key`, the session the operator's webchat and Telegram share on
 * purpose, and every message they have ever sent lives in it. A panel that
 * "helpfully" started naming that session explicitly would be correct right up
 * until an instance sets `ROBOTHOR_DEFAULT_CHAT_AGENT`, at which point the
 * operator's history is in a session the Helm no longer opens. So the bodies
 * are asserted key by key, not with `toMatchObject`.
 *
 * The switcher offers only `chattable` agents — the ones that HOLD a session
 * between runs, plus the configured default. An isolated worker would answer
 * from a session it forgets the moment the run ends, which is not a
 * conversation and must not be offered as one.
 */

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({ notifyConversationUpdate: vi.fn(), setRender: vi.fn() }),
}));

vi.mock("@/hooks/use-throttle", () => ({ useThrottle: (value: string) => value }));

const MANIFESTS = {
  default_agent: "main",
  count: 3,
  broken: [],
  agents: [
    { id: "main", name: "Robothor", chattable: true, session_target: "isolated" },
    { id: "scheduler", name: "Scheduler", chattable: true, session_target: "persistent" },
    { id: "invoice-worker", name: "Invoice Worker", chattable: false, session_target: "isolated" },
  ],
};

function doneStream(text: string) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(
        encoder.encode(`event: done\ndata: ${JSON.stringify({ text })}\n\n`)
      );
      controller.close();
    },
  });
}

interface MockOptions {
  /** `"unreachable"` makes the listing fetch reject, as a dead bridge does. */
  manifests?: { status: number; body: unknown } | "unreachable";
  historyFor?: (agent: string) => Array<{ role: string; content: string }>;
}

let calls: Array<{ url: string; init?: RequestInit }> = [];

function setupFetch(options: MockOptions = {}) {
  const manifests = options.manifests ?? { status: 200, body: MANIFESTS };
  global.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (url.startsWith("/api/bridge/api/agent-manifests")) {
      if (manifests === "unreachable") throw new Error("bridge unreachable");
      return {
        ok: manifests.status < 400,
        status: manifests.status,
        json: async () => manifests.body,
      };
    }
    if (url.startsWith("/api/chat/history")) {
      const agent = new URL(url, "http://helm.test").searchParams.get("agent") ?? "";
      return {
        ok: true,
        json: async () => ({ messages: options.historyFor?.(agent) ?? [] }),
      };
    }
    if (url.startsWith("/api/chat/plan/status")) return { ok: true, json: async () => ({ active: false }) };
    if (url.startsWith("/api/chat/deep/status")) return { ok: true, json: async () => ({ active: false }) };
    if (url === "/api/chat/send") return { ok: true, body: doneStream("Sure.") };
    return { ok: true, json: async () => ({}) };
  });
}

function sentBodies(url: string): Array<Record<string, unknown>> {
  return calls
    .filter((call) => call.url === url && call.init?.body)
    .map((call) => JSON.parse(String(call.init!.body)));
}

function historyUrls(): string[] {
  return calls.filter((call) => call.url.startsWith("/api/chat/history")).map((call) => call.url);
}

async function send(text: string) {
  await waitFor(() => expect(screen.getByTestId("chat-input")).toBeTruthy());
  fireEvent.change(screen.getByTestId("chat-input"), { target: { value: text } });
  fireEvent.click(screen.getByTestId("send-button"));
  await waitFor(() => expect(sentBodies("/api/chat/send").length).toBeGreaterThan(0));
}

async function switcher(): Promise<HTMLSelectElement> {
  await waitFor(() => expect(screen.getByTestId("agent-switcher")).toBeTruthy());
  return screen.getByTestId("agent-switcher") as HTMLSelectElement;
}

describe("ChatPanel — choosing which agent to talk to", () => {
  beforeEach(() => {
    calls = [];
    window.localStorage.clear();
    setupFetch();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("offers the chattable agents only, and starts on the default", async () => {
    render(<ChatPanel />);

    const select = await switcher();

    expect(select.value).toBe("main");
    expect([...select.options].map((option) => option.value)).toEqual(["main", "scheduler"]);
    expect([...select.options].map((option) => option.textContent)).toEqual([
      "Robothor",
      "Scheduler",
    ]);
  });

  it("sends NO agent for the default — the shared main session is untouched", async () => {
    render(<ChatPanel />);
    await switcher();

    await send("how's everything?");

    expect(sentBodies("/api/chat/send")).toEqual([{ message: "how's everything?" }]);
  });

  it("loads the default agent's history with no agent on the query", async () => {
    render(<ChatPanel />);
    await switcher();

    expect(historyUrls()).toEqual(["/api/chat/history"]);
  });

  it("names the agent in the body once somebody else is chosen", async () => {
    render(<ChatPanel />);
    const select = await switcher();

    fireEvent.change(select, { target: { value: "scheduler" } });
    await send("what's on today?");

    expect(sentBodies("/api/chat/send")).toEqual([
      { message: "what's on today?", agent: "scheduler" },
    ]);
  });

  it("reloads history for the agent that was switched to", async () => {
    setupFetch({
      historyFor: (agent) =>
        agent === "scheduler"
          ? [{ role: "assistant", content: "Your 9am moved." }]
          : [{ role: "assistant", content: "Morning." }],
    });
    render(<ChatPanel />);
    const select = await switcher();
    await waitFor(() => expect(screen.getByText("Morning.")).toBeTruthy());

    fireEvent.change(select, { target: { value: "scheduler" } });

    await waitFor(() => expect(screen.getByText("Your 9am moved.")).toBeTruthy());
    expect(screen.queryByText("Morning.")).toBeNull();
    expect(historyUrls()).toEqual(["/api/chat/history", "/api/chat/history?agent=scheduler"]);
  });

  it("goes back to sending no agent when the default is chosen again", async () => {
    render(<ChatPanel />);
    const select = await switcher();

    fireEvent.change(select, { target: { value: "scheduler" } });
    await waitFor(() => expect(select.value).toBe("scheduler"));
    fireEvent.change(select, { target: { value: "main" } });
    await waitFor(() => expect(select.value).toBe("main"));
    await send("back to you");

    expect(sentBodies("/api/chat/send")).toEqual([{ message: "back to you" }]);
  });

  it("remembers the choice in this browser, and nowhere else", async () => {
    const view = render(<ChatPanel />);
    const select = await switcher();

    fireEvent.change(select, { target: { value: "scheduler" } });
    await waitFor(() =>
      expect(window.localStorage.getItem(CHAT_AGENT_STORAGE_KEY)).toBe("scheduler")
    );
    expect(window.location.search).toBe("");

    view.unmount();
    calls = [];
    render(<ChatPanel />);

    const restored = await switcher();
    expect(restored.value).toBe("scheduler");
    expect(historyUrls()).toEqual(["/api/chat/history?agent=scheduler"]);
  });

  it("falls back to the default when the remembered agent is no longer chattable", async () => {
    window.localStorage.setItem(CHAT_AGENT_STORAGE_KEY, "invoice-worker");
    render(<ChatPanel />);

    const select = await switcher();

    expect(select.value).toBe("main");
    await send("hello");
    expect(sentBodies("/api/chat/send")).toEqual([{ message: "hello" }]);
  });

  it("shows no switcher, and changes nothing, when the fleet listing is refused", async () => {
    setupFetch({ manifests: { status: 403, body: { detail: "operator only" } } });
    render(<ChatPanel />);

    await send("hello");

    expect(screen.queryByTestId("agent-switcher")).toBeNull();
    expect(sentBodies("/api/chat/send")).toEqual([{ message: "hello" }]);
    expect(historyUrls()).toEqual(["/api/chat/history"]);
  });

  it("does not strand a remembered agent behind a switcher nobody can see", async () => {
    // No listing means no control on screen. A remembered agent left standing
    // would send every message to a conversation the person cannot see they
    // are in, and cannot leave.
    window.localStorage.setItem(CHAT_AGENT_STORAGE_KEY, "scheduler");
    setupFetch({ manifests: { status: 403, body: { detail: "operator only" } } });
    render(<ChatPanel />);

    await send("hello");

    expect(screen.queryByTestId("agent-switcher")).toBeNull();
    expect(sentBodies("/api/chat/send")).toEqual([{ message: "hello" }]);
  });

  it("does the same when the bridge cannot be reached at all", async () => {
    window.localStorage.setItem(CHAT_AGENT_STORAGE_KEY, "scheduler");
    setupFetch({ manifests: "unreachable" });
    render(<ChatPanel />);

    await waitFor(() => expect(historyUrls()).toContain("/api/chat/history"));
    await send("hello");

    expect(screen.queryByTestId("agent-switcher")).toBeNull();
    expect(sentBodies("/api/chat/send")).toEqual([{ message: "hello" }]);
  });

  it("shows no switcher when the default agent is the only one worth offering", async () => {
    setupFetch({
      manifests: {
        status: 200,
        body: { ...MANIFESTS, agents: [MANIFESTS.agents[0], MANIFESTS.agents[2]] },
      },
    });
    render(<ChatPanel />);

    await send("hello");

    expect(screen.queryByTestId("agent-switcher")).toBeNull();
  });
});
