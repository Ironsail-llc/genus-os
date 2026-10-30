import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
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
});
