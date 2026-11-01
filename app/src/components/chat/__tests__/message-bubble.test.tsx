import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MessageBubble } from "@/components/chat/message-bubble";

const noop = vi.fn();

describe("MessageBubble", () => {
  it("shows the time the message was sent, with the exact instant in <time>", () => {
    const when = new Date("2026-10-31T09:00:00Z");
    const { container } = render(
      <MessageBubble msg={{ id: "a", role: "assistant", content: "hi", timestamp: when }} onRecovered={noop} />,
    );
    const time = container.querySelector("time");
    expect(time).not.toBeNull();
    expect(time).toHaveAttribute("datetime", "2026-10-31T09:00:00.000Z");
    expect(time?.textContent).not.toBe("");
  });

  it("shows no time when the message has none", () => {
    const { container } = render(
      <MessageBubble msg={{ id: "a", role: "assistant", content: "hi" }} onRecovered={noop} />,
    );
    expect(container.querySelector("time")).toBeNull();
  });

  it("hands its own id to Regenerate and Edit", () => {
    const onRegenerate = vi.fn();
    const onEdit = vi.fn();
    render(
      <>
        <MessageBubble
          msg={{ id: "a1", role: "assistant", content: "reply" }}
          onRecovered={noop}
          onRegenerate={onRegenerate}
          onEdit={onEdit}
        />
        <MessageBubble
          msg={{ id: "u1", role: "user", content: "ask" }}
          onRecovered={noop}
          onRegenerate={onRegenerate}
          onEdit={onEdit}
        />
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Regenerate response" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit message" }));
    expect(onRegenerate).toHaveBeenCalledWith("a1");
    expect(onEdit).toHaveBeenCalledWith("u1");
  });

  it("disables Regenerate while busy", () => {
    render(
      <MessageBubble
        msg={{ id: "a1", role: "assistant", content: "reply" }}
        onRecovered={noop}
        onRegenerate={vi.fn()}
        busy
      />,
    );
    expect(screen.getByRole("button", { name: "Regenerate response" })).toBeDisabled();
  });

  it("offers no actions on a message without callbacks", () => {
    render(<MessageBubble msg={{ id: "a1", role: "assistant", content: "reply" }} onRecovered={noop} />);
    expect(screen.queryByRole("button", { name: "Regenerate response" })).toBeNull();
    // Copy needs nothing from the panel, so it is always there.
    expect(screen.getByRole("button", { name: "Copy message" })).toBeInTheDocument();
  });
});
