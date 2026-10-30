import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock redis-client — factory must not reference outer variables (hoisting)
vi.mock("@/lib/event-bus/redis-client", () => ({
  streamLengths: vi.fn(),
}));

// Must import after mock
import { GET } from "@/app/api/events/stats/route";
import { streamLengths } from "@/lib/event-bus/redis-client";

const mockStreamLengths = vi.mocked(streamLengths);

beforeEach(() => {
  vi.clearAllMocks();
});

describe("GET /api/events/stats", () => {
  it("returns stream lengths and total", async () => {
    mockStreamLengths.mockResolvedValue({
      email: 100,
      crm: 50,
      health: 25,
      calendar: 10,
      vision: 0,
      agent: 5,
      system: 3,
    });

    const response = await GET();
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.streams.email).toBe(100);
    expect(data.streams.crm).toBe(50);
    expect(data.total).toBe(193);
    expect(data.timestamp).toBeDefined();
  });

  it("returns 503 when redis unavailable", async () => {
    mockStreamLengths.mockRejectedValue(new Error("Redis down"));

    const response = await GET();
    expect(response.status).toBe(503);

    const data = await response.json();
    expect(data.error).toBe("Event bus unavailable");
  });

  it("returns 0 total when all streams empty", async () => {
    mockStreamLengths.mockResolvedValue({
      email: 0,
      crm: 0,
      health: 0,
      calendar: 0,
      vision: 0,
      agent: 0,
      system: 0,
    });

    const response = await GET();
    const data = await response.json();

    expect(data.total).toBe(0);
  });
});
