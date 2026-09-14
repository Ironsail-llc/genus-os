import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { useInbox } from "../use-inbox";

const QUESTION = {
  kind: "question",
  id: "11111111-1111-4111-8111-111111111111",
  run_id: "abcdef12-3456-4789-8abc-def012345678",
  agent_id: "invoice-chaser",
  question: "Which vendor should I chase first?",
  detail: "",
  options: ["Alice", "Bob"],
  expires_at: "2026-06-15T13:00:00Z",
  created_at: "2026-06-15T11:30:00Z",
};

const APPROVAL = {
  kind: "workflow",
  id: "22222222-2222-4222-8222-222222222222",
  run_id: "99887766-5544-4332-8110-aabbccddeeff",
  agent_id: "",
  question: "Send the quote to Alice?",
  detail: "The draft sits on the run.",
  options: [],
  expires_at: "2026-06-15T14:00:00Z",
  created_at: "2026-06-15T11:45:00Z",
};

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue(jsonResponse({ count: 2, pending: [QUESTION, APPROVAL] }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("useInbox", () => {
  it("reads the pending list off the bridge once on mount, for the badge", async () => {
    const { result } = renderHook(() => useInbox({ active: false }));

    await waitFor(() => expect(result.current.count).toBe(2));
    expect(fetchMock).toHaveBeenCalledWith("/api/bridge/api/approvals");
    expect(result.current.items.map((i) => i.id)).toEqual([QUESTION.id, APPROVAL.id]);
    expect(result.current.isLoading).toBe(false);
  });

  it("polls every 30 seconds while the view is on screen, and only then", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { result, rerender } = renderHook(
      ({ active }: { active: boolean }) => useInbox({ active }),
      { initialProps: { active: false } }
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Nothing on screen: the badge keeps the count it has rather than waking
    // an operator-gated route every half minute behind a view nobody opened.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(90_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    rerender({ active: true });
    // Opening the view refreshes at once, then on the interval.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);

    rerender({ active: false });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(result.current.error).toBeNull();
  });

  it("tells a non-operator apart from a broken appliance", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "operator only" }, 403));
    const { result } = renderHook(() => useInbox({ active: true }));

    await waitFor(() => expect(result.current.refusedAsNonOperator).toBe(true));
    expect(result.current.error).toBeNull();
    expect(result.current.count).toBe(0);
  });

  it("reports a refused listing with the bridge's own sentence", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "the engine is down" }, 500));
    const { result } = renderHook(() => useInbox({ active: true }));

    await waitFor(() => expect(result.current.error).toBe("the engine is down"));
    expect(result.current.refusedAsNonOperator).toBe(false);
  });

  it("says so when the bridge cannot be reached at all", async () => {
    fetchMock.mockRejectedValue(new Error("connection refused"));
    const { result } = renderHook(() => useInbox({ active: true }));

    await waitFor(() => expect(result.current.error).toMatch(/bridge/i));
  });

  it("posts a question answer as `answer` and drops the settled card", async () => {
    const { result } = renderHook(() => useInbox({ active: true }));
    await waitFor(() => expect(result.current.count).toBe(2));

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ settled: true, kind: "question", id: QUESTION.id })
    );

    let outcome: { settled: boolean; message: string | null } | undefined;
    await act(async () => {
      outcome = await result.current.answer(QUESTION.id, "question", { answer: "Alice" });
    });

    expect(fetchMock).toHaveBeenCalledWith(
      `/api/bridge/api/approvals/question/${QUESTION.id}`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ answer: "Alice" }),
      })
    );
    expect(outcome).toEqual({ settled: true, message: null });
    expect(result.current.items.map((i) => i.id)).toEqual([APPROVAL.id]);
    expect(result.current.count).toBe(1);
  });

  it("posts a workflow verdict as `approved` with the note", async () => {
    const { result } = renderHook(() => useInbox({ active: true }));
    await waitFor(() => expect(result.current.count).toBe(2));

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ settled: true, kind: "workflow", id: APPROVAL.id, decision: "rejected" })
    );

    await act(async () => {
      await result.current.answer(APPROVAL.id, "workflow", {
        approved: false,
        note: "Wrong figure.",
      });
    });

    expect(fetchMock).toHaveBeenCalledWith(
      `/api/bridge/api/approvals/workflow/${APPROVAL.id}`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ approved: false, note: "Wrong figure." }),
      })
    );
    expect(result.current.items.map((i) => i.id)).toEqual([QUESTION.id]);
  });

  it("keeps a card that the route says it did not settle, and carries the message back", async () => {
    const { result } = renderHook(() => useInbox({ active: true }));
    await waitFor(() => expect(result.current.count).toBe(2));

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ settled: false, message: "Already answered — nothing changed." })
    );

    let outcome: { settled: boolean; message: string | null } | undefined;
    await act(async () => {
      outcome = await result.current.answer(QUESTION.id, "question", { answer: "Alice" });
    });

    expect(outcome).toEqual({
      settled: false,
      message: "Already answered — nothing changed.",
    });
    expect(result.current.count).toBe(2);
  });

  it("carries a 4xx back as the route's own sentence", async () => {
    const { result } = renderHook(() => useInbox({ active: true }));
    await waitFor(() => expect(result.current.count).toBe(2));

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ settled: false, message: "answer is required" }, 400)
    );

    let outcome: { settled: boolean; message: string | null } | undefined;
    await act(async () => {
      outcome = await result.current.answer(QUESTION.id, "question", { answer: "" });
    });

    expect(outcome).toEqual({ settled: false, message: "answer is required" });
    expect(result.current.count).toBe(2);
  });

  it("refreshes on demand", async () => {
    const { result } = renderHook(() => useInbox({ active: false }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      await result.current.refresh();
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
