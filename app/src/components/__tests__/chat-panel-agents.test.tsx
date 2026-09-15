import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { ChatPanel } from "../chat-panel";
import { CHAT_AGENT_STORAGE_KEY } from "@/lib/chat/agent-session";

/**
 * Talking to an agent that is not the main one, from the chat the operator
 * already has open.
 *
 * Two invariants, and every test here defends one of them.
 *
 * **The default agent sends no `agent` at all.** The engine resolves an absent
 * session key to `main_session_key`, the session the operator's webchat and
 * Telegram share on purpose, and every message they have ever sent lives in it.
 * A panel that "helpfully" started naming that session explicitly would be
 * correct right up until an instance sets `ROBOTHOR_MAIN_SESSION_KEY`, at which
 * point the operator's history is in a session the Helm no longer opens. So the
 * bodies are asserted key by key, not with `toMatchObject`, and the default
 * option's value is the EMPTY STRING rather than an id — "sends no key" is then
 * structurally true instead of a comparison that can drift.
 *
 * **The header never names an agent the message will not reach.** The failure
 * this guards is not a fork, it is a MERGE: the operator types into what the
 * header calls "Scheduler" and the text is appended to the shared main session.
 * Three ways in, all closed here — the listing omitting the default agent, the
 * listing omitting `default_agent` entirely, and a remembered agent routed
 * before the listing could veto it.
 *
 * The switcher offers only `chattable` agents — the ones that HOLD a session
 * between runs, plus the main agent. An isolated worker would answer from a
 * session it forgets the moment the run ends, which is not a conversation and
 * must not be offered as one.
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
      controller.enqueue(encoder.encode(`event: done\ndata: ${JSON.stringify({ text })}\n\n`));
      controller.close();
    },
  });
}

interface MockOptions {
  /** `"unreachable"` makes the listing fetch reject, as a dead bridge does. */
  manifests?: { status: number; body: unknown } | "unreachable";
  historyFor?: (agent: string) => Array<{ role: string; content: string }>;
  /** Hold the history response for this agent until its gate is released. */
  gate?: string;
}

let calls: Array<{ url: string; init?: RequestInit }> = [];
const gates = new Map<string, () => void>();

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
      if (options.gate === agent) await new Promise<void>((release) => gates.set(agent, release));
      return { ok: true, json: async () => ({ messages: options.historyFor?.(agent) ?? [] }) };
    }
    if (url.startsWith("/api/chat/plan/status"))
      return { ok: true, json: async () => ({ active: false }) };
    if (url.startsWith("/api/chat/deep/status"))
      return { ok: true, json: async () => ({ active: false }) };
    if (url === "/api/chat/send") return { ok: true, body: doneStream("Sure.") };
    return { ok: true, json: async () => ({}) };
  });
}

function sentBodies(url: string): Array<Record<string, unknown>> {
  return calls
    .filter((call) => call.url === url && call.init?.body)
    .map((call) => JSON.parse(String(call.init!.body)));
}

const urlsFor = (prefix: string) =>
  calls.filter((call) => call.url.startsWith(prefix)).map((call) => call.url);

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

/** The listing has landed and the panel has settled on an agent. */
async function listingApplied() {
  await waitFor(() => expect(urlsFor("/api/bridge/api/agent-manifests").length).toBe(1));
  await waitFor(() => expect(screen.getByTestId("chat-input")).toBeTruthy());
}

