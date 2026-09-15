import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * The BFF layer: what a browser may say about which agent it is talking to,
 * and what the app does with it.
 *
 * The browser sends an `agent` ID, never a session key. That is the seam: a
 * session key is a shape the engine parses (`agent:<id>:primary`), and a
 * browser that could spell one could spell `agent:main:user:someone-else` too.
 * `sessionKeyForAgent` is the only thing that turns one into the other, and it
 * refuses anything that is not a plain id.
 *
 * The other rule here is the canvas prompt. It teaches the MAIN agent about the
 * dashboard's render markers, and injecting it is a write into whichever
 * session the request names. Injecting it into another agent's session would
 * hand a worker a capability its manifest never granted and a prompt its
 * instructions never mention — so it stays main-only, which is also the reason
 * a request that names an agent must not trigger it.
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

vi.mock("@/lib/engine/server-client", () => ({
  getEngineClient: () => engine,
}));

vi.mock("@/lib/engine/session-state", () => ({
  ensureCanvasPromptInjected: () => ensureCanvasPromptInjected(),
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
  });

  afterEach(() => {
    vi.resetModules();
  });

  it("sends no session key when the body names no agent", async () => {
    const { POST } = await import("../send/route");

    await POST(post("http://helm.test/api/chat/send", { message: "hello" }));

    expect(engine.chatSend).toHaveBeenCalledWith("hello", "");
  });

  it("sends the agent's session key when the body names one", async () => {
    const { POST } = await import("../send/route");

    await POST(post("http://helm.test/api/chat/send", { message: "hi", agent: "scheduler" }));

    expect(engine.chatSend).toHaveBeenCalledWith("hi", KEY);
  });

  it("injects the canvas prompt for the main session and for nobody else", async () => {
    const { POST } = await import("../send/route");

    await POST(post("http://helm.test/api/chat/send", { message: "hello" }));
    expect(ensureCanvasPromptInjected).toHaveBeenCalledTimes(1);

    await POST(post("http://helm.test/api/chat/send", { message: "hi", agent: "scheduler" }));
    expect(ensureCanvasPromptInjected).toHaveBeenCalledTimes(1);
  });

  it("refuses an agent id that is not a plain id, falling back to the main session", async () => {
    const { POST } = await import("../send/route");

    await POST(
      post("http://helm.test/api/chat/send", { message: "hi", agent: "main:user:somebody" })
    );

    expect(engine.chatSend).toHaveBeenCalledWith("hi", "");
  });

  it("reads history from the named agent's session, and injects nothing for it", async () => {
    const { GET } = await import("../history/route");

    await GET(new Request("http://helm.test/api/chat/history?limit=20&agent=scheduler"));

    expect(engine.chatHistory).toHaveBeenCalledWith(20, KEY);
    expect(ensureCanvasPromptInjected).not.toHaveBeenCalled();
  });

  it("reads history from the main session when the query names no agent", async () => {
    const { GET } = await import("../history/route");

    await GET(new Request("http://helm.test/api/chat/history"));

    expect(engine.chatHistory).toHaveBeenCalledWith(50, "");
    expect(ensureCanvasPromptInjected).toHaveBeenCalledTimes(1);
  });

  it("keeps the whole plan round-trip on the same session as the plan it started", async () => {
    const start = (await import("../plan/start/route")).POST;
    const approve = (await import("../plan/approve/route")).POST;
    const reject = (await import("../plan/reject/route")).POST;
    const status = (await import("../plan/status/route")).GET;

    await start(post("http://helm.test/x", { message: "do it", agent: "scheduler" }));
    expect(engine.planStart).toHaveBeenCalledWith("do it", false, KEY);

    await approve(post("http://helm.test/x", { plan_id: "p-1", agent: "scheduler" }));
    expect(engine.planApprove).toHaveBeenCalledWith("p-1", KEY);

    await reject(post("http://helm.test/x", { plan_id: "p-1", agent: "scheduler" }));
    expect(engine.planReject).toHaveBeenCalledWith("p-1", undefined, KEY);

    await status(new Request("http://helm.test/x?agent=scheduler"));
    expect(engine.planStatus).toHaveBeenCalledWith(KEY);
  });

  it("leaves the plan round-trip on the main session when no agent is named", async () => {
    const start = (await import("../plan/start/route")).POST;
    const status = (await import("../plan/status/route")).GET;

    await start(post("http://helm.test/x", { message: "do it" }));
    expect(engine.planStart).toHaveBeenCalledWith("do it", false, "");

    await status(new Request("http://helm.test/x"));
    expect(engine.planStatus).toHaveBeenCalledWith("");
  });

  it("carries the agent through deep mode too", async () => {
    const start = (await import("../deep/start/route")).POST;
    const status = (await import("../deep/status/route")).GET;

    await start(post("http://helm.test/x", { query: "why", agent: "scheduler" }));
    expect(engine.deepStart).toHaveBeenCalledWith("why", KEY);

    await status(new Request("http://helm.test/x?agent=scheduler"));
    expect(engine.deepStatus).toHaveBeenCalledWith(KEY);
  });
});
