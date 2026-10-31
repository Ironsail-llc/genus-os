/**
 * The chat follows the newest text only while the reader is at the bottom.
 *
 * It used to run a smooth scrollIntoView on every throttled update, which
 * restarted the animation ten times a second and dragged anyone reading back up
 * the conversation down to the bottom again.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { usePinnedScroll } from "../use-pinned-scroll";

/** jsdom lays nothing out, so the box's geometry is set by hand. */
function setGeometry(el: HTMLElement, { scrollHeight, clientHeight }: { scrollHeight: number; clientHeight: number }) {
  Object.defineProperty(el, "scrollHeight", { configurable: true, value: scrollHeight });
  Object.defineProperty(el, "clientHeight", { configurable: true, value: clientHeight });
}

function Harness({ tick, repin }: { tick: number; repin: number }) {
  const { containerRef, onScroll } = usePinnedScroll<HTMLDivElement>([tick], repin);
  return <div data-testid="box" ref={containerRef} onScroll={onScroll} />;
}

describe("usePinnedScroll", () => {
  it("follows new content while the reader is at the bottom", () => {
    const { getByTestId, rerender } = render(<Harness tick={0} repin={0} />);
    const box = getByTestId("box");
    setGeometry(box, { scrollHeight: 1000, clientHeight: 400 });
    rerender(<Harness tick={1} repin={0} />);
    expect(box.scrollTop).toBe(1000);
  });

  it("leaves the reader where they are once they scroll up", () => {
    const { getByTestId, rerender } = render(<Harness tick={0} repin={0} />);
    const box = getByTestId("box");
    setGeometry(box, { scrollHeight: 1000, clientHeight: 400 });
    box.scrollTop = 200; // 400 px above the bottom
    fireEvent.scroll(box);
    setGeometry(box, { scrollHeight: 1200, clientHeight: 400 });
    rerender(<Harness tick={1} repin={0} />);
    rerender(<Harness tick={2} repin={0} />);
    expect(box.scrollTop).toBe(200);
  });

  it("counts a reader within 80 px of the bottom as at the bottom", () => {
    const { getByTestId, rerender } = render(<Harness tick={0} repin={0} />);
    const box = getByTestId("box");
    setGeometry(box, { scrollHeight: 1000, clientHeight: 400 });
    box.scrollTop = 540; // 60 px above the bottom
    fireEvent.scroll(box);
    setGeometry(box, { scrollHeight: 1100, clientHeight: 400 });
    rerender(<Harness tick={1} repin={0} />);
    expect(box.scrollTop).toBe(1100);
  });

  it("follows again once the reader returns to the bottom", () => {
    const { getByTestId, rerender } = render(<Harness tick={0} repin={0} />);
    const box = getByTestId("box");
    setGeometry(box, { scrollHeight: 1000, clientHeight: 400 });
    box.scrollTop = 100;
    fireEvent.scroll(box);
    box.scrollTop = 600;
    fireEvent.scroll(box);
    setGeometry(box, { scrollHeight: 1300, clientHeight: 400 });
    rerender(<Harness tick={1} repin={0} />);
    expect(box.scrollTop).toBe(1300);
  });

  it("jumps to the bottom when the reader sends, wherever they were", () => {
    const { getByTestId, rerender } = render(<Harness tick={0} repin={0} />);
    const box = getByTestId("box");
    setGeometry(box, { scrollHeight: 1000, clientHeight: 400 });
    box.scrollTop = 0;
    fireEvent.scroll(box);
    setGeometry(box, { scrollHeight: 1100, clientHeight: 400 });
    rerender(<Harness tick={0} repin={1} />);
    expect(box.scrollTop).toBe(1100);
  });

  describe("when content grows with nothing else changing", () => {
    // Bubbles offscreen are laid out at a placeholder height until rendered,
    // so the list can grow after the jump to the bottom with no new message.
    let resize: (() => void) | null = null;
    afterEach(() => {
      vi.unstubAllGlobals();
      resize = null;
    });
    function stubResizeObserver() {
      vi.stubGlobal(
        "ResizeObserver",
        class {
          constructor(cb: () => void) {
            resize = cb;
          }
          observe() {}
          unobserve() {}
          disconnect() {}
        },
      );
    }

    it("stays at the bottom while pinned", () => {
      stubResizeObserver();
      const { getByTestId } = render(<Harness tick={0} repin={0} />);
      const box = getByTestId("box");
      setGeometry(box, { scrollHeight: 1600, clientHeight: 400 });
      resize?.();
      expect(box.scrollTop).toBe(1600);
    });

    it("leaves a reader who scrolled up where they are", () => {
      stubResizeObserver();
      const { getByTestId } = render(<Harness tick={0} repin={0} />);
      const box = getByTestId("box");
      setGeometry(box, { scrollHeight: 1000, clientHeight: 400 });
      box.scrollTop = 100;
      fireEvent.scroll(box);
      setGeometry(box, { scrollHeight: 1600, clientHeight: 400 });
      resize?.();
      expect(box.scrollTop).toBe(100);
    });
  });
});
