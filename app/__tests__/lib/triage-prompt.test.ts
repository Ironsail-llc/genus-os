import { describe, it, expect, vi, beforeEach } from "vitest";

const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

import { triageDashboard, buildTriageUserPrompt } from "@/lib/dashboard/triage-prompt";

describe("buildTriageUserPrompt", () => {
  it("formats messages into user prompt", () => {
    const messages = [
      { role: "user", content: "How are the services?" },
      { role: "assistant", content: "Everything is running smoothly." },
    ];
    const prompt = buildTriageUserPrompt(messages);
    expect(prompt).toContain("User: How are the services?");
    expect(prompt).toContain("Assistant: Everything is running smoothly.");
    expect(prompt).toContain("Should the dashboard update?");
  });

  it("truncates long messages to 500 chars", () => {
    const messages = [{ role: "user", content: "x".repeat(1000) }];
    const prompt = buildTriageUserPrompt(messages);
    expect(prompt).not.toContain("x".repeat(600));
  });

  it("takes last 4 messages", () => {
    const messages = [
      { role: "user", content: "msg1" },
      { role: "assistant", content: "msg2" },
      { role: "user", content: "msg3" },
      { role: "assistant", content: "msg4" },
      { role: "user", content: "msg5" },
    ];
    const prompt = buildTriageUserPrompt(messages);
    expect(prompt).not.toContain("msg1");
    expect(prompt).toContain("msg2");
    expect(prompt).toContain("msg5");
  });
});

describe("triageDashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns shouldUpdate=true for substantive conversation", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          choices: [
            {
              message: {
                content:
                  '{"shouldUpdate": true, "dataNeeds": ["health"], "summary": "Service health"}',
              },
            },
          ],
        }),
    });

    const result = await triageDashboard(
      [
        { role: "user", content: "How are the services?" },
        { role: "assistant", content: "Checking..." },
      ],
      "test-api-key"
    );

    expect(result.shouldUpdate).toBe(true);
    expect(result.dataNeeds).toEqual(["health"]);
    expect(result.summary).toBe("Service health");
  });

  it("returns shouldUpdate=false for trivial conversation", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          choices: [
            {
              message: {
                content: '{"shouldUpdate": false, "dataNeeds": [], "summary": ""}',
              },
            },
          ],
        }),
    });

    const result = await triageDashboard(
      [
        { role: "user", content: "thanks" },
        { role: "assistant", content: "You're welcome!" },
      ],
      "test-api-key"
    );

    expect(result.shouldUpdate).toBe(false);
    expect(result.dataNeeds).toEqual([]);
  });

  it("returns shouldUpdate=false on API error (safe default)", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 500,
    });

    const result = await triageDashboard(
      [{ role: "user", content: "test" }],
      "test-api-key"
    );

    expect(result.shouldUpdate).toBe(false);
  });

  it("defaults to shouldUpdate=true on network error (graceful fallback)", async () => {
    mockFetch.mockRejectedValue(new Error("Network error"));

    const result = await triageDashboard(
      [{ role: "user", content: "test" }],
      "test-api-key"
    );

    // On triage error, default to updating — better to show something than silently skip
    expect(result.shouldUpdate).toBe(true);
    expect(result.dataNeeds).toContain("overview");
  });

  it("handles markdown-wrapped JSON response", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          choices: [
            {
              message: {
                content:
                  '```json\n{"shouldUpdate": true, "dataNeeds": ["web:weather"], "summary": "Weather"}\n```',
              },
            },
          ],
        }),
    });

    const result = await triageDashboard(
      [{ role: "user", content: "What's the weather?" }],
      "test-api-key"
    );

    expect(result.shouldUpdate).toBe(true);
    expect(result.dataNeeds).toEqual(["web:weather"]);
  });

  it("sends request with correct model and low max_tokens", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          choices: [
            {
              message: {
                content: '{"shouldUpdate": false, "dataNeeds": [], "summary": ""}',
              },
            },
          ],
        }),
    });

    await triageDashboard([{ role: "user", content: "test" }], "my-key");

    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("openrouter.ai"),
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining('"max_tokens":256'),
      })
    );
  });
});
