import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { ChatMarkdown } from "@/components/chat/chat-markdown";

const writeText = vi.fn().mockResolvedValue(undefined);

beforeEach(() => {
  writeText.mockClear();
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});

const PY = ["Try this:", "", "```python", "def f():", "    return 'x'  # done", "```"].join("\n");

describe("code blocks in chat", () => {
  it("label the language", () => {
    render(<ChatMarkdown content={PY} />);
    expect(screen.getByTestId("code-language")).toHaveTextContent("python");
  });

  it("copy only the code, without fences or a trailing newline", async () => {
    render(<ChatMarkdown content={PY} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    });
    expect(writeText).toHaveBeenCalledWith("def f():\n    return 'x'  # done");
  });

  it("colour keywords, strings and comments with distinct classes", () => {
    const { container } = render(<ChatMarkdown content={PY} />);
    const kw = [...container.querySelectorAll(".tok-keyword")].map((n) => n.textContent);
    expect(kw).toEqual(["def", "return"]);
    expect(container.querySelector(".tok-string")?.textContent).toBe("'x'");
    expect(container.querySelector(".tok-comment")?.textContent).toBe("# done");
  });

  it("render a fence with no language as plain code with a generic label", () => {
    render(<ChatMarkdown content={"```\nplain text\n```"} />);
    expect(screen.getByTestId("code-language")).toHaveTextContent("code");
    expect(screen.getByText("plain text")).toBeInTheDocument();
  });

  it("leave inline code inline: no toolbar", () => {
    render(<ChatMarkdown content={"use `ls` here"} />);
    expect(screen.queryByRole("button", { name: "Copy code" })).toBeNull();
    expect(screen.getByText("ls").tagName).toBe("CODE");
  });
});
