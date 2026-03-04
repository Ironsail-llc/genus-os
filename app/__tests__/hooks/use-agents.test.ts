import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { useAgents } from "@/hooks/use-agents";

describe("useAgents", () => {
  const mockAgents = [
    { name: "classifier", status: "healthy", schedule: "0 * * * *" },
    { name: "vision", status: "failed", schedule: "*/10 * * * *" },
    { name: "steward", status: "degraded", schedule: "0 10,18 * * *" },
  ];

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ success: true, data: { agents: mockAgents } }),
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("fetches agents on mount", async () => {
    const { result } = renderHook(() => useAgents());

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.agents).toHaveLength(3);
    expect(global.fetch).toHaveBeenCalledWith("/api/actions/execute", expect.objectContaining({
      method: "POST",
      body: expect.stringContaining("agent_status"),
    }));
  });

  it("computes summary correctly", async () => {
    const { result } = renderHook(() => useAgents());

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.summary).toEqual({
      healthy: 1,
      degraded: 1,
      failed: 1,
      sleeping: 0,
      total: 3,
    });
  });

  it("polls every 60 seconds", async () => {
    const { result } = renderHook(() => useAgents());

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    const initialCallCount = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.length;

    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });

    expect((global.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(initialCallCount);
  });

  it("handles fetch error gracefully", async () => {
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      json: async () => ({ error: "Bridge down" }),
    });

    const { result } = renderHook(() => useAgents());

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    // Should have empty agents, no crash
    expect(result.current.agents).toHaveLength(0);
  });

  it("refetches on visibility change after stale threshold", async () => {
    const { result } = renderHook(() => useAgents());

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    const callsBefore = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.length;

    // Simulate going hidden
    Object.defineProperty(document, "visibilityState", { value: "hidden", writable: true });
    document.dispatchEvent(new Event("visibilitychange"));

    // Advance past stale threshold
    vi.advanceTimersByTime(61_000);

    // Come back visible
    Object.defineProperty(document, "visibilityState", { value: "visible", writable: true });
    document.dispatchEvent(new Event("visibilitychange"));

    await waitFor(() => {
      expect((global.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(callsBefore);
    });
  });

  it("exposes refetch function", async () => {
    const { result } = renderHook(() => useAgents());

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(typeof result.current.refetch).toBe("function");
  });
});
