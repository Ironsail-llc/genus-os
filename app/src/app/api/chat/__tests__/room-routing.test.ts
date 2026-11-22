import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * The chat routes and a room.
 *
 * The browser names a ROOM by id; it never sends, fetches or builds a session
 * key. The routes check only that the id has a room id's shape and forward it
 * to the engine as `room_id` with the caller's own token, and the engine
 * resolves the key and the caller's access. With no room named, every route
 * behaves exactly as before (the agent-routing suite is the regression for
 * that path; a few of its assertions are repeated here next to their room
 * counterparts).
 */

const engine = {
  chatSend: vi.fn(),
  chatAbort: vi.fn(),
  chatOutcome: vi.fn(),
  chatHistory: vi.fn(),
};
const chatSteer = vi.fn();
const chatInterrupt = vi.fn();
const ensureCanvasPromptInjected = vi.fn().mockResolvedValue(undefined);
const resolveChatAgent = vi.fn();

vi.mock("@/lib/engine/server-client", () => ({ getEngineClient: () => engine }));
vi.mock("@/lib/engine/steer-client", () => ({
  chatSteer: (...args: unknown[]) => chatSteer(...args),
  chatInterrupt: (...args: unknown[]) => chatInterrupt(...args),
}));
vi.mock("@/lib/engine/session-state", () => ({
  ensureCanvasPromptInjected: () => ensureCanvasPromptInjected(),
}));
vi.mock("@/lib/chat/agent-guard", () => ({
  resolveChatAgent: (agent: unknown) => resolveChatAgent(agent),
}));

const ROOM = "11111111-1111-4111-8111-111111111111";

function emptyStream() {
  return new ReadableStream({ start: (c) => c.close() });
}

