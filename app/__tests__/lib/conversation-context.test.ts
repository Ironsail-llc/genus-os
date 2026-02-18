import { describe, it, expect, vi, beforeEach } from "vitest";

const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

import {
  fetchConversationContext,
  fetchDataForNeeds,
  fetchWebSearch,
} from "@/lib/dashboard/conversation-context";

describe("fetchConversationContext", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  function mockJsonResponse(data: unknown) {
    return {
      ok: true,
      json: () => Promise.resolve(data),
    };
  }

  it("fetches contacts data for contacts topic", async () => {
    mockFetch.mockResolvedValue(
      mockJsonResponse({ data: [{ id: "1", name: { firstName: "Alice", lastName: "Smith" } }] })
    );

    const result = await fetchConversationContext("contacts");
    expect(result.topic).toBe("contacts");
    expect(result.data).toBeDefined();
    expect(result.timestamp).toBeTruthy();

    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/people"),
      expect.any(Object)
    );
  });

  it("fetches inbox data for inbox topic", async () => {
    mockFetch.mockResolvedValue(
      mockJsonResponse({ data: { payload: [{ id: 1, unread_count: 2 }] } })
    );

    const result = await fetchConversationContext("inbox");
    expect(result.topic).toBe("inbox");
    expect(result.data).toBeDefined();
  });

  it("fetches health checks for health topic", async () => {
    mockFetch.mockResolvedValue(mockJsonResponse({ status: "ok" }));

    const result = await fetchConversationContext("health");
    expect(result.topic).toBe("health");
    expect(result.data).toBeDefined();

    expect(mockFetch.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("fetches orchestrator query for memory topic", async () => {
    mockFetch.mockResolvedValue(
      mockJsonResponse({ answer: "Philip worked on the CRM project last week" })
    );

    const result = await fetchConversationContext("memory", "CRM project");
    expect(result.topic).toBe("memory");
    expect(result.data).toBeDefined();

    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("/query"),
      expect.objectContaining({ method: "POST" })
    );
  });

  it("returns empty data on fetch failure (graceful degradation)", async () => {
    mockFetch.mockRejectedValue(new Error("Network error"));

    const result = await fetchConversationContext("contacts");
    expect(result.topic).toBe("contacts");
    expect(result.data).toBeDefined();
  });

  it("fetches multiple sources for overview topic", async () => {
    mockFetch.mockResolvedValue(mockJsonResponse({ status: "ok" }));

    const result = await fetchConversationContext("overview");
    expect(result.topic).toBe("overview");
    expect(result.data).toBeDefined();

    expect(mockFetch.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("fetches calendar data for calendar topic", async () => {
    mockFetch.mockResolvedValue(
      mockJsonResponse({ answer: "Meeting at 2pm with the team" })
    );

    const result = await fetchConversationContext("calendar");
    expect(result.topic).toBe("calendar");
    expect(result.data).toBeDefined();
  });
});

describe("fetchWebSearch", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("fetches from SearXNG and returns formatted results", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          results: [
            { title: "Weather NYC", url: "https://example.com", content: "Sunny, 75F" },
            { title: "NYC Forecast", url: "https://example.com/2", content: "Clear skies" },
          ],
        }),
    });

    const result = await fetchWebSearch("weather NYC");
    expect(result.query).toBe("weather NYC");
    expect(result.resultCount).toBe(2);
    expect(result.results).toHaveLength(2);
    expect((result.results as Array<{ title: string }>)[0].title).toBe("Weather NYC");
  });

  it("returns empty results on error", async () => {
    mockFetch.mockRejectedValue(new Error("SearXNG down"));

    const result = await fetchWebSearch("test query");
    expect(result.query).toBe("test query");
    expect(result.results).toEqual([]);
    expect(result.resultCount).toBe(0);
  });

  it("limits results to 8", async () => {
    const results = Array.from({ length: 15 }, (_, i) => ({
      title: `Result ${i}`,
      url: `https://example.com/${i}`,
      content: `Content ${i}`,
    }));

    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ results }),
    });

    const result = await fetchWebSearch("test");
    expect((result.results as unknown[]).length).toBe(8);
  });
});

describe("fetchDataForNeeds", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns empty object for empty needs array", async () => {
    const result = await fetchDataForNeeds([]);
    expect(result).toEqual({});
  });

  it("fetches health data for health need", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ status: "ok" }),
    });

    const result = await fetchDataForNeeds(["health"]);
    expect(result.health).toBeDefined();
  });

  it("fetches multiple data sources in parallel", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ data: [], status: "ok" }),
    });

    const result = await fetchDataForNeeds(["health", "contacts"]);
    expect(result.health).toBeDefined();
    expect(result.contacts).toBeDefined();
  });

  it("handles web:<query> data need", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          results: [{ title: "Test", url: "https://example.com", content: "Content" }],
        }),
    });

    const result = await fetchDataForNeeds(["web:weather NYC"]);
    expect(result.web).toBeDefined();
    expect((result.web as { query: string }).query).toBe("weather NYC");
  });

  it("handles memory:<query> data need", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ answer: "Found it" }),
    });

    const result = await fetchDataForNeeds(["memory:CRM project"]);
    expect(result.memory).toBeDefined();
    expect((result.memory as { query: string }).query).toBe("CRM project");
  });

  it("handles failures gracefully — partial results", async () => {
    let callNum = 0;
    mockFetch.mockImplementation(() => {
      callNum++;
      if (callNum <= 3) {
        // Health checks succeed
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ status: "ok" }),
        });
      }
      // Contacts fails
      return Promise.reject(new Error("Network error"));
    });

    const result = await fetchDataForNeeds(["health", "contacts"]);
    // Health should succeed even if contacts fails
    expect(result.health).toBeDefined();
  });
});
