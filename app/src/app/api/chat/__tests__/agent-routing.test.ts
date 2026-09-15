import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * The BFF layer: what a browser may say about which agent it is talking to,
 * and what the app does with it.
 *
 * The browser sends an `agent` ID, never a session key. That is the seam: a
 * session key is a shape the engine parses (`agent:<id>:primary`), and a
 * browser that could spell one could spell `agent:main:user:someone-else` too.
 *
 * Three rules, one test group each:
 *
 * 1. **No agent named → no key.** Byte-identical to every request the Helm has
 *    sent since B1, which is what keeps the operator's shared main session
 *    shared.
 * 2. **An agent named is CHECKED, server-side** (`resolveChatAgent`), against
 *    the caller's own operator-gated fleet listing. `chattable` in the browser
 *    is UI; `chat.py` runs whatever agent the key names for any authenticated
 *    caller, so the gate has to be here.
 * 3. **A refusal is a refusal.** A named agent that cannot be keyed, or that
 *    the caller may not address, answers 400/403. It never falls through to
 *    "no key", because "no key" is the operator's own conversation.
 *
 * The canvas prompt is the fourth rule and it rides on the first: it teaches
 * the agent about the dashboard's render markers, and injecting it is a WRITE
 * into whichever session the request names. Pushed into another agent's session
 * it would hand a worker a capability its manifest never granted, so it stays
 * main-only — which is also why a request naming an agent must not trigger it.
 */

const engine = {
  chatSend: vi.fn(),
  chatHistory: vi.fn(),
  planStart: vi.fn(),
  planApprove: vi.fn(),
  planReject: vi.fn(),
  planStatus: vi.fn(),
  deepStart: vi.fn(),
  deepStatus: vi.fn(),
};

const ensureCanvasPromptInjected = vi.fn().mockResolvedValue(undefined);
const resolveChatAgent = vi.fn();

vi.mock("@/lib/engine/server-client", () => ({
  getEngineClient: () => engine,
}));

vi.mock("@/lib/engine/session-state", () => ({
  ensureCanvasPromptInjected: () => ensureCanvasPromptInjected(),
}));

vi.mock("@/lib/chat/agent-guard", () => ({
  resolveChatAgent: (agent: unknown) => resolveChatAgent(agent),
}));

const KEY = "agent:scheduler:primary";

function emptyStream() {
  return new ReadableStream({
    start(controller) {
      controller.close();
    },
  });
}

function post(url: string, body: unknown) {
  return new Request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** Every route this feature threads an agent through, as {call, name}. */
async function routes() {
  return {
    send: (await import("../send/route")).POST,
    history: (await import("../history/route")).GET,
    planStart: (await import("../plan/start/route")).POST,
    planApprove: (await import("../plan/approve/route")).POST,
    planReject: (await import("../plan/reject/route")).POST,
    planStatus: (await import("../plan/status/route")).GET,
    deepStart: (await import("../deep/start/route")).POST,
    deepStatus: (await import("../deep/status/route")).GET,
  };
}

/** Each route driven with `agent: "scheduler"` — for the refusal sweeps. */
async function callEachWithAgent(): Promise<Response[]> {
  const r = await routes();
  return [
    await r.send(post("http://helm.test/x", { message: "hi", agent: "scheduler" })),
    await r.history(new Request("http://helm.test/x?agent=scheduler")),
    await r.planStart(post("http://helm.test/x", { message: "hi", agent: "scheduler" })),
    await r.planApprove(post("http://helm.test/x", { plan_id: "p-1", agent: "scheduler" })),
    await r.planReject(post("http://helm.test/x", { plan_id: "p-1", agent: "scheduler" })),
    await r.planStatus(new Request("http://helm.test/x?agent=scheduler")),
    await r.deepStart(post("http://helm.test/x", { query: "why", agent: "scheduler" })),
    await r.deepStatus(new Request("http://helm.test/x?agent=scheduler")),
  ];
}

describe("the chat BFF routes and the chosen agent", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    engine.chatSend.mockResolvedValue({ body: emptyStream() });
    engine.chatHistory.mockResolvedValue({ messages: [], sessionKey: "agent:main:primary" });
    engine.planStart.mockResolvedValue({ body: emptyStream() });
    engine.planApprove.mockResolvedValue({ body: emptyStream() });
    engine.planReject.mockResolvedValue({ ok: true });
    engine.planStatus.mockResolvedValue({ active: false });
    engine.deepStart.mockResolvedValue({ body: emptyStream() });
    engine.deepStatus.mockResolvedValue({ active: false });
    resolveChatAgent.mockImplementation(async (agent: unknown) =>
      agent ? { ok: true, key: `agent:${String(agent)}:primary` } : { ok: true, key: "" }
    );
  });

  afterEach(() => {
    vi.resetModules();
  });

  it("sends no session key when the body names no agent", async () => {
    const { send } = await routes();

    await send(post("http://helm.test/api/chat/send", { message: "hello" }));

    expect(engine.chatSend).toHaveBeenCalledWith("hello", "");
  });

  it("sends the agent's session key when the body names one", async () => {
    const { send } = await routes();

    await send(post("http://helm.test/api/chat/send", { message: "hi", agent: "scheduler" }));

    expect(engine.chatSend).toHaveBeenCalledWith("hi", KEY);
  });

  it("injects the canvas prompt for the main session and for nobody else", async () => {
    const { send } = await routes();

    await send(post("http://helm.test/api/chat/send", { message: "hello" }));
    expect(ensureCanvasPromptInjected).toHaveBeenCalledTimes(1);

    await send(post("http://helm.test/api/chat/send", { message: "hi", agent: "scheduler" }));
    expect(ensureCanvasPromptInjected).toHaveBeenCalledTimes(1);
  });

  it("reads history from the named agent's session, and injects nothing for it", async () => {
    const { history } = await routes();

    await history(new Request("http://helm.test/api/chat/history?limit=20&agent=scheduler"));

    expect(engine.chatHistory).toHaveBeenCalledWith(20, KEY);
    expect(ensureCanvasPromptInjected).not.toHaveBeenCalled();
  });

  it("reads history from the main session when the query names no agent", async () => {
    const { history } = await routes();

    await history(new Request("http://helm.test/api/chat/history"));

    expect(engine.chatHistory).toHaveBeenCalledWith(50, "");
    expect(ensureCanvasPromptInjected).toHaveBeenCalledTimes(1);
  });

  it("keeps the whole plan round-trip on the same session as the plan it started", async () => {
    const r = await routes();

    await r.planStart(post("http://helm.test/x", { message: "do it", agent: "scheduler" }));
    expect(engine.planStart).toHaveBeenCalledWith("do it", false, KEY);

    await r.planApprove(post("http://helm.test/x", { plan_id: "p-1", agent: "scheduler" }));
    expect(engine.planApprove).toHaveBeenCalledWith("p-1", KEY);

    await r.planReject(post("http://helm.test/x", { plan_id: "p-1", agent: "scheduler" }));
    expect(engine.planReject).toHaveBeenCalledWith("p-1", undefined, KEY);

    await r.planStatus(new Request("http://helm.test/x?agent=scheduler"));
    expect(engine.planStatus).toHaveBeenCalledWith(KEY);
  });

  it("leaves the plan round-trip on the main session when no agent is named", async () => {
    const r = await routes();

    await r.planStart(post("http://helm.test/x", { message: "do it" }));
    expect(engine.planStart).toHaveBeenCalledWith("do it", false, "");

    await r.planStatus(new Request("http://helm.test/x"));
    expect(engine.planStatus).toHaveBeenCalledWith("");
  });

  it("carries the agent through deep mode too", async () => {
    const r = await routes();

    await r.deepStart(post("http://helm.test/x", { query: "why", agent: "scheduler" }));
    expect(engine.deepStart).toHaveBeenCalledWith("why", KEY);

    await r.deepStatus(new Request("http://helm.test/x?agent=scheduler"));
    expect(engine.deepStatus).toHaveBeenCalledWith(KEY);
  });
});

