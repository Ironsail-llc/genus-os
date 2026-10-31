import { describe, it, expect, vi, beforeEach } from "vitest";
import { NextRequest } from "next/server";

// Mock redis-client — factory must not reference outer variables (hoisting)
vi.mock("@/lib/event-bus/redis-client", async () => {
  const actual = await vi.importActual<typeof import("@/lib/event-bus/redis-client")>(
    "@/lib/event-bus/redis-client"
  );
  return {
    isValidStream: actual.isValidStream,
    readRecent: vi.fn(),
    readSince: vi.fn(),
    openStreamReader: vi.fn(),
  };
});

import { GET } from "@/app/api/events/stream/route";
import * as redis from "@/lib/event-bus/redis-client";

type Reader = {
  readSince: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
};

const mocked = redis as unknown as {
  readRecent: ReturnType<typeof vi.fn>;
  readSince: ReturnType<typeof vi.fn>;
  openStreamReader: ReturnType<typeof vi.fn>;
};

function envelope(id: string) {
  return {
    id, timestamp: "t", type: "task.created", source: "s", actor: "a",
    payload: {}, correlation_id: "",
  };
}

let readers: Reader[];

/** A reader whose reads wait a moment and return nothing, like an idle XREAD BLOCK. */
function idleReader(): Reader {
  const reader: Reader = {
    readSince: vi.fn(
      () => new Promise((resolve) => setTimeout(() => resolve([]), 5))
    ),
    close: vi.fn(async () => {}),
  };
  readers.push(reader);
  return reader;
}

function request(streams: string): NextRequest {
  return new NextRequest(`http://localhost/api/events/stream?streams=${streams}`);
}

/** Read the SSE body until `marker` has been seen. */
async function readUntil(res: Response, marker: string) {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let text = "";
  while (!text.includes(marker)) {
    const { value, done } = await reader.read();
    if (done) break;
    text += decoder.decode(value);
  }
  return { reader, text };
}

const tick = (ms = 20) => new Promise((r) => setTimeout(r, ms));

beforeEach(() => {
  vi.clearAllMocks();
  readers = [];
  mocked.openStreamReader.mockImplementation(() => idleReader());
  mocked.readSince.mockResolvedValue([]);
});

describe("GET /api/events/stream", () => {
  it("starts reading from the newest replayed id, not $, so nothing between reads is lost", async () => {
    // XREVRANGE returns newest first.
    mocked.readRecent.mockResolvedValue([envelope("5-0"), envelope("4-0")]);
    const res = await GET(request("agent"));
    const { reader } = await readUntil(res, "event: sync");
    await tick();
    expect(readers[0].readSince.mock.calls[0][0]).toEqual({ agent: "5-0" });
    await reader.cancel();
  });

  it("starts an empty stream from 0-0, so its first event is delivered", async () => {
    mocked.readRecent.mockResolvedValue([]);
    const res = await GET(request("agent"));
    const { reader } = await readUntil(res, "event: sync");
    await tick();
    expect(readers[0].readSince.mock.calls[0][0]).toEqual({ agent: "0-0" });
    await reader.cancel();
  });

  it("keeps the replay and the wire format unchanged", async () => {
    mocked.readRecent.mockResolvedValue([envelope("5-0"), envelope("4-0")]);
    const res = await GET(request("agent"));
    const { reader, text } = await readUntil(res, "event: sync");
    expect(res.headers.get("Content-Type")).toBe("text/event-stream");
    const frames = text.split("\n\n").filter(Boolean);
    expect(frames[0]).toBe(
      `event: message\ndata: ${JSON.stringify({ stream: "agent", ...envelope("4-0") })}`
    );
    expect(frames[1]).toContain('"id":"5-0"');
    expect(frames[2]).toBe('event: sync\ndata: {"status":"synced"}');
    await reader.cancel();
  });

  it("gives each connection its own blocking reader and never blocks on the shared client", async () => {
    mocked.readRecent.mockResolvedValue([]);
    const a = await readUntil(await GET(request("agent")), "event: sync");
    const b = await readUntil(await GET(request("agent")), "event: sync");
    await tick();
    expect(mocked.openStreamReader).toHaveBeenCalledTimes(2);
    expect(readers[0].readSince).toHaveBeenCalled();
    expect(readers[1].readSince).toHaveBeenCalled();
    expect(mocked.readSince).not.toHaveBeenCalled();
    await a.reader.cancel();
    await b.reader.cancel();
  });

  it("closes the connection's reader and stops reading when the client goes away", async () => {
    mocked.readRecent.mockResolvedValue([]);
    const { reader } = await readUntil(await GET(request("agent")), "event: sync");
    await tick();
    await reader.cancel();
    await tick();
    expect(readers[0].close).toHaveBeenCalledTimes(1);
    const calls = readers[0].readSince.mock.calls.length;
    await tick(50);
    expect(readers[0].readSince.mock.calls.length).toBe(calls);
  });

  it("advances to the id of each delivered event", async () => {
    mocked.readRecent.mockResolvedValue([envelope("5-0")]);
    mocked.openStreamReader.mockImplementation(() => {
      const r = idleReader();
      r.readSince.mockResolvedValueOnce([{ stream: "agent", events: [envelope("6-0")] }]);
      return r;
    });
    const res = await GET(request("agent"));
    const { reader, text } = await readUntil(res, '"id":"6-0"');
    await tick();
    expect(text).toContain('"id":"6-0"');
    expect(readers[0].readSince.mock.calls[1][0]).toEqual({ agent: "6-0" });
    await reader.cancel();
  });
});
