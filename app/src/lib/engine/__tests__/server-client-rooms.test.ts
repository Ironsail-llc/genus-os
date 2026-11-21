/**
 * The engine client and a room: the id travels as `room_id`, never as a
 * session key, and a busy room comes back as a typed refusal naming who is
 * being answered.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const bridgeAuthHeaders = vi.fn(async () => ({ Authorization: "Bearer caller-token" }));
vi.mock("@/lib/bridge-auth", () => ({ bridgeAuthHeaders: () => bridgeAuthHeaders() }));

const { EngineClient } = await import("@/lib/engine/server-client");
const { RoomBusy, EngineRefusal } = await import("@/lib/engine/engine-refusal");
const { chatSteer, chatInterrupt } = await import("@/lib/engine/steer-client");

const ROOM = "11111111-1111-4111-8111-111111111111";

function stub(response: Response) {
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

describe("EngineClient in a room", () => {
  it("sends room_id in the body and no session_key", async () => {
    const fetchMock = stub(new Response("event: done\ndata: {}\n\n", { status: 200 }));
    await new EngineClient().chatSend("hi", "", "r-1", false, undefined, ROOM);
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).toEqual({ message: "hi", room_id: ROOM, request_id: "r-1" });
  });

  it("sends the outbox entry's client id with a room message", async () => {
    const fetchMock = stub(new Response("event: done\ndata: {}\n\n", { status: 200 }));
    await new EngineClient().chatSend("hi", "", "r-1", false, undefined, ROOM, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      message: "hi", room_id: ROOM, request_id: "r-1", client_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    });
  });

  it("raises RoomBusy with the name being answered on a 409 room_busy", async () => {
    stub(Response.json({ error: "room_busy", answering: "Alice" }, { status: 409 }));
    const err = await new EngineClient().chatSend("hi", "", "r-1", false, undefined, ROOM).catch((e) => e);
    expect(err).toBeInstanceOf(RoomBusy);
    expect(err.answering).toBe("Alice");
  });

  it("raises a refusal carrying the status when the engine says the room is not there", async () => {
    stub(Response.json({ error: "room not found" }, { status: 404 }));
    const err = await new EngineClient().chatSend("hi", "", undefined, false, undefined, ROOM).catch((e) => e);
    expect(err).toBeInstanceOf(EngineRefusal);
    expect(err.status).toBe(404);
  });

  it("reads history with room_id", async () => {
    const fetchMock = stub(Response.json({ messages: [] }));
    await new EngineClient().chatHistory(20, "", { before: 9 }, ROOM);
    const url = new URL(fetchMock.mock.calls[0][0]);
    expect(url.searchParams.get("room_id")).toBe(ROOM);
    expect(url.searchParams.get("before")).toBe("9");
    expect(url.searchParams.has("session_key")).toBe(false);
  });

  it("aborts, recovers, steers and interrupts with room_id", async () => {
    let fetchMock = stub(Response.json({ ok: true, aborted: true }));
    await new EngineClient().chatAbort("", "r-1", ROOM);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ request_id: "r-1", room_id: ROOM });

    fetchMock = stub(Response.json({ terminal: true }));
    await new EngineClient().chatOutcome("r-1", "", ROOM);
    expect(new URL(fetchMock.mock.calls[0][0]).searchParams.get("room_id")).toBe(ROOM);

    fetchMock = stub(Response.json({ ok: true, found: true }));
    await chatSteer("", "focus", "r-1", ROOM);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ text: "focus", request_id: "r-1", room_id: ROOM });

    fetchMock = stub(Response.json({ ok: true, found: true }));
    await chatInterrupt("", "r-1", ROOM);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ request_id: "r-1", room_id: ROOM });
  });

  it("adds nothing to a request with no room", async () => {
    const fetchMock = stub(new Response("event: done\ndata: {}\n\n", { status: 200 }));
    await new EngineClient().chatSend("hi");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ message: "hi" });
  });
});
