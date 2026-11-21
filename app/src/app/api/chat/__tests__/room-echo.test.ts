import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Failing closed when the engine does not honour `room_id`.
 *
 * A room request carries no session key, and an engine that ignores `room_id`
 * reads "no key" as the caller's MAIN conversation. So the engine echoes the
 * room (`roomId`) on every room answer, and a route that does not get that
 * echo — or gets another room's — refuses with a 502 and hands nothing on.
 * Before a send, the route confirms the echo for this room with a history
 * read (cached briefly per caller), so a message is never posted into the
 * wrong conversation in the first place.
 */

const engine = { chatSend: vi.fn(), chatAbort: vi.fn(), chatOutcome: vi.fn(), chatHistory: vi.fn() };
const chatSteer = vi.fn();
const chatInterrupt = vi.fn();
vi.mock("@/lib/engine/server-client", () => ({ getEngineClient: () => engine }));
vi.mock("@/lib/engine/steer-client", () => ({
  chatSteer: (...args: unknown[]) => chatSteer(...args),
  chatInterrupt: (...args: unknown[]) => chatInterrupt(...args),
}));
vi.mock("@/lib/engine/session-state", () => ({ ensureCanvasPromptInjected: vi.fn(async () => {}) }));
vi.mock("@/lib/chat/agent-guard", () => ({ resolveChatAgent: vi.fn(async () => ({ ok: true, key: "" })) }));
vi.mock("@/lib/bridge-auth", () => ({ bridgeAuthHeaders: vi.fn(async () => ({ Authorization: "Bearer caller-token" })) }));

const ROOM = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";

function sse(text: string) {
  return new ReadableStream({
    start(c) {
      c.enqueue(new TextEncoder().encode(text));
      c.close();
    },
  });
}

