import { describe, it, expect, vi, beforeEach } from "vitest";

// Create mock methods
const mockXrevrange = vi.fn();
const mockXread = vi.fn();
const mockXlen = vi.fn();
const mockQuit = vi.fn();

// Mock ioredis — use a real class so `new Redis()` works
vi.mock("ioredis", () => {
  class MockRedis {
    xrevrange = mockXrevrange;
    xread = mockXread;
    xlen = mockXlen;
    quit = mockQuit;
  }
  return { default: MockRedis };
});

// Must import after mock setup
import {
  readRecent,
  readSince,
  streamLengths,
  isValidStream,
  streamKey,
  closeRedis,
} from "../redis-client";

beforeEach(async () => {
  vi.clearAllMocks();
  // Reset the module-level client so each test gets a fresh connection
  await closeRedis();
});

describe("streamKey", () => {
  it("prefixes stream name with robothor:events:", () => {
    expect(streamKey("email")).toBe("robothor:events:email");
    expect(streamKey("crm")).toBe("robothor:events:crm");
  });
});

describe("isValidStream", () => {
  it("accepts valid stream names", () => {
    expect(isValidStream("email")).toBe(true);
    expect(isValidStream("crm")).toBe(true);
    expect(isValidStream("health")).toBe(true);
    expect(isValidStream("vision")).toBe(true);
    expect(isValidStream("calendar")).toBe(true);
    expect(isValidStream("agent")).toBe(true);
    expect(isValidStream("system")).toBe(true);
  });

  it("rejects invalid stream names", () => {
    expect(isValidStream("invalid")).toBe(false);
    expect(isValidStream("")).toBe(false);
    expect(isValidStream("Email")).toBe(false);
  });
});

describe("readRecent", () => {
  it("parses stream entries into EventEnvelope", async () => {
    mockXrevrange.mockResolvedValue([
      [
        "1-0",
        [
          "timestamp", "2026-01-01T00:00:00Z",
          "type", "email.new",
          "source", "email_sync",
          "actor", "robothor",
          "payload", '{"subject":"Hello"}',
          "correlation_id", "",
        ],
      ],
    ]);

    const events = await readRecent("email", 5);
    expect(events).toHaveLength(1);
    expect(events[0].id).toBe("1-0");
    expect(events[0].type).toBe("email.new");
    expect(events[0].source).toBe("email_sync");
    expect(events[0].payload).toEqual({ subject: "Hello" });
  });

  it("handles empty stream", async () => {
    mockXrevrange.mockResolvedValue([]);
    const events = await readRecent("email");
    expect(events).toHaveLength(0);
  });

  it("handles malformed JSON payload gracefully", async () => {
    mockXrevrange.mockResolvedValue([
      ["1-0", [
        "timestamp", "t", "type", "t", "source", "s",
        "actor", "a", "payload", "not-json", "correlation_id", "",
      ]],
    ]);
    const events = await readRecent("email");
    expect(events).toHaveLength(1);
    expect(events[0].payload).toEqual({});
  });
});

describe("readSince", () => {
  it("returns events from multiple streams", async () => {
    mockXread.mockResolvedValue([
      [
        "robothor:events:email",
        [["2-0", [
          "timestamp", "t", "type", "email.new", "source", "s",
          "actor", "a", "payload", "{}", "correlation_id", "",
        ]]],
      ],
      [
        "robothor:events:crm",
        [["3-0", [
          "timestamp", "t", "type", "crm.create", "source", "s",
          "actor", "a", "payload", "{}", "correlation_id", "",
        ]]],
      ],
    ]);

    const results = await readSince({ email: "$", crm: "$" });
    expect(results).toHaveLength(2);
    expect(results[0].stream).toBe("email");
    expect(results[1].stream).toBe("crm");
  });

  it("returns empty array when no new events", async () => {
    mockXread.mockResolvedValue(null);
    const results = await readSince({ email: "$" });
    expect(results).toHaveLength(0);
  });
});

describe("streamLengths", () => {
  it("returns lengths for all streams", async () => {
    mockXlen.mockResolvedValue(42);
    const lengths = await streamLengths();
    expect(lengths.email).toBe(42);
    expect(lengths.crm).toBe(42);
  });

  it("returns 0 on error", async () => {
    mockXlen.mockRejectedValue(new Error("fail"));
    const lengths = await streamLengths();
    expect(lengths.email).toBe(0);
  });
});
