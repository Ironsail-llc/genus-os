import { describe, it, expect, vi, beforeEach } from "vitest";

const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

import { fetchPeople, fetchPerson, createPerson, searchPeople } from "@/lib/api/people";
import { fetchConversations, fetchMessages, sendMessage } from "@/lib/api/conversations";
import { searchMemory, storeMemory } from "@/lib/api/memory";
import { fetchHealth } from "@/lib/api/health";

function mockJsonResponse(data: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "OK",
    json: () => Promise.resolve(data),
  };
}

describe("People API", () => {
  beforeEach(() => vi.clearAllMocks());

  it("fetchPeople() returns typed Person[]", async () => {
    const people = [
      { id: "1", name: { firstName: "John", lastName: "Doe" } },
    ];
    mockFetch.mockResolvedValue(mockJsonResponse({ people }));

    const result = await fetchPeople();
    expect(result).toEqual(people);
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/bridge/api/people"),
      expect.any(Object)
    );
  });

  it("fetchPerson(id) returns single Person", async () => {
    const person = { id: "1", name: { firstName: "Jane", lastName: "Doe" } };
    mockFetch.mockResolvedValue(mockJsonResponse(person));

    const result = await fetchPerson("1");
    expect(result).toEqual(person);
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/bridge/api/people/1"),
      expect.any(Object)
    );
  });

  it("createPerson() sends POST with correct body", async () => {
    const newPerson = {
      id: "2",
      name: { firstName: "Alice", lastName: "Smith" },
    };
    mockFetch.mockResolvedValue(mockJsonResponse(newPerson));

    await createPerson({ name: { firstName: "Alice", lastName: "Smith" } });
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/bridge/api/people"),
      expect.objectContaining({ method: "POST" })
    );
  });

  it("searchPeople(query) passes search param", async () => {
    mockFetch.mockResolvedValue(mockJsonResponse({ people: [] }));

    await searchPeople("john");
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("search=john"),
      expect.any(Object)
    );
  });
});

describe("Conversations API", () => {
  beforeEach(() => vi.clearAllMocks());

  it("fetchConversations() returns typed Conversation[]", async () => {
    const convos = [{ id: 1, status: "open" }];
    mockFetch.mockResolvedValue(
      mockJsonResponse({ data: { meta: {}, payload: convos } })
    );

    const result = await fetchConversations();
    expect(result).toEqual(convos);
  });

  it("fetchMessages(conversationId) returns Message[]", async () => {
    const msgs = [{ id: 1, content: "Hello" }];
    mockFetch.mockResolvedValue(mockJsonResponse(msgs));

    const result = await fetchMessages(123);
    expect(result).toEqual(msgs);
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/bridge/api/conversations/123/messages"),
      expect.any(Object)
    );
  });

  it("sendMessage() sends POST correctly", async () => {
    mockFetch.mockResolvedValue(mockJsonResponse({ id: 1, content: "Reply" }));

    await sendMessage(123, "Reply");
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/bridge/api/conversations/123/messages"),
      expect.objectContaining({ method: "POST" })
    );
  });
});

describe("Memory API", () => {
  beforeEach(() => vi.clearAllMocks());

  it("searchMemory(query) returns typed results", async () => {
    const orchestratorResponse = {
      answer: "Test answer",
      query: "test",
      memories_found: 1,
      web_results_found: 0,
      sources: {
        memory: [
          { tier: "long_term", type: "technical", similarity: 0.85, preview: "Test fact" },
        ],
        web: [],
      },
    };
    mockFetch.mockResolvedValue(mockJsonResponse(orchestratorResponse));

    const result = await searchMemory("test");
    expect(result.length).toBe(2); // AI answer + 1 memory result
    expect(result[0].content).toBe("Test answer");
    expect(result[0].category).toBe("answer");
    expect(result[1].content).toBe("Test fact");
    expect(result[1].similarity).toBe(0.85);
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/orchestrator/query"),
      expect.objectContaining({ method: "POST" })
    );
  });

  it("storeMemory(content) sends POST correctly", async () => {
    mockFetch.mockResolvedValue(mockJsonResponse({ status: "ok" }));

    await storeMemory("A new fact");
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/orchestrator/ingest"),
      expect.objectContaining({ method: "POST" })
    );
  });
});

describe("Health API", () => {
  beforeEach(() => vi.clearAllMocks());

  it("fetchHealth() returns typed HealthResponse", async () => {
    const health = { status: "ok", services: [], timestamp: "2026-01-01" };
    mockFetch.mockResolvedValue(mockJsonResponse(health));

    const result = await fetchHealth();
    expect(result).toEqual(health);
  });
});