/** A room turn's stream as an engine that honours `room_id` starts it: a `room` event naming the room. */
function roomStream(roomId = ROOM) {
  return new ReadableStream({
    start(c) {
      c.enqueue(new TextEncoder().encode(`event: room\ndata: ${JSON.stringify({ roomId })}\n\nevent: done\ndata: {"text":"ok"}\n\n`));
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

describe("the chat routes in a room", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // An engine that honours `room_id` echoes it back on every room answer.
    engine.chatSend.mockImplementation(async (...args: unknown[]) => ({ body: args[5] ? roomStream() : emptyStream() }));
    engine.chatAbort.mockImplementation(async (...args: unknown[]) => ({ ok: true, durable_stopped: true, ...(args[2] ? { roomId: ROOM } : {}) }));
    engine.chatOutcome.mockImplementation(async (...args: unknown[]) => ({ terminal: true, text: "done", ...(args[2] ? { roomId: ROOM } : {}) }));
    engine.chatHistory.mockImplementation(async (...args: unknown[]) => ({
      messages: [],
      sessionKey: "agent:main:room:x",
      ...(args[3] ? { roomId: ROOM } : {}),
    }));
    chatSteer.mockImplementation(async (...args: unknown[]) => ({ ok: true, found: true, ...(args[3] ? { roomId: ROOM } : {}) }));
    chatInterrupt.mockImplementation(async (...args: unknown[]) => ({ ok: true, found: true, ...(args[2] ? { roomId: ROOM } : {}) }));
    resolveChatAgent.mockImplementation(async (agent: unknown) =>
      agent ? { ok: true, key: `agent:${String(agent)}:primary` } : { ok: true, key: "" },
    );
  });

  afterEach(() => {
    vi.resetModules();
  });

  it("forwards the room id to the engine and no session key", async () => {
    const { send } = await routes();
    const res = await send(post({ message: "how did we do?", room: ROOM, request_id: "r-1" }));
    expect(res.status).toBe(200);
    expect(engine.chatSend).toHaveBeenCalledWith("how did we do?", "", "r-1", false, undefined, ROOM);
    expect(resolveChatAgent).not.toHaveBeenCalled();
  });

  it("forwards the outbox entry's client id with a room message, and only a well-formed one", async () => {
    const { send } = await routes();
    const CLIENT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
    await send(post({ message: "hi", room: ROOM, request_id: "r-1", client_id: CLIENT }));
    expect(engine.chatSend).toHaveBeenLastCalledWith("hi", "", "r-1", false, undefined, ROOM, CLIENT);
    const res = await send(post({ message: "hi", room: ROOM, client_id: "agent:main:primary" }));
    expect(res.status).toBe(400);
  });

  it("never injects the canvas prompt into a room's session", async () => {
    const { send, history } = await routes();
    await send(post({ message: "hi", room: ROOM }));
    await history(new Request(`http://helm.test/x?room=${ROOM}`));
    expect(ensureCanvasPromptInjected).not.toHaveBeenCalled();
  });

  it("refuses a room value that is not a room id, before the engine is asked", async () => {
    const { send, history } = await routes();
    for (const bad of ["agent:main:room:x", "main", 7, { id: ROOM }]) {
      const res = await send(post({ message: "hi", room: bad }));
      expect(res.status).toBe(400);
      expect((await res.json()).request_admitted).toBe(false);
    }
    const res = await history(new Request("http://helm.test/x?room=agent%3Amain%3Aprimary"));
    expect(res.status).toBe(400);
    expect(engine.chatSend).not.toHaveBeenCalled();
    expect(engine.chatHistory).not.toHaveBeenCalled();
  });

  it("refuses a request that names both a room and an agent", async () => {
    const { send } = await routes();
    const res = await send(post({ message: "hi", room: ROOM, agent: "scheduler" }));
    expect(res.status).toBe(400);
    expect(engine.chatSend).not.toHaveBeenCalled();
  });

  it("answers 409 room_busy with who is being answered, and admits nothing", async () => {
    const { RoomBusy } = await import("@/lib/engine/engine-refusal");
    engine.chatSend.mockRejectedValueOnce(new RoomBusy("Alice"));
    const { send } = await routes();
    const res = await send(post({ message: "me too", room: ROOM }));
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "room_busy", answering: "Alice", request_admitted: false });
  });

  it("passes the engine's refusal of a room through (no access is a 404)", async () => {
    const { EngineRefusal } = await import("@/lib/engine/engine-refusal");
    engine.chatSend.mockRejectedValueOnce(new EngineRefusal(404, "room not found"));
    const { send } = await routes();
    const res = await send(post({ message: "hi", room: ROOM }));
    expect(res.status).toBe(404);
    expect((await res.json()).request_admitted).toBe(false);
  });

  it("reads a room's history, paging included, and never hands the browser a session key", async () => {
    const { history } = await routes();
    const res = await history(new Request(`http://helm.test/x?room=${ROOM}&limit=20`));
    expect(res.status).toBe(200);
    expect(await res.json()).not.toHaveProperty("sessionKey");
    expect(engine.chatHistory).toHaveBeenLastCalledWith(20, "", {}, ROOM);
    await history(new Request(`http://helm.test/x?room=${ROOM}&before=40`));
    expect(engine.chatHistory).toHaveBeenLastCalledWith(50, "", { before: 40 }, ROOM);
  });

  it("stops, steers, interrupts and recovers a room's turn by room id", async () => {
    const r = await routes();
    await r.abort(post({ room: ROOM, request_id: "r-1" }));
    expect(engine.chatAbort).toHaveBeenCalledWith("", "r-1", ROOM);
    await r.steer(post({ room: ROOM, request_id: "r-1", text: "focus on Q4" }));
    expect(chatSteer).toHaveBeenCalledWith("", "focus on Q4", "r-1", ROOM);
    await r.interrupt(post({ room: ROOM, request_id: "r-1" }));
    expect(chatInterrupt).toHaveBeenCalledWith("", "r-1", ROOM);
    await r.outcome(new Request(`http://helm.test/x?room=${ROOM}&request_id=r-1`));
    expect(engine.chatOutcome).toHaveBeenCalledWith("r-1", "", ROOM);
  });

  describe("with no room named, exactly as before", () => {
    it("send, history, abort, steer, interrupt and outcome keep their old engine calls", async () => {
      const r = await routes();
      await r.send(post({ message: "hello" }));
      expect(engine.chatSend).toHaveBeenLastCalledWith("hello", "");
      await r.send(post({ message: "hi", agent: "scheduler", request_id: "r-2" }));
      expect(engine.chatSend).toHaveBeenLastCalledWith("hi", "agent:scheduler:primary", "r-2");
      await r.history(new Request("http://helm.test/x"));
      expect(engine.chatHistory).toHaveBeenLastCalledWith(50, "");
      await r.abort(post({ request_id: "r-1" }));
      expect(engine.chatAbort).toHaveBeenLastCalledWith("", "r-1");
      await r.steer(post({ request_id: "r-1", text: "x" }));
      expect(chatSteer).toHaveBeenLastCalledWith("", "x", "r-1");
      await r.interrupt(post({ request_id: "r-1" }));
      expect(chatInterrupt).toHaveBeenLastCalledWith("", "r-1");
      await r.outcome(new Request("http://helm.test/x?request_id=r-1"));
      expect(engine.chatOutcome).toHaveBeenLastCalledWith("r-1", "");
      expect(ensureCanvasPromptInjected).toHaveBeenCalled();
    });

    it("treats an empty room value as no room", async () => {
      const { send } = await routes();
      await send(post({ message: "hello", room: "" }));
      expect(engine.chatSend).toHaveBeenLastCalledWith("hello", "");
    });
  });
});
