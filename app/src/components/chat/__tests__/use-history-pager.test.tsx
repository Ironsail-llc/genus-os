import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useState } from "react";
import { useHistoryPager } from "@/components/chat/use-history-pager";
import type { HistoryMessage } from "@/lib/chat/history-messages";

const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

function page(ids: number[], hasMore: boolean) {
  return {
    ok: true,
    json: () =>
      Promise.resolve({
        hasMore,
        messages: ids.map((id) => ({
          id,
          role: id % 2 ? "user" : "assistant",
          content: `row ${id}`,
          timestamp: "2026-10-31T09:00:00Z",
        })),
      }),
  };
}

const LOADED: HistoryMessage[] = [
  { id: "hist-51", serverId: 51, role: "user", content: "row 51" },
  { id: "hist-52", serverId: 52, role: "assistant", content: "row 52" },
];

function setup(agentQuery = "") {
  return renderHook(
    ({ q }) => {
      const [messages, setMessages] = useState<HistoryMessage[]>(LOADED);
      const pager = useHistoryPager(q, messages, setMessages);
      return { messages, pager };
    },
    { initialProps: { q: agentQuery } },
  );
}

beforeEach(() => mockFetch.mockReset());

describe("useHistoryPager", () => {
  it("offers nothing until the first page says there is more", () => {
    const { result } = setup();
    expect(result.current.pager.hasMore).toBe(false);
  });

  it("asks for the page before the oldest message on screen and puts it above, in order", async () => {
    mockFetch.mockResolvedValueOnce(page([49, 50], false));
    const { result } = setup("?agent=scheduler");
    act(() => result.current.pager.settle(true));

    await act(async () => {
      await result.current.pager.loadEarlier();
    });

    const url = String(mockFetch.mock.calls[0][0]);
    expect(url).toMatch(/^\/api\/chat\/history\?agent=scheduler&/);
    expect(url).toContain("before=51");
    expect(result.current.messages.map((m) => m.serverId)).toEqual([49, 50, 51, 52]);
    expect(result.current.pager.hasMore).toBe(false);
  });

  it("never shows a message twice", async () => {
    mockFetch.mockResolvedValueOnce(page([50, 51], true));
    const { result } = setup();
    act(() => result.current.pager.settle(true));
    await act(async () => {
      await result.current.pager.loadEarlier();
    });
    expect(result.current.messages.map((m) => m.serverId)).toEqual([50, 51, 52]);
    expect(result.current.pager.hasMore).toBe(true);
  });

  it("drops a page that answers after the chat was pointed at another agent", async () => {
    let resolve!: (v: unknown) => void;
    mockFetch.mockReturnValueOnce(new Promise((r) => (resolve = r)));
    const { result, rerender } = setup("");
    act(() => result.current.pager.settle(true));
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.pager.loadEarlier();
    });
    rerender({ q: "?agent=scheduler" });
    await act(async () => {
      resolve(page([49, 50], false));
      await pending;
    });
    expect(result.current.messages.map((m) => m.serverId)).toEqual([51, 52]);
  });

  it("keeps the button when a page fails, so the reader can try again", async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({}) });
    const { result } = setup();
    act(() => result.current.pager.settle(true));
    await act(async () => {
      await result.current.pager.loadEarlier();
    });
    expect(result.current.pager.hasMore).toBe(true);
    expect(result.current.pager.loading).toBe(false);
    expect(result.current.messages).toHaveLength(2);
  });

  it("never prepends rows with no stored id, and stops offering when a page has none", async () => {
    // An engine with pages switched off answers any page with its in-memory
    // tail: the newest rows, without ids. Prepending them would show the tail twice.
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ messages: [{ role: "user", content: "row 51" }, { role: "assistant", content: "row 52" }] }),
    });
    const { result } = setup();
    act(() => result.current.pager.settle(true));
    await act(async () => {
      await result.current.pager.loadEarlier();
    });
    expect(result.current.messages.map((m) => m.content)).toEqual(["row 51", "row 52"]);
    expect(result.current.pager.hasMore).toBe(false);
  });
});

