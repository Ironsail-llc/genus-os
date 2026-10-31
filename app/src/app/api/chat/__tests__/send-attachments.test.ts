import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * `/api/chat/send` with files attached. The route checks the same limits the
 * composer states (3 files, 5 MB each, a short list of types, real base64) and
 * passes what it accepts straight through to the engine, which checks them
 * again: the browser is not the boundary.
 */

const engine = { chatSend: vi.fn() };

vi.mock("@/lib/engine/server-client", () => ({ getEngineClient: () => engine }));
vi.mock("@/lib/engine/session-state", () => ({ ensureCanvasPromptInjected: () => Promise.resolve() }));
vi.mock("@/lib/chat/agent-guard", () => ({
  resolveChatAgent: async () => ({ ok: true, key: "" }),
}));

const REQUEST = "00000000-0000-4000-8000-000000000001";

function emptyStream() {
  return new ReadableStream({ start(c) { c.close(); } });
}

function file(name: string, mime: string, bytes: number | string = "hello") {
  const body = typeof bytes === "number" ? "a".repeat(bytes) : bytes;
  return { name, mime, size: body.length, data: Buffer.from(body).toString("base64") };
}

async function send(body: Record<string, unknown>) {
  const { POST } = await import("../send/route");
  return POST(new Request("http://helm.test/api/chat/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));
}

describe("/api/chat/send with attachments", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    engine.chatSend.mockResolvedValue({ body: emptyStream() });
  });

  it("passes accepted files through to the engine", async () => {
    const files = [file("notes.md", "text/markdown"), file("shot.png", "image/png")];
    const res = await send({ message: "read", request_id: REQUEST, attachments: files });
    expect(res.status).toBe(200);
    expect(engine.chatSend).toHaveBeenCalledWith("read", "", REQUEST, false, files);
  });

  it("allows files with no message", async () => {
    const files = [file("a.txt", "text/plain")];
    const res = await send({ message: "", request_id: REQUEST, attachments: files });
    expect(res.status).toBe(200);
    expect(engine.chatSend).toHaveBeenCalledWith("", "", REQUEST, false, files);
  });

  it("still refuses an empty message with no files", async () => {
    const res = await send({ message: "", attachments: [] });
    expect(res.status).toBe(400);
    expect(engine.chatSend).not.toHaveBeenCalled();
  });

  it("413s a file over 5 MB", async () => {
    const res = await send({ message: "x", attachments: [file("big.txt", "text/plain", 5 * 1024 * 1024 + 1)] });
    expect(res.status).toBe(413);
    expect((await res.json()).error).toMatch(/5 MB/);
    expect(engine.chatSend).not.toHaveBeenCalled();
  });

  it("413s more than 3 files", async () => {
    const files = [1, 2, 3, 4].map((i) => file(`${i}.txt`, "text/plain"));
    const res = await send({ message: "x", attachments: files });
    expect(res.status).toBe(413);
    expect(engine.chatSend).not.toHaveBeenCalled();
  });

  it.each([
    ["an unsupported type", file("a.zip", "application/zip")],
    ["data that is not base64", { name: "a.txt", mime: "text/plain", size: 3, data: "not base64!!" }],
    ["a size that does not match the data", { name: "a.txt", mime: "text/plain", size: 99, data: "aGk=" }],
    ["a missing name", { mime: "text/plain", size: 2, data: "aGk=" }],
    ["something that is not a file", "a string"],
  ])("422s %s", async (_label, item) => {
    const res = await send({ message: "x", attachments: [item] });
    expect(res.status).toBe(422);
    expect(engine.chatSend).not.toHaveBeenCalled();
  });

  it.each([
    ["tool.exe", "text/plain"],
    ["evil.png", "text/plain"],
    ["notes.txt", "image/png"],
    ["report.pdf", "text/plain"],
  ])("422s %s sent as %s: the extension is part of the type", async (name, mime) => {
    const res = await send({ message: "x", attachments: [file(name, mime)] });
    expect(res.status).toBe(422);
    expect((await res.json()).error).toBe(`${name} is not a supported type`);
    expect(engine.chatSend).not.toHaveBeenCalled();
  });

  it.each([
    ["notes.txt", "text/plain"],
    ["photo.jpg", "image/jpeg"],
    ["Photo.JPEG", "image/jpeg"],
    ["notes.md", "text/plain"],
    ["README", "text/plain"],
  ])("passes %s sent as %s", async (name, mime) => {
    const res = await send({ message: "x", request_id: REQUEST, attachments: [file(name, mime)] });
    expect(res.status).toBe(200);
    expect(engine.chatSend).toHaveBeenCalled();
  });

  it("422s attachments that are not a list", async () => {
    const res = await send({ message: "x", attachments: { name: "a.txt" } });
    expect(res.status).toBe(422);
    expect(engine.chatSend).not.toHaveBeenCalled();
  });

  it("passes an engine refusal through as the engine's own status and words", async () => {
    const { EngineRefusal } = await import("@/lib/engine/engine-refusal");
    engine.chatSend.mockRejectedValue(new EngineRefusal(422, "shot.png is not the picture it claims to be"));
    const res = await send({ message: "x", request_id: REQUEST, attachments: [file("shot.png", "image/png")] });
    expect(res.status).toBe(422);
    expect(await res.json()).toEqual({
      error: "shot.png is not the picture it claims to be",
      request_admitted: false,
    });
  });

  it("answers 502 with the engine's words when the engine failed before admitting the turn", async () => {
    const { EngineRefusal } = await import("@/lib/engine/engine-refusal");
    engine.chatSend.mockRejectedValue(new EngineRefusal(500, "a.txt could not be kept; try sending it again"));
    const res = await send({ message: "x", request_id: REQUEST, attachments: [file("a.txt", "text/plain")] });
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({
      error: "a.txt could not be kept; try sending it again",
      request_admitted: false,
    });
  });

  it("422s files sent to join a running turn", async () => {
    const res = await send({ message: "x", join_running: true, attachments: [file("a.txt", "text/plain")] });
    expect(res.status).toBe(422);
    expect((await res.json()).request_admitted).toBe(false);
    expect(engine.chatSend).not.toHaveBeenCalled();
  });

  it("413s a body too large to hold three files before reading it", async () => {
    const { POST } = await import("../send/route");
    const json = vi.fn();
    const req = { headers: new Headers({ "content-length": String(30 * 1024 * 1024) }), json } as unknown as Request;
    const res = await POST(req);
    expect(res.status).toBe(413);
    expect(json).not.toHaveBeenCalled();
    expect(engine.chatSend).not.toHaveBeenCalled();
  });

  it("answers 400 with request_admitted false for a body that is not JSON", async () => {
    const { POST } = await import("../send/route");
    const res = await POST(new Request("http://helm.test/api/chat/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{not json",
    }));
    expect(res.status).toBe(400);
    expect(await res.json()).toMatchObject({ request_admitted: false });
    expect(engine.chatSend).not.toHaveBeenCalled();
  });

  it("answers 413 with request_admitted false for a body that arrived shorter than it said", async () => {
    // What a proxy that buffers only part of the body hands on: fewer bytes
    // than the Content-Length the browser sent, and JSON cut off part way.
    const { POST } = await import("../send/route");
    const cut = '{"message": "x", "attachments": [{"name": "a.png", "data": "iVBOR';
    const req = {
      headers: new Headers({ "content-length": String(cut.length + 4_000_000) }),
      text: async () => cut,
      json: async () => JSON.parse(cut),
    } as unknown as Request;
    const res = await POST(req);
    expect(res.status).toBe(413);
    expect(await res.json()).toMatchObject({ request_admitted: false });
    expect(engine.chatSend).not.toHaveBeenCalled();
  });

  it("leaves a plain send's engine call exactly as it was", async () => {
    await send({ message: "hi", request_id: REQUEST });
    expect(engine.chatSend).toHaveBeenCalledWith("hi", "", REQUEST);
  });
});
