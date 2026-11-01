import { describe, it, expect, vi, beforeEach } from "vitest";

// Create mock methods
const mockXrevrange = vi.fn();
const mockXread = vi.fn();
const mockXlen = vi.fn();
const mockXadd = vi.fn();
const mockQuit = vi.fn();
const mockConstructor = vi.fn();

// Mock ioredis — use a real class so `new Redis()` works
vi.mock("ioredis", () => {
  class MockRedis {
    xrevrange = mockXrevrange;
    xread = mockXread;
    xlen = mockXlen;
    xadd = mockXadd;
    quit = mockQuit;
    constructor(...args: unknown[]) {
      mockConstructor(...args);
    }
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
  resolveRedisConnection,
  writeVital,
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

describe("resolveRedisConnection", () => {
  it("falls back to the localhost default when nothing is set", () => {
    expect(resolveRedisConnection({})).toEqual({
      url: "redis://localhost:6379/0",
    });
  });

  it("builds host, port, password and DB 0 from ROBOTHOR_REDIS_*", () => {
    expect(
      resolveRedisConnection({
        ROBOTHOR_REDIS_HOST: "cache.example.com",
        ROBOTHOR_REDIS_PORT: "6380",
        ROBOTHOR_REDIS_PASSWORD: "p@ss:w/rd",
      })
    ).toEqual({
      host: "cache.example.com",
      port: 6380,
      password: "p@ss:w/rd",
      db: 0,
    });
  });

  it("defaults the port to 6379 and omits an empty password", () => {
    expect(
      resolveRedisConnection({
        ROBOTHOR_REDIS_HOST: "cache.example.com",
        ROBOTHOR_REDIS_PASSWORD: "",
      })
    ).toEqual({ host: "cache.example.com", port: 6379, db: 0 });
  });

  it("selects the DB from ROBOTHOR_REDIS_DB, as the Python services do", () => {
    expect(
      resolveRedisConnection({
        ROBOTHOR_REDIS_HOST: "cache.example.com",
        ROBOTHOR_REDIS_DB: "3",
      })
    ).toEqual({ host: "cache.example.com", port: 6379, db: 3 });
  });

  it("applies ROBOTHOR_REDIS_DB to the localhost default too", () => {
    expect(resolveRedisConnection({ ROBOTHOR_REDIS_DB: "2" })).toEqual({
      url: "redis://localhost:6379/2",
    });
  });

  it("falls back to DB 0 when ROBOTHOR_REDIS_DB is not an integer", () => {
    expect(
      resolveRedisConnection({
        ROBOTHOR_REDIS_HOST: "cache.example.com",
        ROBOTHOR_REDIS_DB: "two",
      })
    ).toEqual({ host: "cache.example.com", port: 6379, db: 0 });
  });

  it("lets REDIS_URL win over ROBOTHOR_REDIS_*", () => {
    expect(
      resolveRedisConnection({
        REDIS_URL: "redis://override.example.com:6390/2",
        ROBOTHOR_REDIS_HOST: "cache.example.com",
        ROBOTHOR_REDIS_PORT: "6380",
        ROBOTHOR_REDIS_PASSWORD: "secret",
      })
    ).toEqual({ url: "redis://override.example.com:6390/2" });
  });
});

describe("client construction", () => {
  it("connects with the ROBOTHOR_REDIS_* settings when REDIS_URL is unset", async () => {
    vi.stubEnv("REDIS_URL", "");
    vi.stubEnv("ROBOTHOR_REDIS_HOST", "cache.example.com");
    vi.stubEnv("ROBOTHOR_REDIS_PORT", "6380");
    vi.stubEnv("ROBOTHOR_REDIS_PASSWORD", "secret");
    try {
      mockXrevrange.mockResolvedValue([]);
      await readRecent("email");
      expect(mockConstructor).toHaveBeenCalledTimes(1);
      expect(mockConstructor).toHaveBeenCalledWith(
        expect.objectContaining({
          host: "cache.example.com",
          port: 6380,
          password: "secret",
          db: 0,
          lazyConnect: true,
        })
      );
    } finally {
      vi.unstubAllEnvs();
    }
  });
});

describe("writeVital", () => {
  it("appends the metric to the vitals stream, capped, outside the event-bus streams", async () => {
    mockXadd.mockResolvedValue("1-0");

    await writeVital({ name: "LCP", value: 812.4, rating: "good", path: "/", view: "chat" });

    expect(mockXadd).toHaveBeenCalledTimes(1);
    const args = mockXadd.mock.calls[0];
    expect(args.slice(0, 4)).toEqual(["robothor:events:vitals", "MAXLEN", "~", "10000"]);
    expect(args[4]).toBe("*");
    const fields: Record<string, string> = {};
    for (let i = 5; i < args.length; i += 2) fields[args[i]] = args[i + 1];
    expect(fields).toMatchObject({ name: "LCP", value: "812.4", rating: "good", path: "/", view: "chat" });
    expect(fields.timestamp).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    // The live views never stream vitals: it is not one of the readable streams.
    expect(isValidStream("vitals")).toBe(false);
  });
});
