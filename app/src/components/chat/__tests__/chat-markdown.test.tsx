import { describe, it, expect, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";
import { ChatMarkdown } from "@/components/chat/chat-markdown";

// The real react-markdown and remark-gfm: the point is the rendered structure.
const REPLY = [
  "Here is what you can do:",
  "",
  "1. **Email** – read and send",
  "2. **Calendar** – check and book",
  "",
  "## Next steps",
  "",
  "Paragraph.",
].join("\n");

describe("ChatMarkdown", () => {
  it("renders a reply as structured blocks, not one run of text", () => {
    const { container } = render(<ChatMarkdown content={REPLY} />);
    const lists = container.querySelectorAll("ol");
    expect(lists).toHaveLength(1);
    expect(lists[0].querySelectorAll("li")).toHaveLength(2);
    expect(container.querySelectorAll("h2")).toHaveLength(1);
    expect(container.querySelectorAll("p").length).toBeGreaterThanOrEqual(2);
    expect(container.querySelectorAll("strong")).toHaveLength(2);
  });

  it("uses typography styles that follow the app theme", () => {
    const { container } = render(<ChatMarkdown content="Hello" />);
    const wrapper = container.firstElementChild as HTMLElement;
    const classes = wrapper.className.split(/\s+/);
    expect(classes).toContain("prose");
    expect(classes).toContain("dark:prose-invert");
    expect(classes).not.toContain("prose-invert");
  });

  it("renders GitHub-flavoured tables", () => {
    const { container } = render(
      <ChatMarkdown content={"| A | B |\n| - | - |\n| 1 | 2 |"} />,
    );
    expect(container.querySelector("table")).not.toBeNull();
  });

  it("opens links in a new tab without leaking the opener", () => {
    const { container } = render(
      <ChatMarkdown content="See [the docs](https://example.com/docs)." />,
    );
    const link = container.querySelector("a") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("https://example.com/docs");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("keeps same-page footnote anchors in the same tab, with their targets", () => {
    const { container } = render(
      <ChatMarkdown content={"A claim.[^1]\n\n[^1]: The source."} />,
    );
    const anchors = Array.from(container.querySelectorAll("a")).filter((a) =>
      (a.getAttribute("href") ?? "").startsWith("#"),
    );
    expect(anchors.length).toBeGreaterThanOrEqual(2);
    for (const a of anchors) {
      expect(a.hasAttribute("target")).toBe(false);
      const id = (a.getAttribute("href") ?? "").slice(1);
      expect(container.querySelector(`[id="${id}"]`)).not.toBeNull();
    }
  });

  describe("workspace links (H1)", () => {
    const ROOM = "11111111-1111-4111-8111-111111111111";

    it("opens a workspace link in the same tab, client-side, without a page load", () => {
      const push = vi.spyOn(window.history, "pushState");
      const heard = vi.fn();
      window.addEventListener("popstate", heard);
      const { container } = render(
        <ChatMarkdown content={`Open [Weekly sales](/?v=chat&room=${ROOM}).`} />,
      );
      const link = container.querySelector("a") as HTMLAnchorElement;
      expect(link.getAttribute("href")).toBe(`/?v=chat&room=${ROOM}`);
      expect(link.hasAttribute("target")).toBe(false);
      const click = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
      link.dispatchEvent(click);
      expect(click.defaultPrevented).toBe(true);
      expect(push).toHaveBeenCalledWith(null, "", `/?v=chat&room=${ROOM}`);
      expect(heard).toHaveBeenCalledTimes(1);
      window.removeEventListener("popstate", heard);
      push.mockRestore();
    });

    it("leaves a modified click to the browser (open in a new tab)", () => {
      const push = vi.spyOn(window.history, "pushState");
      const { container } = render(<ChatMarkdown content={`[Weekly sales](/?v=chat&room=${ROOM})`} />);
      const link = container.querySelector("a") as HTMLAnchorElement;
      fireEvent.click(link, { metaKey: true });
      fireEvent.click(link, { ctrlKey: true });
      expect(push).not.toHaveBeenCalled();
      push.mockRestore();
    });

    it("keeps external links exactly as they were", () => {
      const push = vi.spyOn(window.history, "pushState");
      const { container } = render(<ChatMarkdown content="[site](https://example.com/?v=chat)" />);
      const link = container.querySelector("a") as HTMLAnchorElement;
      expect(link.getAttribute("target")).toBe("_blank");
      fireEvent.click(link);
      expect(push).not.toHaveBeenCalled();
      push.mockRestore();
    });
  });
});