function post(body: unknown) {
  return new Request("http://helm.test/x", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function routes() {
  return {
    send: (await import("../send/route")).POST,
    history: (await import("../history/route")).GET,
    abort: (await import("../abort/route")).POST,
    outcome: (await import("../outcome/route")).GET,
    steer: (await import("../steer/route")).POST,
    interrupt: (await import("../interrupt/route")).POST,
  };
}

/** What an engine that ignores `room_id` answers: the caller's main conversation. */
const MAIN_HISTORY = { messages: [{ id: 1, role: "user", content: "my private main chat" }], sessionKey: "agent:main:primary" };

beforeEach(() => {
  vi.clearAllMocks();
  engine.chatHistory.mockResolvedValue({ messages: [], roomId: ROOM });
  engine.chatSend.mockResolvedValue({ body: sse(`event: room\ndata: {"roomId":"${ROOM}"}\n\nevent: done\ndata: {"text":"ok"}\n\n`) });
});

afterEach(() => vi.resetModules());

describe("an engine that ignores room_id", () => {
  it("history: refuses, and shows none of what it answered", async () => {
    engine.chatHistory.mockResolvedValue(MAIN_HISTORY);
    const { history } = await routes();
    const res = await history(new Request(`http://helm.test/x?room=${ROOM}`));
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(JSON.stringify(body)).not.toContain("my private main chat");
    expect(body.messages ?? []).toEqual([]);
    expect(body).not.toHaveProperty("sessionKey");
  });

  it("history: refuses an echo naming another room", async () => {
    engine.chatHistory.mockResolvedValue({ messages: [], roomId: OTHER });
    const { history } = await routes();
    expect((await history(new Request(`http://helm.test/x?room=${ROOM}`))).status).toBe(502);
  });

  it("send: confirms the room before sending, and sends nothing unconfirmed", async () => {
    engine.chatHistory.mockResolvedValue(MAIN_HISTORY);
    const { send } = await routes();
    const res = await send(post({ message: "for the room only", room: ROOM, request_id: "r-1" }));
    expect(res.status).toBe(502);
    expect(await res.json()).toMatchObject({ request_admitted: false });
    expect(engine.chatSend).not.toHaveBeenCalled();
  });

  it("send: a join_running follow-up is confirmed the same way", async () => {
    engine.chatHistory.mockResolvedValue(MAIN_HISTORY);
    const { send } = await routes();
    const res = await send(post({ message: "also", room: ROOM, join_running: true }));
    expect(res.status).toBe(502);
    expect(engine.chatSend).not.toHaveBeenCalled();
  });

  it("send: refuses a stream that does not open with the room, and forwards none of it", async () => {
    engine.chatSend.mockResolvedValue({ body: sse(`event: delta\ndata: {"text":"main says hi"}\n\nevent: done\ndata: {"text":"main says hi"}\n\n`) });
    const { send } = await routes();
    const res = await send(post({ message: "hi", room: ROOM }));
    expect(res.status).toBe(502);
    const text = await res.text();
    expect(text).not.toContain("main says hi");
    expect(JSON.parse(text)).toMatchObject({ request_admitted: false });
  });

  it("send: refuses a stream that names another room", async () => {
    engine.chatSend.mockResolvedValue({ body: sse(`event: room\ndata: {"roomId":"${OTHER}"}\n\nevent: done\ndata: {}\n\n`) });
    const { send } = await routes();
    expect((await send(post({ message: "hi", room: ROOM }))).status).toBe(502);
  });

  it("steer: confirms the room before posting the text", async () => {
    engine.chatHistory.mockResolvedValue(MAIN_HISTORY);
    const { steer } = await routes();
    const res = await steer(post({ room: ROOM, request_id: "r-1", text: "focus" }));
    expect(res.status).toBe(502);
    expect(chatSteer).not.toHaveBeenCalled();
  });

  it("abort, steer, interrupt and outcome refuse an answer with no echo", async () => {
    engine.chatAbort.mockResolvedValue({ ok: true, durable_stopped: true });
    engine.chatOutcome.mockResolvedValue({ terminal: true, text: "main text" });
    chatSteer.mockResolvedValue({ ok: true, found: true });
    chatInterrupt.mockResolvedValue({ ok: true, found: true });
    const r = await routes();
    expect((await r.abort(post({ room: ROOM, request_id: "r-1" }))).status).toBe(502);
    expect((await r.steer(post({ room: ROOM, request_id: "r-1", text: "x" }))).status).toBe(502);
    expect((await r.interrupt(post({ room: ROOM, request_id: "r-1" }))).status).toBe(502);
    const outcome = await r.outcome(new Request(`http://helm.test/x?room=${ROOM}&request_id=r-1`));
    expect(outcome.status).toBe(502);
    expect(await outcome.text()).not.toContain("main text");
  });
});

describe("an engine that honours room_id", () => {
  it("send: accepts a leading room event, and forwards the whole stream", async () => {
    const { send } = await routes();
    const res = await send(post({ message: "hi", room: ROOM }));
    expect(res.status).toBe(200);
    expect(await res.text()).toContain('"text":"ok"');
  });

  it("send: accepts the echo as a field of the first event's data", async () => {
    engine.chatSend.mockResolvedValue({ body: sse(`event: accepted\ndata: {"roomId":"${ROOM}","request_id":"r"}\n\nevent: done\ndata: {"text":"ok"}\n\n`) });
    const { send } = await routes();
    expect((await send(post({ message: "hi", room: ROOM }))).status).toBe(200);
  });

  it("confirms a room once per caller for a while, not on every send", async () => {
    const { send } = await routes();
    await send(post({ message: "one", room: ROOM }));
    engine.chatSend.mockResolvedValue({ body: sse(`event: room\ndata: {"roomId":"${ROOM}"}\n\n`) });
    await send(post({ message: "two", room: ROOM }));
    expect(engine.chatHistory).toHaveBeenCalledTimes(1);
    expect(engine.chatSend).toHaveBeenCalledTimes(2);
  });

  it("leaves the main conversation alone: no preflight, no echo needed", async () => {
    engine.chatSend.mockResolvedValue({ body: sse(`event: done\ndata: {"text":"hi"}\n\n`) });
    engine.chatHistory.mockResolvedValue({ messages: [], sessionKey: "agent:main:primary" });
    const { send, history } = await routes();
    expect((await send(post({ message: "hi" }))).status).toBe(200);
    expect(engine.chatHistory).not.toHaveBeenCalled();
    const res = await history(new Request("http://helm.test/x"));
    expect(res.status).toBe(200);
    expect((await res.json()).sessionKey).toBe("agent:main:primary");
  });
});