describe("the chat BFF routes refuse an agent rather than falling through to main", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    engine.chatSend.mockResolvedValue({ body: emptyStream() });
    engine.chatHistory.mockResolvedValue({ messages: [], sessionKey: "" });
    engine.planStart.mockResolvedValue({ body: emptyStream() });
    engine.planApprove.mockResolvedValue({ body: emptyStream() });
    engine.planReject.mockResolvedValue({ ok: true });
    engine.planStatus.mockResolvedValue({ active: false });
    engine.deepStart.mockResolvedValue({ body: emptyStream() });
    engine.deepStatus.mockResolvedValue({ active: false });
  });

  afterEach(() => {
    vi.resetModules();
  });

  it("answers 403 on every route when the caller may not address that agent", async () => {
    // The member case, and the crafted-POST case. Nothing reaches the engine:
    // the old code would have called it with the main session's empty key,
    // writing a message meant for a worker into the operator's own history.
    resolveChatAgent.mockResolvedValue({ ok: false, status: 403, error: "not yours" });

    const responses = await callEachWithAgent();

    expect(responses.map((res) => res.status)).toEqual([403, 403, 403, 403, 403, 403, 403, 403]);
    for (const call of Object.values(engine)) {
      expect(call).not.toHaveBeenCalled();
    }
  });

  it("answers 400 on every route when the named agent cannot be keyed", async () => {
    resolveChatAgent.mockResolvedValue({ ok: false, status: 400, error: "bad id" });

    const responses = await callEachWithAgent();

    expect(responses.map((res) => res.status)).toEqual([400, 400, 400, 400, 400, 400, 400, 400]);
    for (const call of Object.values(engine)) {
      expect(call).not.toHaveBeenCalled();
    }
  });

  it("says why, in the caller's own words, rather than a bare status", async () => {
    resolveChatAgent.mockResolvedValue({
      ok: false,
      status: 403,
      error: "that agent is not one this account may chat with",
    });
    const { send } = await routes();

    const res = await send(post("http://helm.test/x", { message: "hi", agent: "scheduler" }));

    expect(await res.json()).toEqual({
      error: "that agent is not one this account may chat with",
    });
  });

  it("never consults the guard, or refuses, when no agent was named", async () => {
    // The default chat is every request the operator makes. It must not pay for
    // a bridge round-trip, and it must not be refusable by one.
    resolveChatAgent.mockResolvedValue({ ok: true, key: "" });
    const { send, history } = await routes();

    const sent = await send(post("http://helm.test/x", { message: "hi" }));
    const read = await history(new Request("http://helm.test/x"));

    expect(sent.status).toBe(200);
    expect(read.status).toBe(200);
    expect(engine.chatSend).toHaveBeenCalledWith("hi", "");
  });
});
