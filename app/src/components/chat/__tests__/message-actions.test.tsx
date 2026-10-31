import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { MessageActions } from "@/components/chat/message-actions";

const writeText = vi.fn().mockResolvedValue(undefined);

beforeEach(() => {
  writeText.mockClear();
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});

describe("MessageActions", () => {
  it("is a labelled toolbar of labelled buttons on an assistant message", () => {
    render(<MessageActions role="assistant" content="hi" busy={false} onRegenerate={vi.fn()} />);
    expect(screen.getByRole("toolbar", { name: "Message actions" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy message" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy as plain text" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regenerate response" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit message" })).toBeNull();
  });

  it("offers Edit, not Regenerate, on a user message", () => {
    render(<MessageActions role="user" content="hi" busy={false} onEdit={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Edit message" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Regenerate response" })).toBeNull();
  });

  it("copies the markdown exactly, and announces it", async () => {
    render(<MessageActions role="assistant" content={"**bold** `x`"} busy={false} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy message" }));
    });
    expect(writeText).toHaveBeenCalledWith("**bold** `x`");
    expect(screen.getByRole("status")).toHaveTextContent("Copied");
  });

  it("copies plain text with the markdown taken out", async () => {
    render(<MessageActions role="assistant" content={"**bold** `x`"} busy={false} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy as plain text" }));
    });
    expect(writeText).toHaveBeenCalledWith("bold x");
  });

  it("says so when the clipboard refuses", async () => {
    writeText.mockRejectedValueOnce(new Error("denied"));
    render(<MessageActions role="assistant" content="x" busy={false} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy message" }));
    });
    expect(screen.getByRole("status")).toHaveTextContent("Copy failed");
  });

  it("regenerates on click, and cannot while a turn is streaming", () => {
    const onRegenerate = vi.fn();
    const { rerender } = render(
      <MessageActions role="assistant" content="x" busy={false} onRegenerate={onRegenerate} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Regenerate response" }));
    expect(onRegenerate).toHaveBeenCalledTimes(1);

    rerender(<MessageActions role="assistant" content="x" busy onRegenerate={onRegenerate} />);
    const button = screen.getByRole("button", { name: "Regenerate response" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onRegenerate).toHaveBeenCalledTimes(1);
  });

  it("edits on click, and cannot while a turn is streaming", () => {
    const onEdit = vi.fn();
    const { rerender } = render(<MessageActions role="user" content="x" busy={false} onEdit={onEdit} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit message" }));
    expect(onEdit).toHaveBeenCalledTimes(1);
    rerender(<MessageActions role="user" content="x" busy onEdit={onEdit} />);
    expect(screen.getByRole("button", { name: "Edit message" })).toBeDisabled();
  });

  it("is reachable from the keyboard: every button is in the tab order", () => {
    render(<MessageActions role="assistant" content="x" busy={false} onRegenerate={vi.fn()} />);
    for (const button of screen.getAllByRole("button")) {
      expect(button.tabIndex).toBe(0);
    }
  });
});