describe("ChatPanel — choosing which agent to talk to", () => {
  beforeEach(() => {
    calls = [];
    gates.clear();
    window.localStorage.clear();
    setupFetch();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("offers the chattable agents, with the main agent first and keyless", async () => {
    render(<ChatPanel />);

    const select = await switcher();

    // The default option's value is "" on purpose: the thing it means is
    // "send no session key", and an id here is a value that can drift from
    // that meaning. Its LABEL is the main agent's name from the listing.
    expect(select.value).toBe("");
    expect([...select.options].map((option) => option.value)).toEqual(["", "scheduler"]);
    expect([...select.options].map((option) => option.textContent)).toEqual([
      "Robothor",
      "Scheduler",
    ]);
    expect(screen.queryByTestId("agent-switcher-note")).toBeNull();
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

    expect(urlsFor("/api/chat/history")).toEqual(["/api/chat/history"]);
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
    expect(urlsFor("/api/chat/history")).toEqual([
      "/api/chat/history",
      "/api/chat/history?agent=scheduler",
    ]);
  });

  it("goes back to sending no agent when the default is chosen again", async () => {
    render(<ChatPanel />);
    const select = await switcher();

    fireEvent.change(select, { target: { value: "scheduler" } });
    await waitFor(() => expect(select.value).toBe("scheduler"));
    fireEvent.change(select, { target: { value: "" } });
    await waitFor(() => expect(select.value).toBe(""));
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
    // Two loads, and in this order: the remembered agent is NOT routed to until
    // the listing has confirmed it is still chattable (see the next test for
    // why). The first load is the main session, which the operator owns.
    await waitFor(() =>
      expect(urlsFor("/api/chat/history")).toEqual([
        "/api/chat/history",
        "/api/chat/history?agent=scheduler",
      ])
    );
  });
});

describe("ChatPanel — the header must never name an agent the message misses", () => {
  beforeEach(() => {
    calls = [];
    gates.clear();
    window.localStorage.clear();
    setupFetch();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("still offers the default when the listing does not include it", async () => {
    // A YAML typo in the main manifest puts it in `broken`, not `agents`. The
    // default option must survive that: without it the `<select>` value matches
    // no option, the browser shows row 0 (some worker) while the panel is still
    // on the main session, and there is no option that gets the operator back.
    // A manifest typo has taken this instance down before; this is the same
    // blast radius pointed at the operator's history.
    setupFetch({
      manifests: {
        status: 200,
        body: {
          default_agent: "main",
          agents: [
            { id: "scheduler", name: "Scheduler", chattable: true, session_target: "persistent" },
            { id: "helper", name: "Helper", chattable: true, session_target: "persistent" },
          ],
        },
      },
    });
    render(<ChatPanel />);

    const select = await switcher();

    expect(select.value).toBe("");
    expect([...select.options].map((option) => option.value)).toEqual(["", "scheduler", "helper"]);
    expect(select.options[0].textContent).toBe("main");
    // And it says so, rather than leaving an operator to wonder where the
    // agent they were editing went.
    expect(screen.getByTestId("agent-switcher-note").textContent).toContain("main");

    await send("hi");
    expect(sentBodies("/api/chat/send")).toEqual([{ message: "hi" }]);
  });

  it("keeps a keyless default option when the listing names no default agent", async () => {
    // An app deployed ahead of the bridge. With no `default_agent` there is no
    // id that means "no key", so the option carries none — and never assumes
    // the literal "main", which is a real, separately-addressable agent here.
    setupFetch({
      manifests: { status: 200, body: { ...MANIFESTS, default_agent: undefined } },
    });
    render(<ChatPanel />);

    const select = await switcher();

    expect(select.value).toBe("");
    expect([...select.options].map((option) => option.value)).toEqual(["", "main", "scheduler"]);
    await send("hi");
    expect(sentBodies("/api/chat/send")).toEqual([{ message: "hi" }]);
  });

  it("offers an id the old client-side regex would have refused", async () => {
    // `acme.bot` is a perfectly good manifest id — nothing validates the
    // charset on the read path. The client used to refuse it and fall through
    // to the MAIN session, so a private message to a worker was written into
    // the operator's shared history. The listing is the allowlist; the key is
    // built from the listed id verbatim.
    setupFetch({
      manifests: {
        status: 200,
        body: {
          default_agent: "main",
          agents: [
            { id: "main", name: "Robothor", chattable: true, session_target: "isolated" },
            { id: "acme.bot", name: "Acme Bot", chattable: true, session_target: "persistent" },
          ],
        },
      },
    });
    render(<ChatPanel />);
    const select = await switcher();

    fireEvent.change(select, { target: { value: "acme.bot" } });
    await send("private note");

    expect(sentBodies("/api/chat/send")).toEqual([
      { message: "private note", agent: "acme.bot" },
    ]);
  });

  it("never offers an id that cannot produce a well-formed session key", async () => {
    // A colon is the key's own delimiter. `agent:a:primary:b:primary` is not a
    // shape `_effective_session_key` parses, and an option that cannot be
    // honoured must not be on screen at all — the BFF refuses it outright.
    setupFetch({
      manifests: {
        status: 200,
        body: {
          default_agent: "main",
          agents: [
            { id: "main", name: "Robothor", chattable: true, session_target: "isolated" },
            { id: "scheduler", name: "Scheduler", chattable: true, session_target: "persistent" },
            { id: "a:primary:b", name: "Malformed", chattable: true, session_target: "persistent" },
          ],
        },
      },
    });
    render(<ChatPanel />);

    const select = await switcher();

    expect([...select.options].map((option) => option.value)).toEqual(["", "scheduler"]);
  });
});

describe("ChatPanel — a remembered agent is never routed to unconfirmed", () => {
  beforeEach(() => {
    calls = [];
    gates.clear();
    window.localStorage.clear();
    setupFetch();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("does not read a remembered non-chattable agent even once", async () => {
    // Probe P1. The old panel seeded `agent` from storage before the first
    // render, so the mount-time history, plan and deep requests all carried a
    // key the listing was about to veto.
    window.localStorage.setItem(CHAT_AGENT_STORAGE_KEY, "invoice-worker");
    render(<ChatPanel />);

    await switcher();

    expect(urlsFor("/api/chat/history")).toEqual(["/api/chat/history"]);
    expect(urlsFor("/api/chat/plan/status")).toEqual(["/api/chat/plan/status"]);
    expect(urlsFor("/api/chat/deep/status")).toEqual(["/api/chat/deep/status"]);
  });

  it("never lets a member send a key, on any of the three mount requests", async () => {
    // Probe P2. The listing is operator-gated, so a member gets a 403 and no
    // switcher — but a stale `localStorage` value still rode three requests
    // before the refusal landed. Under `ROBOTHOR_PER_USER_SESSIONS=observe|off`
    // the engine honours a member's requested key verbatim, so the app must not
    // be leaning on `enforce` being the default.
    window.localStorage.setItem(CHAT_AGENT_STORAGE_KEY, "scheduler");
    setupFetch({ manifests: { status: 403, body: { detail: "operator only" } } });
    render(<ChatPanel />);

    await listingApplied();
    await send("hello");

    expect(screen.queryByTestId("agent-switcher")).toBeNull();
    expect(urlsFor("/api/chat/history")).toEqual(["/api/chat/history"]);
    expect(urlsFor("/api/chat/plan/status")).toEqual(["/api/chat/plan/status"]);
    expect(urlsFor("/api/chat/deep/status")).toEqual(["/api/chat/deep/status"]);
    expect(sentBodies("/api/chat/send")).toEqual([{ message: "hello" }]);
  });

  it("does the same when the bridge cannot be reached at all", async () => {
    window.localStorage.setItem(CHAT_AGENT_STORAGE_KEY, "scheduler");
    setupFetch({ manifests: "unreachable" });
    render(<ChatPanel />);

    await listingApplied();
    await send("hello");

    expect(screen.queryByTestId("agent-switcher")).toBeNull();
    expect(urlsFor("/api/chat/history")).toEqual(["/api/chat/history"]);
    expect(sentBodies("/api/chat/send")).toEqual([{ message: "hello" }]);
  });

  it("shows no switcher when the main agent is the only one worth offering", async () => {
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

  it("discards a late history response from the agent the operator left", async () => {
    // Probe P3. Both effects `setMessages([])` and then resolve whenever they
    // resolve; without a cancellation token the ABANDONED agent's transcript
    // lands last and wins, so the operator reads agent A's conversation under
    // agent B's name and the next thing they type goes to B.
    setupFetch({
      gate: "scheduler",
      historyFor: (agent) =>
        agent === "scheduler"
          ? [{ role: "assistant", content: "SCHEDULER SAYS" }]
          : [{ role: "assistant", content: "MAIN SAYS" }],
    });
    render(<ChatPanel />);
    const select = await switcher();
    await waitFor(() => expect(screen.getByText("MAIN SAYS")).toBeTruthy());

    fireEvent.change(select, { target: { value: "scheduler" } });
    await waitFor(() => expect(gates.get("scheduler")).toBeDefined());
    fireEvent.change(select, { target: { value: "" } });
    await waitFor(() => expect(screen.getByText("MAIN SAYS")).toBeTruthy());

    gates.get("scheduler")!();
    await new Promise((resolve) => setTimeout(resolve, 60));

    expect(screen.queryByText("SCHEDULER SAYS")).toBeNull();
    expect(screen.getByText("MAIN SAYS")).toBeTruthy();
  });
});
