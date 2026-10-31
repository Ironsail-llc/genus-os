import { describe, it, expect } from "vitest";
import { markdownToPlainText } from "../plain-text";

describe("markdownToPlainText", () => {
  it("drops emphasis and inline code marks", () => {
    expect(markdownToPlainText("**bold** and _it_ and `x`")).toBe("bold and it and x");
  });

  it("drops heading and quote markers", () => {
    expect(markdownToPlainText("# Title\n\n> quoted line")).toBe("Title\n\nquoted line");
  });

  it("keeps a link's text and an image's alt text", () => {
    expect(markdownToPlainText("see [the docs](https://example.com) ![a chart](x.png)")).toBe(
      "see the docs a chart",
    );
  });

  it("keeps a fenced block's code without the fences", () => {
    expect(markdownToPlainText("Run:\n\n```bash\nls -la\n```\n")).toBe("Run:\n\nls -la");
  });

  it("leaves list items readable", () => {
    expect(markdownToPlainText("- one\n- **two**")).toBe("- one\n- two");
  });

  it("does not eat a lone asterisk or underscore inside words", () => {
    expect(markdownToPlainText("2 * 3 = snake_case_name")).toBe("2 * 3 = snake_case_name");
  });
});
