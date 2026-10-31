/**
 * The chat renders a trailing window of messages, not the whole session.
 *
 * History is capped at 50, so a loaded conversation always fits. A long live
 * session does not: past the window, the oldest bubbles stay out of the tree
 * until the reader asks for them.
 */
import { describe, it, expect } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useMessageWindow, MESSAGE_WINDOW } from "../use-message-window";

const many = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }));

describe("useMessageWindow", () => {
  it("renders everything when the session fits the window", () => {
    const messages = many(50);
    const { result } = renderHook(() => useMessageWindow(messages));
    expect(result.current.visible).toEqual(messages);
    expect(result.current.hiddenCount).toBe(0);
  });

  it("renders only the newest window of a longer session", () => {
    const messages = many(MESSAGE_WINDOW + 30);
    const { result } = renderHook(() => useMessageWindow(messages));
    expect(result.current.visible).toHaveLength(MESSAGE_WINDOW);
    expect(result.current.visible[0]).toBe(messages[30]);
    expect(result.current.visible.at(-1)).toBe(messages.at(-1));
    expect(result.current.hiddenCount).toBe(30);
  });

  it("shows another window of earlier messages on request", () => {
    const messages = many(MESSAGE_WINDOW * 2 + 10);
    const { result } = renderHook(() => useMessageWindow(messages));
    act(() => result.current.showEarlier());
    expect(result.current.hiddenCount).toBe(10);
    act(() => result.current.showEarlier());
    expect(result.current.hiddenCount).toBe(0);
    expect(result.current.visible).toEqual(messages);
  });

  it("keeps earlier messages shown as new ones arrive", () => {
    let messages = many(MESSAGE_WINDOW + 5);
    const { result, rerender } = renderHook(() => useMessageWindow(messages));
    act(() => result.current.showEarlier());
    messages = many(MESSAGE_WINDOW + 6);
    rerender();
    expect(result.current.hiddenCount).toBe(0);
  });

  it("returns the same array while nothing changes, so memoised rows stay put", () => {
    const messages = many(10);
    const { result, rerender } = renderHook(() => useMessageWindow(messages));
    const first = result.current.visible;
    rerender();
    expect(result.current.visible).toBe(first);
  });
});
